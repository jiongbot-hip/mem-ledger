"""冲突报告 / 端口索引 / 路径体检。

报告**只列候选，不判对错**。真正的裁决写在 `overrides.jsonl`，由人做。
这个边界要守住：报告一旦开始"自动判定谁对"，人就会停止复核，
而它判错的时候没人会发现。
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import date
from pathlib import Path

from .schema import ACTIVE, norm as _norm
from .store import conflicts, origin_system
from .text import norm_value


def port_index(claims: list[dict]) -> tuple[dict[int, list[dict]], list[dict]]:
    """端口 → 主张列表；同时返回「同一端口被不同主体认领」的冲突。"""
    idx: dict[int, list[dict]] = {}
    for c in claims:
        if c["predicate"].lower() not in ("端口", "port"):
            continue
        m = re.search(r"\d{2,5}", c["value"])
        if not m:
            continue
        n = int(m.group())
        if not (1 <= n <= 65535):
            continue
        idx.setdefault(n, []).append(c)

    def same_entity(a: str, b: str) -> bool:
        na, nb = _norm(a), _norm(b)
        if not na or not nb:
            return False
        if na == nb or na in nb or nb in na:
            return True
        # 去掉同一主体的常见后缀再比：服务/容器/service/实例/模型
        def strip(s):
            return re.sub(r"(服务|容器|实例|进程|服务端|service|server|容器内)$", "", s)
        sa, sb = strip(na), strip(nb)
        return bool(sa and sb) and (sa == sb or sa in sb or sb in sa)

    bad = []
    for n, group in idx.items():
        if n < 1024:
            continue
        subs = sorted({c["subject"] for c in group if c["subject"]})
        clusters: list[list[str]] = []
        for s in subs:
            for cl in clusters:
                if any(same_entity(s, x) for x in cl):
                    cl.append(s)
                    break
            else:
                clusters.append([s])
        if len(clusters) > 1:
            bad.append({"port": n, "group": group, "clusters": clusters})
    bad.sort(key=lambda x: x["port"])
    return idx, bad


def path_check(claims: list[dict], cap: int = 40) -> tuple[int, int, list[dict]]:
    """路径主张体检：总条数、已不存在的条数、不存在且未裁决的前 cap 条。"""
    total = missing = 0
    rows = []
    for c in claims:
        if c["predicate"].lower() not in ("路径", "path"):
            continue
        if c["status"] in ("rejected", "superseded", "stale"):
            continue          # 已裁决过的不再重复提示
        v = c["value"]
        if not v.startswith(("/", "~")):
            continue
        # 占位符与 URL 片段不是文件路径
        if re.search(r"[<>{}]", v) or re.match(r"^/(v1|v2|v1beta|api)(/|$)", v):
            continue
        total += 1
        p = Path(os.path.expanduser(v))
        try:
            ok = p.exists()
        except OSError:
            ok = False
        if not ok:
            missing += 1
            if len(rows) < cap:
                rows.append(c)
    return total, missing, rows


def _ttl_section(claims: list[dict], cfg, limit: int = 40) -> list[str]:
    """复核周期进日报（2026-11-16）。`cfg` 为空就不出这一节（旧调用方不受影响）。

    ## 为什么要有这一段

    `auto_mark = false` 意味着**没有任何东西会自动提醒你**——`mem ttl` 得有人手动跑。
    一个要人记得才会跑的检查，等于没有检查。写进每天 04:20 重建出来的这份报告里，
    它至少每天被生成一次、被看见一次。
    """
    if cfg is None:
        return []
    from .ttl import due as _due
    tm, der = cfg.ttl(), cfg.ttl_derived_origins()
    if not tm:
        return []
    d = _due(claims, tm, derived_prefixes=der)
    L = ["## 8. 复核周期（TTL）：该重新核实的主张", "",
         "> 按谓词类型定复核周期（`config.toml` 的 `[ttl]`）。**只报告，不改状态**——"
         "`stale` 会让条目从检索面消失，那得人看过后再决定。", "",
         "| 类别 | 条数 | 含义 |", "|---|---|---|",
         f"| 已过期 | {len(d['已过期'])} | 超过周期，用前必须核对 |",
         f"| 将到期 | {len(d['将到期'])} | 还剩 1/4 周期以内 |",
         f"| 日期不明 | {len(d['日期不明'])} | 算不出日期，**没法判断**，不是没问题 |",
         f"| 实时派生 | {len(d['实时派生'])} | 每次 build 从配置/命令重读，TTL 不适用 |", ""]
    if not (d["已过期"] or d["将到期"] or d["日期不明"]):
        L += ["今天没有被判过期的主张。这一节全 0 不等于「检查没在工作」："
              "判据来自每条主张自己的 `valid_from`，"
              "空日期会进「日期不明」而不是被当成新鲜。", ""]
    if d["已过期"]:
        L += ["### 8.1 已过期（最久没核实的在前）", ""]
        for x in d["已过期"][:limit]:
            c = x["claim"]
            L.append(f"- `{c['id']}` **{x['predicate']}** 超期 {x['over']} 天 / "
                     f"{c['origin']}：`{(c.get('value') or '')[:80]}`")
            L.append(f"  → 核实后 `mem verify {c['id']} --basis …`，"
                     f"或 `mem stale {c['id']} --why …`")
        L.append("")
    if d["将到期"]:
        L += ["### 8.2 将到期（还剩 1/4 周期以内）", ""]
        for x in d["将到期"][:limit]:
            c = x["claim"]
            L.append(f"- `{c['id']}` **{x['predicate']}** 还剩 {x['left']} 天 / "
                     f"{c['origin']}：`{(c.get('value') or '')[:80]}`")
        L.append("")
    if d["日期不明"]:
        L += ["### 8.3 日期不明（有复核周期但算不出日期）", "",
              "这三类来源都可能缺日期：散文抽取（应带文件 mtime）、人工写入"
              "（`propose`/`verify` 现在会记）、或历史条目。**不要当成没问题。**", ""]
        for x in d["日期不明"][:limit]:
            c = x["claim"]
            L.append(f"- `{c['id']}` **{x['predicate']}** / {c['origin']}")
        L.append("")
    who: dict[str, int] = {}
    for x in d["实时派生"]:
        o = x["claim"].get("origin", "")
        who[o] = who.get(o, 0) + 1
    if who:
        L += ["实时派生：" + "、".join(f"{k} {n}" for k, n in sorted(who.items())) + "。",
              "> 这些值每次 build 从配置/命令输出重读，重读就是复核。"
              "它们证明「配置这么写」，不证明「机器实际如此」——后者要探活。", ""]
    return L


def _probe_section(cfg, limit: int = 30) -> list[str]:
    """探活进日报（2026-11-16）。TTL 那一节说「实时派生要探活」，这里给出探活结果。

    没有这一段的话，「配置写 up、进程早死了」这类错只能靠人想起来手动跑 `mem probe`——
    和 TTL 原来那个「要人记得才跑」的毛病一样。
    """
    if cfg is None:
        return []
    # probe 是可选模块（绑死 models.yaml 契约），核心发行版不带。
    # 没有就不出这一节——**不能**出一节写着"全部一致"，那是把"没测"说成"没问题"。
    try:
        from .probe import run as _probe
    except ImportError:
        return []
    try:
        d = _probe(cfg)
    except Exception as e:                     # 探活失败不能拖垮整份报告
        return ["## 9. 探活：配置 vs 实际", "",
                f"> 探活没跑成：{e}。这不是「都正常」——是**没测**。", ""]
    bad = d.get("配置说常驻但没起") or []
    drift = d.get("上下文不符") or []
    weird = d.get("在跑但配置说没起") or []
    L = ["## 9. 探活：配置 vs 实际", "",
         "> 只看 `models.yaml` 里 `state: up` 的模型端口通不通，加一条 `GET /props` 比上下文。"
         "**只读**：不启停任何服务。", "",
         "| 判定 | 个数 |", "|---|---|",
         f"| 在线 | {len(d.get('在线') or [])} |",
         f"| 配置说常驻但没起 | {len(bad)} |",
         f"| 在跑但配置说没起 | {len(weird)} |",
         f"| 上下文不符 | {len(drift)} |",
         f"| 按需/下线（符合配置） | {len(d.get('按需/下线（符合配置）') or [])} |", ""]
    if bad:
        L += ["### 9.1 配置说常驻、端口上没人", "",
              "| 模型 | 端口 | unit |", "|---|---|---|"]
        for x in bad[:limit]:
            L.append(f"| {x['model']} | {x['port']} | {x.get('service') or '—'} |")
        L += ["", "要么起服务，要么把 `models.yaml` 的 `state` 改成实际状态——"
                  "两个都对，但**别让它继续这么写着**。", ""]
    if drift:
        L += ["### 9.2 上下文不符（配置 vs 实际 n_ctx）", ""]
        for x in drift[:limit]:
            L.append(f"- {x['model']} :{x['port']} 配置 {x['配置']} / 实际 {x['实际']}")
        L.append("")
    if weird:
        L += ["### 9.3 在跑但配置说没起", ""]
        for x in weird[:limit]:
            L.append(f"- {x['model']} :{x['port']} state={x['state'] or '未写'}")
        L.append("")
    if not (bad or drift or weird):
        L += ["配置与实际的模型端口一致。", ""]
    return L


def _dream_section(claims: list[dict], cfg, limit: int = 30,
                   newly_recorded: int = -1) -> list[str]:
    """整合提案进日报（2026-11-16）。

    `mem dream` 之前只能人手动跑，于是「报告每周列同一批问题、没人处理」照旧。
    每天 04:20 重建时 `build` 会自动 `--record` 一次新的（写操作放在 build 里，
    不藏在"生成一段文字"的函数里），报告只负责把待处理清单摆出来。

    **只记录、不应用。** 记的是"脚本觉得该这么做"，裁决仍然要人 `--accept`。
    `newly_recorded < 0` 表示本次没记录（比如自测直接调这个函数）。
    """
    if cfg is None:
        return []
    from . import dream as _dream
    path = cfg.path_of("proposals")
    try:
        ps = _dream.pending(claims, path)
    except OSError as e:
        return ["## 10. 整合提案", "", f"> 读取失败：{e}。这不等于「没有提案」。", ""]
    head = f"累计待处理 **{len(ps)}** 条（`{path.name}`，只追加）"
    if newly_recorded >= 0:
        head = f"本次新记录 **{newly_recorded}** 条，" + head
    L = ["## 10. 整合提案（待你点头，**没有任何一条被自动应用**）", "", head + "。", ""]
    if not ps:
        L += ["没有待处理的整合提案。", ""]
        return L
    L += ["| 提案 | 类型 | 建议动作 | 目标 | 为什么 |", "|---|---|---|---|---|"]
    for p in ps[:limit]:
        why = (p.get("why") or "").replace("|", "／")[:90]
        L.append(f"| `{p['id']}` | {p['kind']} | {p['action']} | `{p['target']}` | {why} |")
    if len(ps) > limit:
        L.append(f"| … | | | | 另有 {len(ps) - limit} 条 |")
    L += ["", "处理：`mem dream --accept <id>` / `mem dream --reject <id> --why …`。"
              "`supersede` 类提案不能一键接受——它必须带 `--by` 指向真实后任。", ""]
    return L


def write_report(claims: list[dict], stats: dict, out: Path, limit: int = 80, cfg=None,
                 new_proposals: int = -1) -> tuple[int, int]:
    cs = conflicts(claims)
    pidx, pconf = port_index(claims)
    ptotal, pmiss, prows = path_check(claims)
    by_origin = Counter(c["origin"].split(":")[0] for c in claims)
    by_status = Counter(c["status"] for c in claims)
    by_ev = Counter(c["evidence"] for c in claims)
    structured = sum(1 for c in claims if c["subject"] and c["predicate"])

    L = [f"# 记忆账本冲突报告（{date.today().isoformat()}）", "",
         "> `claims.jsonl` 是生成物；人的裁决写在 `overrides.jsonl`。重新 `build` 不会丢裁决。",
         "> 本报告只列**候选**：叙述型冲突需要人读原文，高风险冲突（路径/端口/版本）可直接核对。", "",
         "## 1. 账本规模", "",
         "| 来源 | 条数 |", "|---|---|"]
    for k, v in by_origin.most_common():
        L.append(f"| {k} | {v} |")
    L += ["", f"合计 **{len(claims)}** 条；其中结构化（有 subject+predicate）**{structured}** 条"
              f"（{structured * 100 // max(1, len(claims))}%）。", "",
          "状态分布：" + "，".join(f"{k} {v}" for k, v in by_status.most_common()) + "。",
          "", "证据等级：" + "，".join(f"{k} {v}" for k, v in by_ev.most_common()) + "。", "",
          "## 2. 导入统计", "",
          "| 来源 | 读入 | 导入 | 跳过/无效 |", "|---|---|---|---|"]
    for k, s in stats.items():
        if not isinstance(s, dict):
            continue
        skip = s.get("invalid", 0) + s.get("skipped", 0)
        L.append(f"| {k} | {s.get('total', 0)} | {s.get('imported', 0)} | {skip} |")
    L += ["", f"## 3. 冲突候选：{len(cs)} 组", ""]
    if not cs:
        L.append("没有发现同主语同谓词不同值的 active 主张。")
    for i, g in enumerate(cs[:limit], 1):
        tag = "高风险" if g["hard"] else "叙述型"
        L += [f"### 3.{i} {g['subject']} · {g['predicate']}（{tag}）", ""]
        for c in g["ranked"][:6]:
            L.append(f"- `{c['id']}` **{c['status']}** / {c['evidence']} / "
                     f"{c['valid_from'] or '无日期'} / {c['origin']}：{c['value'][:160]}")
        if len(g["ranked"]) > 6:
            L.append(f"- ……另有 {len(g['ranked']) - 6} 条")
        L.append("")
    if len(cs) > limit:
        L += [f"（只列前 {limit} 组，共 {len(cs)} 组；`mem-ledger.py report --all` 可全量。）", ""]

    L += [f"## 4. 端口主张核对：{len(pidx)} 个端口，{len(pconf)} 个端口被**不同主体**认领", "",
          "> 同一端口被多个模型共享是正常设计（如 8091 的 ComfyUI 一实例多模型），"
          "本节已把别名/同 unit 合并；剩下的才是需要人看一眼的。", ""]
    if not pconf:
        L.append("没有同一端口被两个不同主体认领的情况。")
    for x in pconf[:30]:
        L.append(f"### 端口 {x['port']}（{len(x['clusters'])} 个主体）")
        for cl in x["clusters"]:
            L.append(f"- **{' / '.join(cl)}**")
        L.append("")
    L += ["", f"## 5. 路径主张体检：{ptotal} 条未裁决绝对路径，{pmiss} 条当前不存在", "",
          "> 「不存在」不等于错误：可能是外接盘未挂载、按需文件已清理、或当时的临时产物。"
          "这类主张用前必须复核，不能直接当现状。已在 `overrides.jsonl` 裁决为 "
          "rejected/stale/superseded 的条目不在这里重复出现。", ""]
    if prows:
        L += ["| 主张 | 状态 | 来源 | 日期 |", "|---|---|---|---|"]
        for c in prows:
            L.append(f"| {c['subject'] or c['value'][:40]} → `{c['value'][:80]}` | 不存在 "
                     f"| {c['origin']} | {c['valid_from'] or '—'} |")
    L += ["", "## 6. 跨源互证（≥2 个独立来源 → 升 verified）", ""]
    groups: dict[tuple, list[dict]] = {}
    for c in claims:
        if c["subject"] and c["predicate"] and c["status"] in ACTIVE:
            groups.setdefault((_norm(c["subject"]), c["predicate"].lower(),
                               norm_value(c["predicate"], c["value"])), []).append(c)
    proven = [(k, v) for k, v in groups.items()
              if len({x["origin"].split(":")[0] for x in v}) >= 2]
    if not proven:
        L.append("没有跨源互证的主张。")
    else:
        L += ["| 主体 · 谓词 | 值 | 来源 |", "|---|---|---|"]
        for (subj, pred, val), v in sorted(proven):
            srcs = sorted({origin_system(x["origin"]) for x in v})
            L.append(f"| {subj} · {pred} | `{val}` | {' + '.join(srcs)} |")
        L += ["", f"共 **{len(proven)}** 组。这是目前唯一有独立证据的事实类别，"
                  "其余仍是单源 proposed。", ""]
    L += ["## 7. 已知局限", "",
          "1. 灵枢与 Claude 侧没有结构化三元组，全部记为「无主语」，不参与冲突裁决——"
          "它们的互证数为 0，需要 P1 补抽取，不能靠本报告下结论。",
          "2. MemT 的 `topic.value` 常是叙述句而非可比值，冲突组里混有「不同说法」而非「真矛盾」。",
          "3. 报告不判断谁对：只看证据等级和日期，最终裁决走 `overrides.jsonl`。", ""]
    L += _ttl_section(claims, cfg)
    L += _probe_section(cfg)
    L += _dream_section(claims, cfg, newly_recorded=new_proposals)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    return len(cs), structured
