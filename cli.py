#!/usr/bin/env python3
"""记忆系统统一入口。

    mem build   [--report] [--strict]     重建账本（写路径唯一提交者）
    mem search  <问题> [-k 8] [--as AGENT] 检索（影子实现，只读）
    mem eval    --out FILE [--qs 题库]      让检索器吐预测，供评测闸门打分
    mem stats                               账本统计
    mem query   <关键词>                     子串查（不是检索，只是翻）
    mem gaps                                三张缺口清单
    mem report  [--out FILE] [--all]        冲突报告
    mem rules   <verify|conflicts|promote|list>
    mem verify  <id> --basis …              记一条裁决（人做）
    mem supersede <old> --by <new> --reason …
    mem reject  <id> --why …                这条主张被判定为错的
    mem stale   <id> --why …                曾经对、现在无法确认
    mem chain   [id] [--strict]             取代链检查 / 查一条链
    mem propose --claim … [--as AGENT] [--visibility V]
    mem config  [--check]                   看当前配置
    mem api     [--port 18092] [--tokens FILE]  只读 HTTP API（别的 agent 查账本）
    mem self-test                           全部自检

**所有命令都从 `memlib/config.py` 读路径**，没有硬编码。
`--config` 可以指定另一份 config.toml（测试或多实例用）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memlib import build as _build, config as _config, report as _report  # noqa: E402
from memlib import search as _search, store as _store                      # noqa: E402
from memlib.schema import EVIDENCE_RANK, norm as _norm, norm_date           # noqa: E402
from memlib.scope import MEMORY_ADMIN, valid_agent_id, valid_visibility    # noqa: E402

CFG: _config.Config | None = None


def cfg() -> _config.Config:
    global CFG
    if CFG is None:
        CFG = _config.load(getattr(cfg, "path", None))
    return CFG


# ---------- 命令 ----------

def cmd_build(a) -> int:
    return _build.build(cfg(), with_report=a.report, strict=a.strict)


def cmd_search(a) -> int:
    cf = _search.SearchConf(cfg())
    docs = _search.load_all(cf, not a.no_rules)
    if getattr(a, "as_of", ""):
        day = norm_date(a.as_of)
        if not day:
            print(f"--as-of 认不出：{a.as_of!r}（要 YYYY-MM-DD）", file=sys.stderr)
            return 2
        docs, st = _search.as_of_filter(docs, day)
        print("截至 {0}：保留 {1} 条（尚未成立 {2} / 已失效 {3} / 当时已作废 {4} / "
              "日期不明 {5}）".format(day, st["保留"], st["排除_尚未成立"],
                                    st["排除_已经失效"], st["排除_当时已作废"],
                                    st["日期不明"]), file=sys.stderr)
        if st["日期不明"]:
            print("  其中「日期不明」= 没有 valid_from 也没状态变更时间，"
                  "**没法判断那天成不成立**，按保留处理——别把它当成证据。",
                  file=sys.stderr)
    idx = _search.Index(docs, cf, a.as_agent)
    if idx.dropped_scope:
        print(f"（作用域过滤掉 {idx.dropped_scope} 条，reader={a.as_agent}）", file=sys.stderr)
    res = _search.search(idx, " ".join(a.words), a.k)
    if not res:
        print("（无结果）")
        return 0
    for c, s in res:
        print(f"{s:.3f}  {_search.fmt(c)[:300]}")
    return 0


def cmd_eval(a) -> int:
    cf = _search.SearchConf(cfg())
    docs = _search.load_all(cf, not a.no_rules, not a.no_heldout)
    idx = _search.Index(docs, cf, a.as_agent)
    print(f"索引 {len(idx.docs)} 条", file=sys.stderr)
    qfile = Path(a.qs) if a.qs else Path(cfg().path_of("evaluation")) / "questions.jsonl"
    out = Path(a.out)
    lines = []
    for line in qfile.read_text().splitlines():
        if not line.strip():
            continue
        q = json.loads(line)
        res = _search.search(idx, q["question"], a.k)
        lines.append(json.dumps({
            "id": q["id"],
            "answers": [{"text": _search.fmt(c), "claim_id": c["id"],
                         # 合并同一事实后的其他成员 id。判分时要一并认作命中：
                         # 合并没丢信息，只是把四条说同一件事的输成一行；
                         # 若只认首个 id，目标恰好是第二个来源时会被判成漏答——
                         # 那是**合并的副作用**，不是检索退步。
                         "also_ids": [x["id"] for x in (c.get("also_from") or [])],
                         "origin": c.get("layer") or "claims",
                         "evidence": c["evidence"]} for c, _ in res],
        }, ensure_ascii=False))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {len(lines)} 题预测 -> {out}")
    return 0


def cmd_stats(a) -> int:
    claims = _build.read_claims(cfg())
    print(f"账本 {len(claims)} 条")
    print("来源：", dict(Counter(c["origin"].split(":")[0] for c in claims)))
    print("状态：", dict(Counter(c["status"] for c in claims)))
    print("证据：", dict(Counter(c["evidence"] for c in claims)))
    print("作用域：", dict(Counter(c["scope"] for c in claims)))
    print(f"结构化：{sum(1 for c in claims if c['subject'] and c['predicate'])} 条")
    return 0


def cmd_shard(a) -> int:
    """把「结案且够老」的条目挪进年度分片。默认 dry-run，`--apply` 还要 `--yes`。

    为什么默认不动手：这是在挪主账本的行。虽然分片仍被检索读到、也能并回去，
    但「一条主张今天还在主文件、明天不见了」必须是人点头的动作，不能是每天 04:20
    顺手做的事。判据：`status` 不在 ACTIVE 里（rejected/superseded/stale），
    且 `captured_at` 早于阈值天数——`proposed` 一条都不动，那是还没裁决的工作量。
    """
    from datetime import timedelta

    from memlib import atomic as _atomic
    from memlib import schema as _schema
    from memlib.store import read_jsonl, shard_files

    ledger = cfg().path_of("ledger")
    claims_path = cfg().path_of("claims")
    rows = read_jsonl(claims_path)
    cutoff = datetime.now() - timedelta(days=a.days)

    def age_ok(c) -> bool:
        ts = str(c.get("captured_at") or c.get("valid_from") or "")[:10]
        try:
            return datetime.fromisoformat(ts) < cutoff
        except Exception:                     # noqa: BLE001
            return False                       # 日期不认识就不动它

    moved = [c for c in rows
             if c.get("status") not in _schema.ACTIVE and age_ok(c)]
    by_year: dict[str, list[dict]] = {}
    for c in moved:
        y = str(c.get("captured_at") or c.get("valid_from") or "")[:4] or "unknown"
        by_year.setdefault(y, []).append(c)

    print(f"主账本 {len(rows)} 条，其中结案且早于 {a.days} 天：{len(moved)} 条"
          f"（阈值 {cutoff:%Y-%m-%d}）")
    for y, cs in sorted(by_year.items()):
        print(f"  {y}: {len(cs)} 条 -> claims-archive-{y}.jsonl"
              f"  状态 {dict(Counter(c['status'] for c in cs))}")
    if not moved:
        print("没有可挪的。现有条目的 capture 时间都还在阈值内——这不是错误。")
        return 0
    print("模式：" + ("APPLY（先写分片，再原子替换主账本）" if a.apply else "dry-run（只打印）"))
    if not a.apply:
        print("\n未动任何文件。确认后加 --apply --yes。")
        return 0
    if not a.yes:
        print("\n--apply 需要 --yes。回滚：把分片并回主账本再 build。")
        return 1

    ids = {c["id"] for c in moved}
    for y, cs in sorted(by_year.items()):
        p = ledger / f"claims-archive-{y}.jsonl"
        # 只追加：重复写同一行由读取端按 id 去重
        _atomic.append_jsonl(p, cs)
        print(f"  写入分片 {p}（{len(cs)} 条）")

    kept = [c for c in rows if c["id"] not in ids]
    _atomic.write_jsonl(claims_path, kept)
    print(f"主账本 {len(rows)} -> {len(kept)} 条（挪走 {len(moved)} 条，"
          f"分片文件 {len(shard_files(ledger))} 个，检索仍能查到）")
    return 0


def cmd_query(a) -> int:
    q = _norm(" ".join(a.words))
    if not q:
        print("给个关键词")
        return 2
    hits = []
    for c in _build.read_claims(cfg()):
        blob = _norm(" ".join([c["subject"], c["predicate"], c["value"], c["claim"], c["scope"]]))
        if q in blob:
            hits.append(c)
    hits.sort(key=lambda c: (c["status"] != "verified", -EVIDENCE_RANK.get(c["evidence"], 0)))
    for c in hits[:30]:
        print(f"[{c['status']}/{c['evidence']}] {c['id']} {c['scope']} :: "
              f"{(c['subject'] + ' · ' + c['predicate']) if c['subject'] else ''} {c['value'][:120]}")
    print(f"\n{len(hits)} 条")
    return 0


def cmd_gaps(a) -> int:
    claims = _build.read_claims(cfg())
    if not claims:
        print("账本为空，先跑 build", file=sys.stderr)
        return 1
    g = _store.gaps(claims)
    out = cfg().path_of("reports") / f"缺口清单-{date.today().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    L = [f"# 记忆缺口清单（{date.today().isoformat()}）", "",
         "> 由 `mem gaps` 生成。这些清单都是**事实**，不是建议：",
         "> 它们描述账本现在缺什么，怎么补要人判断。", "",
         "> 前三条是**该修**的；最后一条是**已知限制**（导入器无法记录身份），",
         "> 分开列是因为那条的条数远大于真缺口，混在一起会把它们淹掉。", ""]
    for name, rows in g.items():
        L += [f"## {name}：{len(rows)} 条", ""]
        if name == "身份为推导":
            # 2123 条逐条列没有信息量，按来源汇总即可。
            if rows:
                L += ["这些条目来自定时抽取，不是「某个 Agent 在写」，身份只能从 origin 反推。",
                      "分层办法 §7 P0 已明确：硬编一个身份是假的，所以这是设计，不是缺陷。", "",
                      "| 来源 | 条数 |", "|---|---|"]
                for k, v in Counter(x.get("origin", "").split(":")[0]
                                    for x in rows).most_common():
                    L.append(f"| {k} | {v} |")
            L.append("")
            continue
        if not rows:
            L.append("无。")
            L.append("")
            continue
        L += ["| id | 主体 | 值 | 来源 |", "|---|---|---|---|"]
        for c in rows[:a.limit]:
            L.append(f"| `{c['id']}` | {c.get('subject') or '—'} | "
                     f"{(c.get('value') or '')[:60]} | {c.get('origin')} |")
        if len(rows) > a.limit:
            L.append(f"| …… | 另有 {len(rows) - a.limit} 条 | | |")
        L.append("")
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"缺口清单 -> {out}")
    for k, v in g.items():
        print(f"  {k:<8} {len(v):>5} 条")
    return 0


def cmd_ttl(a) -> int:
    """复核周期：哪些主张该重新核实了（分层办法 §5）。

    **默认只报告**：列出已过期 / 将到期 / 日期不明，不自动改状态。
    写操作要人显式打开 `[ttl.meta] auto_mark`——因为 `stale` 意味着
    检索时不再出现，机器不该替人决定撤下哪些内容。
    """
    from memlib import ttl as _ttl
    claims = _build.read_claims(cfg())
    if not claims:
        print("账本为空，先跑 build", file=sys.stderr)
        return 1
    tm = cfg().ttl()
    if not tm:
        print("配置里没有 [ttl] 段，没有谓词参与复核。", file=sys.stderr)
        return 0
    derived = cfg().ttl_derived_origins()
    d = _ttl.due(claims, tm, derived_prefixes=derived)
    print("复核周期检查（谓词类型 → 天数）：")
    for p, n in sorted(tm.items()):
        print(f"  {p:<8} {n:>4} 天")
    print()
    for k, rows in d.items():
        print(f"  {k:<6} {len(rows):>4} 条")
    if d["已过期"]:
        print("\n已过期（最久没核实的在前）：")
        for x in d["已过期"][:a.limit]:
            c = x["claim"]
            print(f"  {c['id']} {x['predicate']} 超期 {x['over']} 天 | "
                  f"{(c.get('value') or '')[:50]}")
            print(f"     复核后：`mem verify {c['id']} --basis …` / "
                  f"`mem stale {c['id']} --why …`")
    if d["将到期"]:
        print("\n快到期（还剩 1/4 周期以内）：")
        for x in d["将到期"][:a.limit]:
            c = x["claim"]
            print(f"  {c['id']} {x['predicate']} 还剩 {x['left']} 天 | "
                  f"{(c.get('value') or '')[:50]}")
    if d["日期不明"]:
        print("\n日期不明（有复核周期但算不出日期，**没法判断**，别当成没问题）：")
        for x in d["日期不明"][:a.limit]:
            c = x["claim"]
            print(f"  {c['id']} {x['predicate']} {c.get('origin')} "
                  f"valid_from={c.get('valid_from')!r}")
    if d["实时派生"]:
        who = {}
        for x in d["实时派生"]:
            who[x["claim"].get("origin", "")] = who.get(x["claim"].get("origin", ""), 0) + 1
        print(f"\n实时派生（{len(d['实时派生'])} 条，TTL 不适用）："
              + "、".join(f"{k} {n}" for k, n in sorted(who.items())))
        print("  这些值每次 build 从配置/命令输出重读，重读就是复核。"
              "它们证明「配置这么写」，不证明「机器实际如此」——后者跑 `mem probe`。")
    if a.mark:
        if not cfg().ttl_auto_mark():
            print("\n[ttl.meta] auto_mark = false，未写任何裁决。"
                  "要自动标 stale 先把配置改成 true。", file=sys.stderr)
            return 2
        n = 0
        for x in d["已过期"]:
            c = x["claim"]
            _append_override({"action": "stale", "target": c["id"],
                              "basis": f"复核周期到期：{x['predicate']} 超期 {x['over']} 天",
                              "actor": MEMORY_ADMIN})
            n += 1
        print(f"\n已追加 {n} 条 stale 裁决；跑 build 生效。")
    return 0


def cmd_dream(a) -> int:
    """记忆整合：把报告里的候选变成**待你点头的裁决草案**（2026-11-16）。

    **一条都不自动写。** 本机的规矩是「Agent 只许 propose」，
    整合自动应用裁决等于把这条作废；而且分不清 `overrides.jsonl` 里
    哪条是人判的、哪条是脚本猜的。

    参考的三个项目（`agent-memory` 的 T1 提案层 / `MemoryOS` 的分层 /
    `mem0` 的操作集）里，只抄了「把复核成本降下来」这一条，
    没抄「让模型在写入路径上判该删什么」。
    """
    from memlib import dream as _dream
    claims = _build.read_claims(cfg())
    if not claims:
        print("账本为空，先跑 build", file=sys.stderr)
        return 1
    path = cfg().path_of("proposals")

    if a.accept or a.reject:
        pid = a.accept or a.reject
        p = _dream.by_id(path, pid)
        if not p:
            print(f"没有这条提案：{pid}（先跑 `mem dream --record`）", file=sys.stderr)
            return 2
        if p.get("status") != "pending":
            print(f"{pid} 已经是 {p.get('status')}，不重复处理。", file=sys.stderr)
            return 2
        if a.reject:
            why = (a.why or "").strip()
            if not why:
                print("--reject 必须写 --why：一条被否掉的提案若没写为什么，"
                      "下次整合还会再提一遍。", file=sys.stderr)
                return 2
            _dream.mark(path, pid, "rejected", why)
            print(f"已记为 rejected：{pid}（{why}）")
            return 0
        act = p["action"]
        if act == "supersede":
            print("supersede 提案不能一键接受：它必须带 --by 指向真实后任。\n"
                  f"  → 先 `mem propose` 写入现行值，再 `mem supersede {p['target']} --by <新 id>`",
                  file=sys.stderr)
            return 2
        _append_override({"action": act, "target": p["target"], "basis": p.get("why", ""),
                          "evidence": "user" if act == "verify" else "inferred",
                          "actor": MEMORY_ADMIN})
        _dream.mark(path, pid, "accepted", "由 me 接受")
        print(f"已追加 {act} 裁决：{p['target']}；跑 build 生效。")
        return 0

    ps = _dream.pending(claims, path)
    if a.record:
        n = _dream.record(path, ps)
        print(f"已记录 {n} 条新提案 -> {path}（其余 {len(ps) - n} 条此前已记录）")
        return 0
    if not ps:
        print("没有待处理的整合提案。")
        return 0
    print(f"待处理整合提案 {len(ps)} 条（**没有任何一条被自动应用**）：")
    for p in ps[:a.limit]:
        print(f"\n  {p['id']}  [{p['kind']}] → {p['action']} {p['target']}")
        print(f"    {p['why']}")
        if p.get("basis"):
            print(f"    依据：{p['basis']}")
    print(f"\n接受：`mem dream --accept <id>`   否掉：`mem dream --reject <id> --why …`")
    print("先 `mem dream --record` 落盘，才能 accept/reject（提案也要留痕）。")
    return 0


def cmd_probe(a) -> int:
    """探活：配置里说 `state: up` 的模型，端口上真有东西吗（2026-11-16）。

    这是 TTL 管不到的那一半：`registry:*` 的值每次 build 从 `models.yaml` 重读，
    重读只证明「配置这么写」。配置写 `up`、进程早死了，构建一辈子发现不了。

    **只读**：TCP 连一下 + 一条 `GET /props`，不启动不重启不写裁决。
    判定按 `state` 分档（见 `memlib/probe.py` 的表），不是「端口没人就是错」——
    65 个模型里只有十几个常驻，不分档的话每天 40 条红字，等于没有检查。
    """
    try:
        from memlib import probe as _probe
    except ImportError:
        print("本发行版没有 memlib/probe.py（探活是可选组件，绑死 models.yaml 契约）。",
              file=sys.stderr)
        return 2
    d = _probe.run(cfg())
    order = ("在线", "配置说常驻但没起", "在跑但配置说没起", "上下文不符",
             "按需/下线（符合配置）")
    print("探活（配置 → 实际）：")
    for k in order:
        print(f"  {k:<16} {len(d.get(k, [])):>3} 个")
    bad = d.get("配置说常驻但没起") or []
    if bad and not a.quiet:
        print("\n配置说常驻、端口上没人（这才是要动手的）：")
        for x in bad[:a.limit]:
            svc = f" / {x['service']}" if x.get("service") else ""
            print(f"  {x['model']:<24} :{x['port']}{svc}")
        print("  → 起服务：`systemctl --user start <unit>`；"
              "确实下线了就改 models.yaml 的 `state`。")
    drift = d.get("上下文不符") or []
    if drift and not a.quiet:
        print("\n上下文不符（配置 vs 实际 n_ctx）：")
        for x in drift[:a.limit]:
            print(f"  {x['model']:<24} :{x['port']} 配置 {x['配置']} / 实际 {x['实际']}")
    weird = d.get("在跑但配置说没起") or []
    if weird and not a.quiet:
        print("\n在跑但配置说没起（可能刚起，也可能配置该更新）：")
        for x in weird[:a.limit]:
            print(f"  {x['model']:<24} :{x['port']} state={x['state'] or '未写'}")
    # 和 `mem chain` / `mem ttl` 同一个规矩：报告不是闸门。
    # 配置与实际不一致要人决定是起服务还是改配置，挂成失败只会天天红。
    return 0


def cmd_chain(a) -> int:
    """取代链的检查与查询（2026-11-15）。

    `superseded_by` 以前是只写不读的字段：记下了「被谁取代」，却没有任何代码
    回答过这个问题。而分层办法 §8 的验收第 1 条明确要求能答出来。
    一个只写不读的字段不是记录，是装饰。
    """
    claims = _build.read_claims(cfg())
    if not claims:
        print("账本为空，先跑 build", file=sys.stderr)
        return 1

    if a.id:
        path = _store.chain_of(claims, a.id)
        by_id = {c["id"]: c for c in claims}
        print(f"取代链：{' -> '.join(path)}")
        for cid in path:
            c = by_id.get(cid)
            if not c:
                print(f"  {cid}（不在 claims 里，可能是规矩 id）")
                continue
            hist = c.get("state_history") or []
            why = next((h.get("basis") for h in reversed(hist) if h.get("basis")), "")
            print(f"  {cid} [{c['status']}] {c.get('subject') or c.get('claim', '')[:40]}")
            if why:
                print(f"    因为：{why[:100]}")
        return 0

    rule_ids = set()
    rp = cfg().path_of("rules")
    if rp.exists():
        for ln in rp.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                rule_ids.add(json.loads(ln)["id"])
    p = _store.chain_problems(claims, rule_ids)
    print("取代链检查：")
    for k, v in p.items():
        print(f"  {k:<8} {len(v):>4} 条")
    # 「多重活跃」是设计冲突的显影，不是数据坏了；不计入失败。
    bad = sum(len(v) for k, v in p.items() if k != "多重活跃")
    if p["缺后任"]:
        print("\n缺后任（superseded 但没写被谁取代，答案只能是散文）：")
        for c in p["缺后任"][:10]:
            ref = c.get("state_history") or [{}]
            print(f"  {c['id']} {c.get('subject') or ''} —— {str(ref[-1].get('basis'))[:60]}")
    if p["指向不存在"]:
        print("\n指向不存在的 id：")
        for c in p["指向不存在"][:10]:
            print(f"  {c['id']} -> {c.get('superseded_by')}")
    if p["成环"]:
        print("\n成环：")
        for c in p["成环"][:10]:
            print(f"  {c['id']} -> {c.get('superseded_by')}")
    if p["残留旧值"]:
        print("\n残留旧值（已判死值仍被其他 active 条目断言）：")
        for x in p["残留旧值"][:15]:
            print(f"  {x['id']} [{x['status']}] 仍说 {x['dead_value'][:60]}"
                  f"\n      （源于已作废的 {x['superseded_id']}）")
    print(f"\n链本身的问题 {bad} 条。"
          "「多重活跃」单列——那是跨源互证**本来就会**产生的多来源条目（§8 已在 2026-11-16 "
          "改成与之匹配的措辞），不是数据坏了；检索时已在 `merge_same_fact` 里合并且保留来源，"
          "所以账本里这 8 组照旧存在是**预期**的。见 `memlib/store.py: chain_problems`。")
    # 默认像 `mem gaps` 一样返回 0：这是**报告**，不是闸门。
    # 挂进每日重建的检查必须「平时是绿的」，否则它会天天红，
    # 红到没人再看——那等于没检查。--strict 留给手动把关用。
    return (1 if bad else 0) if a.strict else 0


def cmd_report(a) -> int:
    claims = _build.read_claims(cfg())
    if not claims:
        print("账本为空，先跑 build。")
        return 2
    default_dir = cfg().path_of("reports")
    out = Path(a.out) if a.out else default_dir / f"冲突报告-{date.today().isoformat()}.md"
    nc, structured = _report.write_report(claims, {}, out,
                                          limit=100000 if a.all else 80)
    print(f"{nc} 组冲突，结构化 {structured} 条 -> {out}")
    return 0


def cmd_rules(a) -> int:
    import importlib.util
    p = Path(__file__).resolve().parent / "bin" / "rules.py"
    if not p.exists():
        print(f"规矩工具不存在：{p}（命令 {a.rules_cmd} 需要它）", file=sys.stderr)
        return 2
    spec = importlib.util.spec_from_file_location("rules", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main([str(p), a.rules_cmd] + list(a.rest))


def _append_override(o: dict) -> None:
    ledger = cfg().path_of("ledger")
    ledger.mkdir(parents=True, exist_ok=True)
    o.setdefault("at", datetime.now().isoformat(timespec="seconds"))
    o.setdefault("actor", "me")
    with cfg().path_of("overrides").open("a", encoding="utf-8") as f:
        f.write(json.dumps(o, ensure_ascii=False) + "\n")


def cmd_verify(a) -> int:
    # 复核即「今天确认过」（2026-11-16）：verify 顺带盖 `valid_from`，
    # TTL 的「日期不明」才有出口——否则那些条目只能永远报「没法判断」。
    # 默认今天，`--valid-from` 可回溯（"我 11-10 核过，今天补记"）。
    vf = (getattr(a, "valid_from", "") or "").strip() or date.today().isoformat()
    nf = norm_date(vf)
    if not nf:
        print(f"--valid-from 认不出：{vf!r}（要 YYYY-MM-DD）", file=sys.stderr)
        return 2
    _append_override({"action": "verify", "target": a.id, "valid_from": nf,
                      "basis": a.basis, "evidence": a.evidence})
    print(f"已记裁决：verify {a.id}（自 {nf} 起成立）；跑 build 生效。")
    return 0


def cmd_supersede(a) -> int:
    """记一条取代裁决。

    `--by` 允许写 claim id、规矩 id，或一句解释（旧数据里就有「见 models.yaml
    file 字段」这种）。但**如果它看起来像 id，就必须真的存在**——写成
    `cl_xxx` 却打错一个字符，`superseded_by` 就永久悬空，
    而 `mem chain` 也只能替你记下这个问题，答不出「被谁取代」。
    """
    by = (a.by or "").strip()
    if not by:
        print("supersede 必须写 --by：后任的 claim id / 规矩 id / 或一句说明。",
              file=sys.stderr)
        return 2
    if re.match(r"^(cl|rule)_", by):
        known = set()
        cl = cfg().path_of("claims")
        if cl.exists():
            for ln in cl.read_text(encoding="utf-8", errors="ignore").splitlines():
                if ln.strip():
                    try:
                        known.add(json.loads(ln)["id"])
                    except json.JSONDecodeError:
                        pass
        rp = cfg().path_of("rules")
        if rp.exists():
            for ln in rp.read_text(encoding="utf-8", errors="ignore").splitlines():
                if ln.strip():
                    try:
                        known.add(json.loads(ln)["id"])
                    except json.JSONDecodeError:
                        pass
        if known and by not in known:
            print(f"--by {by} 在 claims 和 rules 里都不存在。"
                  "要么 id 写错了，要么后任还没进账本——悬空的后任比没有后任更难查。",
                  file=sys.stderr)
            return 2
    _append_override({"action": "supersede", "target": a.old, "by": by,
                      "basis": a.reason})
    print(f"已记裁决：supersede {a.old} by {by}；跑 build 生效。")
    return 0


def _state_change(action: str, target: str, why: str, as_agent: str, **extra) -> int:
    """`reject` / `stale` 的共同入口。

    两者都是**状态变更，不删条目**（总则 §「不用删解决矛盾」）。区别在语义：

      - `reject`  这条主张被判定为**错的**（证据推翻它）
      - `stale`   这条主张曾经对，**现在无法确认**（路径没了、版本变了、端口不通）

    分开是因为处理方式不同：rejected 不该再被检索到，stale 该被复核。
    合成一个「无效」会丢掉这个区别。

    `--why` 必填：一条被否掉的主张如果没写为什么被否，下一个人只能重新怀疑一遍。
    """
    target = (target or "").strip()
    if not target:
        print(f"{action} 需要目标 claim id（先用 `mem search` 或 `mem query` 找到它）",
              file=sys.stderr)
        return 2
    why = (why or "").strip()
    if not why:
        print(f"{action} 必须写 --why：为什么不成立 / 为什么没法确认。"
              "没有理由的裁决等于下一个人要重做一遍判断。", file=sys.stderr)
        return 2
    aid = (as_agent or "").strip() or MEMORY_ADMIN
    # 先确认目标真的存在：往 overrides 里追加一个指向不存在 id 的裁决，
    # build 时只会打印一行「目标不存在，已跳过」——等于什么都没发生，却写进了
    # 只追加的日志，永远躺在那儿。宁可在这里失败。
    cl = cfg().path_of("claims")
    if cl.exists():
        found = False
        with cl.open(encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    if json.loads(ln).get("id") == target:
                        found = True
                        break
                except json.JSONDecodeError:
                    continue
        if not found:
            print(f"账本里没有 {target}。先 `mem build` 再查一遍 id 是否写错；"
                  "不存在的目标不写进裁决日志。", file=sys.stderr)
            return 2
    else:
        print(f"账本还不存在（{cl}），先跑 `mem build`。", file=sys.stderr)
        return 2
    _append_override({"action": action, "target": target,
                      "basis": why, "actor": aid, **extra})
    print(f"已记裁决：{action} {target}（actor={aid}）；跑 build 生效。")
    return 0


def cmd_reject(a) -> int:
    return _state_change("reject", a.id, a.why, a.as_agent)


def cmd_stale(a) -> int:
    return _state_change("stale", a.id, a.why, a.as_agent)


def cmd_propose(a) -> int:
    # 2026-11-15：写入端开始记录身份。不传 --as 时退回 `me`（人），
    # 但那样写出来的仍然是"推导不出来的身份"，所以默认会给个明显提醒。
    aid = (a.as_agent or "").strip()
    vis = (a.visibility or "").strip()
    if aid and not valid_agent_id(aid):
        print(f"agent_id 不合法：{aid!r}（形如 claude-code:main / dsh:tauri / me，"
              "不许空白、不超过两段）", file=sys.stderr)
        return 2
    if vis and not valid_visibility(vis):
        print(f"visibility 不合法：{vis!r}（private / shared / global / team:<名字>）",
              file=sys.stderr)
        return 2
    # §2：Agent 自己写不了 global（global 只由记忆管理员确认）。
    if aid and aid != MEMORY_ADMIN and vis == "global":
        print("Agent 不能直接把主张写成 global（§2：global 只由记忆管理员确认）。"
              "先写 shared 或 private，需要全局可见就走 verify 由人确认。", file=sys.stderr)
        return 2
    if not aid:
        aid = MEMORY_ADMIN
        print("提示：没传 --as，按记忆管理员 me 记账，但这条的 agent_basis 会记成 "
              "unrecorded——**没人记录过它到底是谁写的**，它会出现在 `mem gaps` "
              "的「身份未记录」里。Agent 写入请显式传 --as <agent_id>。",
              file=sys.stderr)
    # 日期（2026-11-16）：人工写入的主张**自己就有日期**——写下它的那天。
    # 不写这一项，TTL 对这些条目只能报「日期不明」，而它们恰恰是证据最强的一类
    # （人当场核过）。默认当天，可用 --valid-from 回溯。
    vf = (a.valid_from or "").strip() or date.today().isoformat()
    nf = norm_date(vf)
    if not nf:
        print(f"--valid-from 认不出：{vf!r}（要 YYYY-MM-DD / YYYY/MM/DD / 时间戳）",
              file=sys.stderr)
        return 2
    o = {"action": "propose", "claim": a.claim, "scope": a.scope,
         "subject": a.subject or "", "predicate": a.predicate or "",
         "value": a.value or a.claim, "evidence": a.evidence,
         "owner": a.owner or "me",
         "agent_id": aid, "valid_from": nf,
         "visibility": vis or cfg().identity("new_write_visibility")}
    _append_override(o)
    print(f"已追加 propose（agent={aid} visibility={o['visibility']} "
          f"valid_from={nf}）；跑 build 后进账本。")
    return 0


def cmd_config(a) -> int:
    c = cfg()
    print(f"配置文件：{c.path}")
    for k, v in c.paths().items():
        print(f"  paths.{k:<10} {v}")
    print("导入器：", ", ".join(c.importers()))
    print(f"检索：k1={c.search('k1')} b={c.search('b')} 重排={c.rerank_weights()}")
    if a.check:
        bad = c.check()
        print("配置自检：", "无问题" if not bad else "")
        for x in bad:
            print("  ✗", x)
        return 0 if not bad else 1
    return 0


def cmd_api(a) -> int:
    """只读 HTTP API（2026-11-16）。写端点**一个都没有**，理由见 `memlib/api.py` 头注释。"""
    from memlib import api as _api
    return _api.serve(cfg(), host=a.host, port=a.port, tokens_path=a.tokens,
                      quiet=a.quiet)


def cmd_guard(a) -> int:
    """脚本快照：`--update` 刷新基线，否则只比对。

    本发行版不带 `memlib/guard.py`（它绑死本机的脚本目录布局）。
    装了的话这个命令照常可用；没装就说清楚，别装作成功。
    """
    try:
        from memlib import guard as _guard
    except ImportError:
        print("本发行版没有 memlib/guard.py（脚本快照是可选组件）。",
              file=sys.stderr)
        return 2
    if a.update:
        return _guard.update(cfg())
    return 1 if _guard.check(cfg()) else 0


def cmd_self_test(a) -> int:
    """跑全部自检。任何一个不过就返回非 0。

    账本 58 项与检索 23 项在进程内跑（`memlib/selftest.py`），
    评测工具的自测仍各自是独立脚本——它们要验的是「另一份数据 + 另一条链路」，
    同进程跑反而会把两边的模块状态搅在一起。
    """
    import subprocess
    eval_bin = cfg().path_of("evaluation") / "bin"
    rc = 0

    from memlib import selftest as _st
    for label, fn in (("mem-ledger", _st.ledger), ("memory-search", _st.search)):
        try:
            bad = fn(cfg())
        except Exception as e:                                  # noqa: BLE001
            print(f"  ✗ {label:<24} 崩了：{e}")
            rc = 1
            continue
        if bad:
            rc = 1

    # 脚本快照：可选组件，本发行版不带就是跳过（不是"通过"）
    try:
        from memlib import guard as _guard
    except ImportError:
        print("  跳过（本发行版没有 memlib/guard.py）脚本快照")
    else:
        if _guard.self_test():      # 先验检查本身有效（含 3 条负向）
            rc = 1
        if _guard.check(cfg()):
            rc = 1

    # 评测工具是独立脚本，各自验另一条链路。发行版不带它们，缺就跳过并打出来。
    for name, args in (("gate.py", ["--selftest"]),
                       ("split-eval.py", ["--selftest"]),
                       ("test-scope-gate.py", []),
                       ("test-memredact.py", []),
                       ("test-state-cmds.py", [])):
        path = eval_bin / name
        if not path.exists():
            print(f"  跳过（不存在）{path}")
            continue
        r = subprocess.run([sys.executable, "-I", str(path)] + args,
                           capture_output=True, text=True)
        last = (r.stdout.strip().splitlines() or ["(无输出)"])[-1]
        print(f"  {'✓' if r.returncode == 0 else '✗'} {name:<24} {last}")
        rc = rc or (r.returncode != 0)
    return 1 if rc else 0


# ---------- 解析 ----------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mem", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="另一份 config.toml（默认仓库根的 config.toml）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build"); b.add_argument("--report", action="store_true")
    b.add_argument("--strict", action="store_true"); b.set_defaults(fn=cmd_build)

    s = sub.add_parser("search"); s.add_argument("words", nargs="+")
    s.add_argument("-k", "--k", dest="k", type=int, default=8)
    s.add_argument("--as", dest="as_agent", default="all")
    s.add_argument("--as-of", dest="as_of", default="",
                   help="按某天（YYYY-MM-DD）的账本状态检索：那天还没成立/已失效的不出现")
    s.add_argument("--no-rules", action="store_true"); s.set_defaults(fn=cmd_search)

    e = sub.add_parser("eval"); e.add_argument("--out", required=True)
    e.add_argument("--qs", default="")
    e.add_argument("-k", "--k", dest="k", type=int, default=8)   # 旧的 `--k` 也认
    e.add_argument("--as", dest="as_agent", default="all")
    e.add_argument("--no-rules", action="store_true")
    e.add_argument("--no-heldout", action="store_true"); e.set_defaults(fn=cmd_eval)

    st = sub.add_parser("stats"); st.set_defaults(fn=cmd_stats)
    sh = sub.add_parser("shard", help="结案且够老的条目挪进年度分片（默认 dry-run）")
    sh.add_argument("--days", type=int, default=30)
    sh.add_argument("--apply", action="store_true")
    sh.add_argument("--yes", action="store_true")
    sh.set_defaults(fn=cmd_shard)
    q = sub.add_parser("query"); q.add_argument("words", nargs="+"); q.set_defaults(fn=cmd_query)
    g = sub.add_parser("gaps"); g.add_argument("--limit", type=int, default=40)
    g.set_defaults(fn=cmd_gaps)
    rp = sub.add_parser("report"); rp.add_argument("--out"); rp.add_argument("--all", action="store_true")
    rp.set_defaults(fn=cmd_report)

    r = sub.add_parser("rules"); r.add_argument("rules_cmd")
    r.add_argument("rest", nargs=argparse.REMAINDER); r.set_defaults(fn=cmd_rules)

    v = sub.add_parser("verify"); v.add_argument("id"); v.add_argument("--basis", required=True)
    v.add_argument("--valid-from", default="", help="实际核实的日期，默认当天")
    v.add_argument("--evidence", default="user"); v.set_defaults(fn=cmd_verify)

    sp = sub.add_parser("supersede"); sp.add_argument("old"); sp.add_argument("--by", required=True)
    sp.add_argument("--reason", default=""); sp.set_defaults(fn=cmd_supersede)

    # 状态变更，不删条目。`--why` 必填，`--as` 记谁做的裁决。
    rj = sub.add_parser("reject", help="这条主张被判定为错的（证据推翻它）")
    rj.add_argument("id"); rj.add_argument("--why", required=True)
    rj.add_argument("--as", dest="as_agent", default=""); rj.set_defaults(fn=cmd_reject)

    st = sub.add_parser("stale", help="曾经对、现在无法确认（路径没了/版本变了/端口不通）")
    st.add_argument("id"); st.add_argument("--why", required=True)
    st.add_argument("--as", dest="as_agent", default=""); st.set_defaults(fn=cmd_stale)

    pr = sub.add_parser("propose"); pr.add_argument("--claim", required=True)
    pr.add_argument("--scope", default="global"); pr.add_argument("--subject")
    pr.add_argument("--predicate"); pr.add_argument("--value")
    pr.add_argument("--evidence", default="extracted"); pr.add_argument("--owner")
    pr.add_argument("--as", dest="as_agent", default="")
    pr.add_argument("--valid-from", default="",
                    help="这条事实自哪天起成立（YYYY-MM-DD），默认写当天")
    pr.add_argument("--visibility", default=""); pr.set_defaults(fn=cmd_propose)

    c = sub.add_parser("config"); c.add_argument("--check", action="store_true")
    c.set_defaults(fn=cmd_config)

    tt = sub.add_parser("ttl", help="复核周期：哪些主张该重新核实了")
    tt.add_argument("--limit", type=int, default=20)
    tt.add_argument("--mark", action="store_true",
                    help="把已过期的写成 stale 裁决（需 [ttl.meta] auto_mark=true）")
    tt.set_defaults(fn=cmd_ttl)

    dr = sub.add_parser("dream", help="记忆整合：从报告生成待你点头的裁决草案（不自动写）")
    dr.add_argument("--record", action="store_true", help="把待处理提案追加进 proposals.jsonl")
    dr.add_argument("--accept", default="", metavar="ID", help="接受一条提案，落成裁决")
    dr.add_argument("--reject", default="", metavar="ID", help="否掉一条提案（须 --why）")
    dr.add_argument("--why", default="")
    dr.add_argument("--limit", type=int, default=20)
    dr.set_defaults(fn=cmd_dream)

    pb = sub.add_parser("probe", help="探活：配置说常驻的模型，实际起没起")
    pb.add_argument("--limit", type=int, default=20)
    pb.add_argument("--quiet", action="store_true", help="只打汇总，不列明细")
    pb.set_defaults(fn=cmd_probe)

    ch = sub.add_parser("chain", help="取代链：不带参数做检查，带 id 查一条链")
    ch.add_argument("id", nargs="?", default="")
    ch.add_argument("--strict", action="store_true", help="有问题就返回非 0（默认只报告）")
    ch.set_defaults(fn=cmd_chain)

    gd = sub.add_parser("guard", help="脚本快照（覆盖式改写防护）")
    gd.add_argument("--update", action="store_true", help="确认无误后刷新基线")
    gd.set_defaults(fn=cmd_guard)

    api = sub.add_parser("api", help="只读 HTTP API：别的 agent 查账本，不需要 import memlib")
    api.add_argument("--host", default="127.0.0.1", help="只接受回环地址")
    api.add_argument("--port", type=int, default=18092)
    api.add_argument("--tokens", default="",
                     help="token 表（默认 ~/.config/mem-api/tokens.toml，**不在 ~/记忆 里**）")
    api.add_argument("--quiet", action="store_true")
    api.set_defaults(fn=cmd_api)

    sl = sub.add_parser("self-test"); sl.set_defaults(fn=cmd_self_test)

    a = ap.parse_args(argv[1:])
    if getattr(a, "config", None):
        cfg.path = a.config
    return a.fn(a)


def entry() -> int:
    """`pip install` 装出来的 `mem` 命令入口。

    控制台脚本调用**不带参数**，而 `main(argv)` 要的是含程序名的完整 argv。
    直接把 `cli:main` 当入口会在 `main() missing 1 required positional argument: 'argv'`
    上炸——装完第一次运行就撞，而且看着像打包问题，其实是签名对不上。
    """
    return main(sys.argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
