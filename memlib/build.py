"""账本构建：跑全部导入器 → 去重 → 应用裁决 → 跨源互证 → 标注身份 → 落盘。

## 写路径唯一提交者

导入器只产候选，**不写账本**。落盘只发生在这里，而且是一次性的原子替换
（写 `.tmp` 再 `replace`）——中途失败不会留下半个账本。

## 顺序是有意义的

1. 导入（六路）→ 去重
2. 应用 `overrides.jsonl`（人的裁决能覆盖导入结果，所以必须在互证之前）
3. 跨源互证升格 verified
4. 标注身份与可见性
5. 排序后落盘

把裁决放在互证之后会让人裁过的条目被"自动升格"再改一遍状态，
那是账本和人的裁决打架，且人永远看不到。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path

from . import atomic, importers, report
from .schema import norm as _norm
from . import store as _store
from .store import annotate_identity, apply_overrides, corroborate
from .text import load_aliases


def load_sources(cfg) -> tuple[list[dict], dict]:
    """跑全部启用中的导入器，按 id 去重。"""
    reg = cfg.importer("registry").get("models")
    load_aliases(reg)          # 必须最先：subj 归一依赖 models.yaml
    claims, stats = [], {}
    for name, mod in importers.enabled_modules(cfg):
        rows, st = mod.run(cfg)
        claims.extend(rows)
        stats[name] = st
    # 去重：同 id 只留一条
    seen, uniq = set(), []
    for c in claims:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        uniq.append(c)
    return uniq, stats


def build(cfg, with_report: bool = False, strict: bool = False) -> int:
    claims_path = cfg.path_of("claims")
    ledger = cfg.path_of("ledger")
    ledger.mkdir(parents=True, exist_ok=True)

    claims, stats = load_sources(cfg)
    claims, log = apply_overrides(
        claims, cfg.path_of("overrides"),
        cfg.identity("default_visibility"), cfg.identity("new_write_visibility"))
    n_corr = corroborate(claims)
    annotate_identity(claims, cfg.identity("default_visibility"))

    # 复核日（2026-11-16）：分层办法 §5 的「按谓词类型定 revalidate_after」
    # 到这里才真的有人算。写在落盘前，让「什么时候该看」跟着条目走。
    # 算不出来（谓词没配 TTL / 日期不认识）就留空——空表示"不需要复核"，
    # 不是"复核日是今天"。来源是实时派生的（配置/命令输出）也留空：
    # 每次 build 重读一遍就是答案，没有"该复核日"这回事。
    from .ttl import revalidate_after as _rva
    tmap = cfg.ttl()
    derived = cfg.ttl_derived_origins()
    for c in claims:
        c["revalidate_after"] = _rva(c, tmap, derived) or None

    if strict:
        from .schema import validate
        bad = [(c["id"], validate(c)) for c in claims]
        bad = [(i, p) for i, p in bad if p]
        if bad:
            print(f"--strict：{len(bad)} 条不合契约，拒绝落盘")
            for i, p in bad[:10]:
                print(f"  {i}: {p}")

    # 分片（2026-11-17）：已归档的条目不属于主账本。**只按「状态没变」排除**——
    # 归档后又被裁决重新打开的（rejected → verified），必须回到主账本，
    # 否则人的裁决就被一次归档静默吃掉了。检索层两边都读，看不见的只有文件大小。
    archived = _store.load_archived(ledger)
    n_sharded = 0
    if archived:
        kept = [c for c in claims if archived.get(c["id"]) != c["status"]]
        n_sharded = len(claims) - len(kept)
        claims = kept

    claims.sort(key=lambda c: (c["scope"], _norm(c["subject"]), _norm(c["predicate"]), c["id"]))
    # 原子落盘（2026-11-17）：固定 `.tmp` 名在并发 build 下会互相截断，
    # 见 `memlib/atomic.py`。这里用唯一临时名，谁最后 replace 谁赢，但不会出混合账本。
    atomic.write_jsonl(claims_path, claims)

    print(f"账本：{len(claims)} 条 -> {claims_path}"
          + (f"（另 {n_sharded} 条在年度分片里，检索照读）" if n_sharded else ""))
    for k, s in stats.items():
        print(f"  {k:<9} 读入 {s.get('total', 0):>5}  导入 {s.get('imported', 0):>5}")
    print(f"  裁决 {len(log)} 条，跨源互证 {n_corr} 组")
    print(f"  状态：{dict(Counter(c['status'] for c in claims))}")
    if with_report:
        # 整合提案：写操作放在 build 里、写得明明白白，不藏在"生成报告"的函数里。
        # 只追加 proposals.jsonl，不动 overrides——提案要人 --accept 才成为裁决。
        from . import dream as _dream
        _prop_path = cfg.path_of("proposals")
        _ps = _dream.pending(claims, _prop_path)
        _new = _dream.record(_prop_path, _ps)

        rp = cfg.path_of("reports") / f"冲突报告-{date.today().isoformat()}.md"
        nc, structured = report.write_report(claims, stats, rp, cfg=cfg,
                                             new_proposals=_new)
        print(f"报告：{nc} 组冲突 -> {rp}（结构化 {structured} 条）")
        print(f"整合提案：新记录 {_new} 条 / 待处理 {len(_ps)} 条（**未自动应用任何一条**）")
        # 复核周期也报一行：报告里写了，但终端上看不看得到是两回事——
        # 每日 04:20 的 systemd 日志里，这一行就是它的入口。
        from .ttl import due as _due
        _d = _due(claims, cfg.ttl(), derived_prefixes=cfg.ttl_derived_origins())
        print("复核周期：已过期 {0} / 将到期 {1} / 日期不明 {2} / 实时派生 {3}".format(
            len(_d["已过期"]), len(_d["将到期"]), len(_d["日期不明"]), len(_d["实时派生"])))
        # probe 是**可选**模块：它绑死 models.yaml / llama.cpp 的 /props 契约，
        # 核心发行版不带。没有就跳过这一节，不在报告里凭空写"全部一致"。
        try:
            from .probe import run as _probe
        except ImportError:
            _probe = None
        _p = _probe(cfg) if _probe else None
        if _p:
            print("探活：在线 {0} / 配置说常驻但没起 {1} / 上下文不符 {2}".format(
                len(_p["在线"]), len(_p["配置说常驻但没起"]), len(_p["上下文不符"])))
    return 0


def read_claims(cfg) -> list[dict]:
    """主账本 + 年度分片。**归档只改文件位置，不改"账本里有没有这条"**——
    stats / gaps / report / query 读到的条数不能因为一次 shard 就变少。"""
    p = Path(cfg.path_of("claims"))
    out: list[dict] = []
    seen: set[str] = set()
    for f in [p] + _store.shard_files(p.parent):
        if not f.exists():
            continue
        for ln in f.open(encoding="utf-8"):
            if not ln.strip():
                continue
            c = json.loads(ln)
            cid = str(c.get("id") or "")
            if cid and cid in seen:       # 主账本先出现，同 id 跳过后面的分片副本
                continue
            if cid:
                seen.add(cid)
            out.append(c)
    return out
