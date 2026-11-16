"""记忆系统自检。项数不写死在标题里——**以实际输出为准**。

## 这两套测试是从哪儿来的

2026-11-15 重构时，`mem-ledger.py` / `memory-search.py` 被改成了兼容壳，
两个文件里的自测随之从磁盘上消失（壳子里不放逻辑）。原文已从会话记录里
取回并逐条移植到这里，**测试名一条没改**，所以两边的项数还能对上：

    mem-ledger.py    selftest()       56 项（重构后 56 → 58：新增两条缺口口径断言）
    memory-search.py cmd_selftest()   23 项

改的是调用方式：旧版靠改模块全局变量（`CLAIMS`、`OVERRIDES`、`ALIASES`）
来指向临时文件，新版统一走 `memlib` 的函数参数。断言本身没动。

上面这两个数是**移植当时的原始项数，不是现在的项数**。之后每条实测缺陷都补了
断言，数字一直在涨（2026-11-17 本机配置下：账本 77、检索 102）。头部原来挂着
「58 项 + 23 项」，和实测对不上——文档说的和跑出来的不一致，正是这套系统自己
最忌讳的那类错，所以改成不写死。

**项数会随配置变**：systemd / docs / 同义词 / 探活几组断言在配置里没有对应来源时
整体跳过，并在输出里标出来。跳过的项不计入分子分母，也不计入失败——
但"跳过"不等于"通过"，两者必须分开说。

## 反例比正例重要

同义词合并错一次、可见性放行错一次，都不会当场报错，只会悄悄把结果弄脏。
所以凡是"不该发生"的，都单独有一条（`private 别人读不到`、`图片不并入 image`、
`过程记录不进账本`……）。一条永远为真的断言等于没写。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import config as _config
from . import scope as _scope
from . import search as _search
from . import store as _store
from . import text as _text
from .importers import docs as _docs
from .schema import claim_id as _claim_id, make_claim as _mk
from .store import (apply_overrides, annotate_identity, conflicts, corroborate,
                    gaps, origin_system)


class _FakeCfg:
    """systemd 导入器的最小配置桩：只给 claims / rules 两个路径。"""

    def __init__(self, claims: Path, rules: Path):
        self._p = {"claims": claims, "rules": rules}

    def path_of(self, k):
        return self._p[k]

    def importer(self, name):
        return {}


# ---------------------------------------------------------------- 账本 58 项

def ledger(cfg=None) -> int:
    ok, ng, skipped = 0, [], []

    def case(name, cond, got=""):
        nonlocal ok
        if cond:
            ok += 1
        else:
            ng.append(f"{name}（实际 {got!r}）")

    def skip(why: str, n: int = 1) -> None:
        """配置里没有这套来源 → 相关断言不适用。

        2026-11-17：自测原来硬依赖本机的 systemd 与 `~/记忆` 目录布局，
        换一台机器（容器、别的发行版、或只是没开 docs 导入器）必红。
        **跳过要打出来**，不能静默少测——那和「测试通过」是两回事。
        """
        skipped.append(f"{why}×{n}")

    cfg = cfg or _config.load()
    from .redact import redact, redact_self_test

    # 脱敏闸门：必须真的在拦，且规则是共享的那一份
    case("脱敏闸门在拦 sk-", redact_self_test())
    case("脱敏覆盖讯飞 ak-",
         "[已脱敏" in redact("API Key 为 " + "".join(["ak-"] + ["0123456789abcdef"] * 2)))
    case("脱敏不误伤任务 id",
         redact("task-5a0f1499-aaaa-bbbb") == "task-5a0f1499-aaaa-bbbb")

    # 别名：临时注入，测完还原（别把真别名表留在测试造的表上）。
    # 注入的范围要盖住后面整段抽取测试——不是图省事：`qwen30/:8085` 这条
    # 要抽出 `q30 port 8085`，靠的就是别名把主语归一；真别名表里没有这一项，
    # 拿掉注入这条测试测的就变成了「真表里有没有 qwen30」（原文也是这么写的）。
    saved_alias = dict(_text.ALIASES)
    try:
        _text.ALIASES.clear()
        _text.ALIASES.update({"q30": "q30", "qwen30": "q30", "qwen3-30b-a3b": "q30",
                              "llama-router": "l1"})
        case("别名 qwen30→q30", _text.canon_subj("qwen30") == "q30",
             _text.canon_subj("qwen30"))
        case("别名 service 去后缀", _text.canon_subj("llama-router.service") == "l1",
             _text.canon_subj("llama-router.service"))
        case("未知名原样返回", _text.canon_subj("某未知东西") == "某未知东西")

        d = _text.derive("服务实际 `-c` 小得多（q30 262144→实际 32768，glm47 131072）")
        case("抽 ctx 箭头", ("q30", "ctx", "32768") in d, d)
        case("无 ctx 语境不抽箭头", _text.derive("（q30 262144→实际 32768）") == [],
             _text.derive("（q30 262144→实际 32768）"))
        d = _text.derive("七单元（qwen30/:8085、qwen35-4b-q4/:8110）")
        case("抽紧凑端口", ("q30", "port", "8085") in d, d)
        d = _text.derive("桥位于 ~/Apps/team-core/bridge.py，监听 127.0.0.1:18091")
        case("抽 host:port", any(p == "port" and v == "18091" for _, p, v in d), d)
        case("IP 末段不误判端口",
             not any(v in ("127", "0", "1") for _, p, v in d if p == "port"), d)
        case("协议名不当主语", _text.derive("SOCKS5 指向 127.0.0.1:1080") == [],
             _text.derive("SOCKS5 指向 127.0.0.1:1080"))
        case("HTTP 状态码不当端口", _text.derive("chinaz 返回 403") == [],
             _text.derive("chinaz 返回 403"))
        case("ctx 小于 512 丢弃", _text.derive("q30 的 ctx 是 63") == [],
             _text.derive("q30 的 ctx 是 63"))

        case("端口归一 08085", _text.norm_value("port", "08085") == "8085",
             _text.norm_value("port", "08085"))
        case("ctx 64K→65536", _text.norm_value("ctx", "64K") == "65536",
             _text.norm_value("ctx", "64K"))
        case("路径去尾斜杠", _text.norm_value("path", "/a/b/") == "/a/b",
             _text.norm_value("path", "/a/b/"))
    finally:
        _text.ALIASES.clear()
        _text.ALIASES.update(saved_alias)

    base = dict(scope="global", subject="x", predicate="port", value="1",
                claim="x", origin="memt", source_refs=[])
    a = _mk(**base, owner="m")
    b = _mk(**{**base, "origin": "registry", "value": "1"})
    claims = [a, b]
    n = corroborate(claims)
    case("两源同值升 verified", n == 1 and all(c["status"] == "verified" for c in claims),
         (n, [c["status"] for c in claims]))
    c1 = _mk(**{**base, "value": "8085", "subject": "q30"})
    c2 = _mk(**{**base, "value": "8086", "subject": "q30", "origin": "registry"})
    cs = conflicts([c1, c2])
    case("同主谓不同值判冲突", len(cs) == 1 and cs[0]["hard"], cs)
    c3 = _mk(**{**base, "value": "8085", "subject": "q30", "origin": "claude"})
    case("同值不判冲突", conflicts([c1, c3]) == [], conflicts([c1, c3]))

    # 身份与可见性（2026-11-15 P0）
    legacy = cfg.identity("default_visibility")
    m1 = _mk(**{**base, "origin": "memt", "source_refs": [
        {"kind": "fact", "session": "s-1"}, {"kind": "fact", "session": "s-1"},
        {"kind": "fact", "session": "s-2"}]})
    m2 = _mk(**{**base, "origin": "manual", "source_refs": [{"kind": "manual", "at": "x"}]})
    m3 = _mk(**{**base, "origin": "docs", "source_refs": []})
    annotate_identity([m1, m2, m3], legacy)
    case("rule 变体归同一系统", origin_system("memt-rule:port") == "memt"
         and origin_system("lingshu") == "lingshu", origin_system("memt-rule:port"))
    # 同系统两个变体不构成跨源互证
    r1 = _mk(**{**base, "origin": "memt", "value": "9"})
    r2 = _mk(**{**base, "origin": "memt-rule:port", "value": "9"})
    case("同系统不互证", corroborate([r1, r2]) == 0, corroborate([r1, r2]))
    case("memt → memt:daily", m1["agent_id"] == "memt:daily", m1["agent_id"])
    case("存量身份标 derived", m1["agent_basis"] == "derived-origin", m1["agent_basis"])
    # 裸 manual（没写 agent_id）**不算 recorded**：没人记录过它，只是默认成 me。
    # 标 recorded 会把一个猜出来的身份显示成"已记录"，那是最难发现的形态。
    case("人裁无记录标 unrecorded",
         m2["agent_basis"] == "unrecorded" and m2["agent_id"] == "me",
         (m2["agent_basis"], m2["agent_id"]))
    case("session_ids 去重保序", m1["source_session_ids"] == ["s-1", "s-2"],
         m1["source_session_ids"])
    case("存量可见性 shared", m1["visibility"] == legacy, m1["visibility"])

    gg = gaps([m1, m2, m3])
    case("无出处只抓空 refs", [c["origin"] for c in gg["无出处"]] == ["docs"],
         [c["origin"] for c in gg["无出处"]])
    # 「越界」看的是 **visibility**，不是 scope（2026-11-15 修的真 bug）。
    # §2 的读写表把「人确认」限定在 visibility；scope 只是「关于什么」。
    # 旧实现查 scope，把 1633 条 visibility=shared 的导入事实全判成越界——
    # 那张清单一度 99% 是噪声。反例（scope=global 不算越界）才是这条的价值。
    scope_g = _mk(scope="global", subject="sg", predicate="q2", value="v",
                  origin="docs", source_refs=[{"kind": "doc", "path": "p"}])
    vis_g = _mk(scope="framework:dsh", subject="sg", predicate="q3", value="v",
                origin="memt", source_refs=[{"kind": "doc", "path": "p"}])
    me_g = _mk(scope="framework:dsh", subject="sg", predicate="q4", value="v",
               origin="manual", source_refs=[{"kind": "manual", "at": "x"}])
    annotate_identity([scope_g, vis_g, me_g], legacy)
    vis_g["visibility"] = "global"        # Agent 自己升成 global → 越界
    me_g["visibility"] = "global"         # 人写的 global → 不算越界
    g2 = gaps([scope_g, vis_g, me_g])
    case("作用域越界口径",
         [c["id"] for c in g2["作用域越界"]] == [vis_g["id"]],
         [(c["scope"], c["visibility"], c["agent_id"]) for c in g2["作用域越界"]])

    # 身份要分开看：导入器推导（设计如此，§7 P0 已写明硬编身份是假的）
    # 不能和「写入端漏记」混为一谈——混在一起时 2123 条已知限制会把真缺口淹掉。
    manual_lost = _mk(scope="g", subject="sx", predicate="q5", value="v",
                      origin="manual", source_refs=[{"kind": "manual", "at": "x"}])
    imported = _mk(scope="g", subject="sx", predicate="q6", value="v",
                   origin="memt", source_refs=[{"kind": "doc", "path": "p"}])
    annotate_identity([manual_lost, imported], legacy)
    manual_lost["agent_basis"] = ""       # 人工写入却没记身份 → 真缺口
    g3 = gaps([manual_lost, imported])
    case("身份：漏记与推导分开",
         [c["id"] for c in g3["身份未记录"]] == [manual_lost["id"]]
         and [c["id"] for c in g3["身份为推导"]] == [imported["id"]],
         ([c["id"] for c in g3["身份未记录"]],
          [c["id"] for c in g3["身份为推导"]]))

    # 写路径：身份合法性（2026-11-15）
    case("agent_id 合法 form", _scope.valid_agent_id("claude-code:main")
         and _scope.valid_agent_id("me") and _scope.valid_agent_id("team:video"))
    case("agent_id 拒绝空/空白/三段",
         not _scope.valid_agent_id("") and not _scope.valid_agent_id("a b")
         and not _scope.valid_agent_id("a:b:c"))
    case("visibility 合法", all(_scope.valid_visibility(v) for v in
                               ("private", "shared", "global", "team:video")))
    case("visibility 拒绝未知", not any(_scope.valid_visibility(v) for v in
                                       ("", "pubic", "team:", "team:a:b")))
    case("新写入默认 private",
         cfg.identity("new_write_visibility") == "private",
         cfg.identity("new_write_visibility"))

    # 写读共用同一份规则：三个模块必须引用同一个 scope 对象
    case("写读规则同一份",
         _store.scope is _scope and _search.scope is _scope)

    # 端到端：overrides 里的 propose 带 --as 后，身份是 recorded 且不被推导覆盖
    tmpdir = Path(tempfile.mkdtemp())
    ov = tmpdir / "o.jsonl"
    ov.write_text("\n".join([
        json.dumps({"action": "propose", "claim": "测试私有主张", "scope": "global",
                    "agent_id": "dsh:tauri", "visibility": "private",
                    "at": "2026-11-15T00:00:00", "actor": "dsh:tauri"},
                   ensure_ascii=False),
        json.dumps({"action": "propose", "claim": "测试非法身份", "scope": "global",
                    "agent_id": "bad agent", "visibility": "shared",
                    "at": "2026-11-15T00:00:01"}, ensure_ascii=False),
        json.dumps({"action": "propose", "claim": "测试非法可见性", "scope": "global",
                    "agent_id": "dsh:tauri", "visibility": "pubic",
                    "at": "2026-11-15T00:00:02"}, ensure_ascii=False),
        json.dumps({"action": "propose", "claim": "测试无身份", "scope": "global",
                    "visibility": "shared",
                    "at": "2026-11-15T00:00:03"}, ensure_ascii=False),
    ]) + "\n", encoding="utf-8")
    rows, lg = apply_overrides([], ov, legacy, cfg.identity("new_write_visibility"))
    annotate_identity(rows, legacy)
    got = {c["claim"]: c for c in rows}
    c = got.get("测试私有主张", {})
    case("propose 记下 agent_id", c.get("agent_id") == "dsh:tauri", c.get("agent_id"))
    case("propose 身份标 recorded", c.get("agent_basis") == "recorded", c.get("agent_basis"))
    case("propose 可见性生效", c.get("visibility") == "private", c.get("visibility"))
    case("非法 agent_id 被拒", "测试非法身份" not in got, list(got))
    case("非法 visibility 被拒", "测试非法可见性" not in got, list(got))
    case("拒绝有明确日志", any("不合法" in x for x in lg), lg)
    # 没写 --as 的那条：身份是**默认值**，不是记录值。
    # 旧实现让 annotate_identity 看到 origin=manual 就标 recorded，
    # 把一个猜出来的 me 显示成「已记录」——那正是「身份靠推导」最难发现的形态。
    case("没传 --as 不标 recorded",
         not got.get("测试无身份", {}).get("agent_basis") == "recorded",
         got.get("测试无身份", {}).get("agent_basis"))
    case("写入私有后别人读不到", not _scope.visible(c, "claude-code:main"))
    case("写入私有后本人读得到", _scope.visible(c, "dsh:tauri"))

    # reject / stale（2026-11-15 补 CLI）：两者都是**状态变更，不删条目**。
    # 分开是因为语义不同：rejected 是「这条是错的」，stale 是「这条没法确认了」；
    # 合成一个「无效」会丢掉「该不该再复核」这个区别。
    tgt = _mk(scope="g", subject="rj", predicate="p", value="v",
              origin="manual", source_refs=[{"kind": "manual", "at": "t"}])
    tgt2 = _mk(scope="g", subject="st", predicate="p", value="v",
               origin="manual", source_refs=[{"kind": "manual", "at": "t"}])
    ov2 = tmpdir / "o2.jsonl"
    ov2.write_text("\n".join([
        json.dumps({"action": "reject", "target": tgt["id"],
                    "basis": "实测端口不通", "actor": "me"}, ensure_ascii=False),
        json.dumps({"action": "stale", "target": tgt2["id"],
                    "basis": "路径已不存在", "actor": "me"}, ensure_ascii=False),
        json.dumps({"action": "reject", "target": "cl_不存在",
                    "basis": "手滑", "actor": "me"}, ensure_ascii=False),
    ]) + "\n", encoding="utf-8")
    rows2, lg2 = apply_overrides([tgt, tgt2], ov2, legacy,
                                 cfg.identity("new_write_visibility"))
    byid = {r["id"]: r for r in rows2}
    case("reject 改状态不删条目",
         byid.get(tgt["id"], {}).get("status") == "rejected" and len(rows2) == 2,
         [r["status"] for r in rows2])
    case("stale 与 rejected 区分开",
         byid.get(tgt2["id"], {}).get("status") == "stale",
         byid.get(tgt2["id"], {}).get("status"))
    case("裁决进 state_history 且带理由",
         [h.get("action") for h in byid.get(tgt["id"], {}).get("state_history", [])]
         == ["reject"], byid.get(tgt["id"], {}).get("state_history"))
    # 负向：指向不存在 id 的裁决必须**说出来**，不能悄悄当成功。
    case("指向不存在的 id 有日志",
         any("不存在" in x and "cl_不存在" in x for x in lg2), lg2)

    # 取代链（2026-11-15）：`superseded_by` 以前只写不读，等于没记。
    # 分层办法 §8 验收第 1 条要求能答「被谁取代过」，所以既要有读的函数，
    # 也要有「链坏了会报」的检查。全是纯函数，用造出来的条目验，不碰真账本。
    a = _mk(scope="g", subject="ca", predicate="path", value="/old/a-very-long-path",
            origin="memt", status="superseded", source_refs=[{"kind": "fact", "id": "x"}])
    a["superseded_by"] = "cl_b"
    b = _mk(scope="g", subject="ca", predicate="path", value="/new/a-very-long-path",
            origin="memt", status="verified", source_refs=[{"kind": "fact", "id": "y"}])
    b["id"] = "cl_b"
    # 同一条死值另有 active 条目在说 → 残留旧值
    c_alive = _mk(scope="g", subject="ca", predicate="note",
                  value="见 /old/a-very-long-path 这个链接", origin="memt",
                  source_refs=[{"kind": "fact", "id": "z"}])
    pr = _store.chain_problems([a, b, c_alive], rule_ids=set())
    case("链：能读到后任", _store.chain_of([a, b], a["id"]) == [a["id"], "cl_b"],
         _store.chain_of([a, b], a["id"]))
    case("链：完整的链不报缺后任", pr["缺后任"] == [], [c["id"] for c in pr["缺后任"]])
    case("链：残留旧值被抓到",
         [x["id"] for x in pr["残留旧值"]] == [c_alive["id"]],
         [(x["id"], x["dead_value"]) for x in pr["残留旧值"]])

    # 负向 ①：superseded 但没写后任 → 必须报（否则「被谁取代过」无解）
    d = _mk(scope="g", subject="cb", predicate="path", value="/gone/old-path-xyz",
            origin="memt", status="superseded", source_refs=[{"kind": "fact", "id": "q"}])
    case("链：缺后任被抓到",
         [c["id"] for c in _store.chain_problems([d], set())["缺后任"]] == [d["id"]])

    # 负向 ②：指向不存在的 id → 必须报
    e = _mk(scope="g", subject="cc", predicate="path", value="/x/dead-path-abc",
            origin="memt", status="superseded", source_refs=[{"kind": "fact", "id": "r"}])
    e["superseded_by"] = "cl_根本没有"
    case("链：悬空指针被抓到",
         [c["id"] for c in _store.chain_problems([e], set())["指向不存在"]] == [e["id"]])

    # 负向 ③：成环必须被抓到，且 chain_of 不能因为环而挂死
    f1 = _mk(scope="g", subject="cd", predicate="p", value="v1", origin="memt",
             status="superseded", source_refs=[{"kind": "fact", "id": "s"}])
    f2 = _mk(scope="g", subject="cd", predicate="p", value="v2", origin="memt",
             status="superseded", source_refs=[{"kind": "fact", "id": "t"}])
    f1["id"], f2["id"] = "cl_f1", "cl_f2"
    f1["superseded_by"], f2["superseded_by"] = "cl_f2", "cl_f1"
    cyc = _store.chain_problems([f1, f2], set())
    case("链：成环被抓到", len(cyc["成环"]) == 1, [c["id"] for c in cyc["成环"]])
    case("链：有环时 chain_of 不死循环",
         _store.chain_of([f1, f2], "cl_f1") == ["cl_f1", "cl_f2"],
         _store.chain_of([f1, f2], "cl_f1"))

    # 负向 ④：后任是**规矩 id** 不算悬空（跨命名空间是合法的）
    g_rule = _mk(scope="g", subject="ce", predicate="p", value="v", origin="memt",
                 status="superseded", source_refs=[{"kind": "fact", "id": "u"}])
    g_rule["superseded_by"] = "rule_37"
    case("链：后任可跨到规矩命名空间",
         _store.chain_problems([g_rule], {"rule_37"})["指向不存在"] == [])

    # systemd 导入器（2026-11-15）：不能只靠「跑一次看着对」。
    # 2026-11-17：这块只在配置启用了 systemd 时跑。没有 systemd 的机器上
    # `_sh` 一律返回空串、导入器收 0 条，断言会「空过」——那不算通过，要标出来。
    # `systemd` 导入器是**本机适配器**，核心发行版不一定带，所以这里延迟导入：
    # 用 `from .importers import systemd` 写在文件头，会让整套自测在缺这个模块时
    # 连 import 都过不去——那是把一个可选来源变成了硬依赖。
    _systemd = None
    if cfg.importer_enabled("systemd"):
        try:
            from .importers import systemd as _systemd
        except ImportError:
            _systemd = None
    if _systemd is not None:
        rows_sd, _st = _systemd.run(cfg)
        cal = [r for r in rows_sd if r["predicate"] == "calendar"]
        case("systemd 抽到定时器日历", len(cal) >= 5, len(cal))
        # 原来按空白截断会把 `*-*-* 04:20:00` 切成 `*-*-*`，丢掉时间。
        # 不变量是「值里必须带时刻」，不是「不能有空格」。
        case("日历带时刻不被截断", all(":" in r["value"] for r in cal),
             [r["value"] for r in cal][:3])
        case("四二十分被读成每日", any("04:20" in r["value"] for r in cal),
             [r["value"] for r in cal])
        en = [r for r in rows_sd if r["predicate"] == "自启"]
        case("状态值是中文不是裸 enabled",
             all("enabled" not in r["value"] or r["value"].startswith(("是", "否")) for r in en),
             [r["value"] for r in en][:3])
        case("谓词与值不再重复", all(r["predicate"] != r["value"] for r in en))
        case("每条 systemd 事实都有可定位出处",
             all(any(ref.get(k) for k in ("id", "path", "session", "at"))
                 for r in rows_sd for ref in r["source_refs"]), "")
        case("只收账本提到过的 unit（不做 systemd 镜像）", len(rows_sd) < 110, len(rows_sd))
        # 挡「导入器拿自己上一轮的输出当判据」那个闭环：造一份只有 systemd 条目的账本，
        # 导入器应当一条都收不到（如果它还读自己的产物，就会把这些 unit 全收回来）。
        tmp2 = Path(tempfile.mkdtemp())
        fake = tmp2 / "c.jsonl"
        fake.write_text(json.dumps({
            "id": "cl_fake", "subject": "totally-made-up.timer", "predicate": "自启",
            "value": "是（enabled）", "origin": "systemd:unit-files",
            "source_refs": [{"kind": "command", "cmd": "x", "at": "2026-11-15"}]},
            ensure_ascii=False) + "\n", encoding="utf-8")
        rows_self, _ = _systemd.run(_FakeCfg(fake, tmp2 / "no-rules.jsonl"))
        case("导入判据不含自身产物",
             not any(r["subject"] == "totally-made-up.timer" for r in rows_self),
             [r["subject"] for r in rows_self if "made-up" in r["subject"]])
        case("状态翻转不会得到同一个 id",
             _claim_id("s", "u.service", "自启", "是（enabled）", "systemd:unit-files")
             != _claim_id("s", "u.service", "自启", "否（disabled）", "systemd:unit-files"))
    else:
        skip("systemd 导入器断言", 8)

    # 文档分类：过程记录不能当事实上桌。
    # 这两条是 2026-11-15 的实测缺陷——不是假想：
    # ① `灵枢-记忆分桶契约-` 末尾多一个 `-`，258KB 修复流水因此没被排除；
    # ② 新写的 systemd 过程记录自称"不进账本"，其实当时没有任何机制保证。
    #
    # 2026-11-17：只测配置里真的给了 dirs 的情况。原来写死 `Path.home()/记忆`，
    # 换台机器就是断的；而且「黑名单前缀没写错」这条判断题在空目录上恒真。
    conf = cfg.importer("docs")
    roots = [Path(str(d)).expanduser() for d in conf.get("dirs", [])]
    tree = Path(str(conf.get("root") or "")).expanduser() if conf.get("root") else None
    if cfg.importer_enabled("docs") and tree and tree.is_dir():
        log_pat, mark = conf.get("log_pat", []), conf.get("proc_mark", "")
        # 扫整棵 docs 树，不是只扫 dirs：排除前缀可能指向被 `deny` 先挡掉的目录
        # （`留出验证-` 就在 `系统/评测/` 下），只扫 dirs 会让它看起来是写错的。
        allmd = [str(p) for p in tree.rglob("*.md")]
        importable = {str(p) for p in _docs._doc_paths(cfg)}
        case("黑名单前缀没有写错的",
             all(any(pat in s for s in allmd) for pat in log_pat),
             [pat for pat in log_pat if not any(pat in s for s in allmd)])
        case("过程记录不进账本（按文件名）",
             not any("灵枢-记忆分桶契约与修复" in s for s in importable),
             sorted(s for s in importable if "分桶契约" in s))
        marked = [s for s in importable
                  if mark and mark in "\n".join(
                      Path(s).read_text(encoding="utf-8", errors="replace").splitlines()[:12])]
        case("过程记录不进账本（按自标记）", not marked, sorted(marked))
        case("评测集永远不进账本",
             not any(("独立评测集" in s) or ("/50-校对与评测/" in s) or ("/系统/" in s)
                     for s in importable))
    else:
        skip("docs 导入器断言", 4)

    # 复核周期进日报（2026-11-16）：`auto_mark=false` 意味着没有任何东西自动提醒，
    # 只能靠每天 04:20 那份报告。所以「日报里有没有这一段」本身要测。
    from . import report as _report
    import tempfile as _tf3

    class _TTLCfg:
        def ttl(self): return {"path": 30}
        def ttl_derived_origins(self): return ("registry:",)

    old = {"id": "cl_old", "subject": "x", "predicate": "path", "value": "/gone",
           "status": "proposed", "origin": "docs-rule:path",
           "valid_from": "2020-01-01", "source_refs": [], "evidence": "extracted"}
    reg = {"id": "cl_reg", "subject": "m", "predicate": "path", "value": "/cfg",
           "status": "proposed", "origin": "registry:models.yaml",
           "valid_from": "2020-01-01", "source_refs": [], "evidence": "registry"}
    sec = "\n".join(_report._ttl_section([old, reg], _TTLCfg()))
    case("日报里有复核周期一节", "复核周期（TTL）" in sec, sec[:60])
    case("日报列出已过期的条目", "cl_old" in sec and "超期" in sec)
    case("日报不把实时派生算成已过期",
         "cl_reg" not in sec and "| 实时派生 | 1 |" in sec, sec[:300])

    # 探活进日报（2026-11-16）：TTL 那一节说「实时派生要探活」，报告里就得有结果，
    # 否则又是一个「要人记得手动跑」的检查。
    # 没有 probe 模块就没有这一节，前两条测不了（2026-11-17：原来会
    # 在 `sec2.split("## 9")[1]` 上直接 IndexError 崩掉整套自测）。
    class _ProbeCfg(_TTLCfg):
        def importer(self, name):
            return {"models": "/nonexistent/models.yaml"}   # 造一个「没配模型表」
    sec2 = "\n".join(_report._probe_section(_ProbeCfg()))
    if _has_probe():
        case("日报里有探活一节", "探活：配置 vs 实际" in sec2, sec2[:60])
        case("探活没数据时不冒充全正常",
             "一致" not in sec2.split("## 9")[1] or "0 |" in sec2, sec2[:300])
    else:
        skip("日报探活断言", 2)
    case("无配置不出探活一节", _report._probe_section(None) == [])

    # 负向：没传 cfg 时不能凭空造出这一节（旧调用方拿不到配置，也就没有 TTL）
    case("无配置不出复核周期一节", _report._ttl_section([old], None) == [])

    total = ok + len(ng)
    tail = f"（跳过 {len(skipped)} 项：{'、'.join(skipped)}）" if skipped else ""
    print(f"mem-ledger 自测 {ok}/{total} 过{tail}")
    for x in ng:
        print(f"  FAIL {x}")
    return 0 if not ng else 1


# ---------------------------------------------------------------- 检索 23 项

def _has_probe() -> bool:
    """`probe` 是可选组件（绑死 models.yaml / llama.cpp 契约），核心发行版不带。"""
    try:
        from . import probe  # noqa: F401
    except ImportError:
        return False
    return True


def _probe_cases(case) -> None:
    """探活相关的 11 条断言（2026-11-16）。

    TTL 管不了「配置说 up、进程早死了」。这里验的是**分档判据**本身：
    `state: up` 但端口没人，才是要动手的那一档；按需模型没起不算错。
    不分档的话，65 个模型里只有十几个常驻，其余全报红——40 条红字的报告等于没有报告。
    """
    from . import probe as _probe

    case("ctx 归一：纯数字", _probe._parse_ctx("8192") == 8192)
    case("ctx 归一：128k", _probe._parse_ctx("128k") == 131072)
    case("ctx 归一：认不出返回 None", _probe._parse_ctx("很多") is None)

    ms = [{"id": "up_live", "port": 1, "state": "up"},
          {"id": "up_dead", "port": 2, "state": "up"},
          {"id": "ondemand_dead", "port": 3, "state": "on_demand"},
          {"id": "ondemand_live", "port": 4, "state": "on_demand"},
          {"id": "zero", "port": 0, "state": "up"}]
    pa = _probe.audit(ms, listening=lambda p: p in (1, 4))
    case("探活：up 且在听 → 在线", [x["model"] for x in pa["在线"]] == ["up_live"],
         pa["在线"])
    case("探活：up 但没起 → 单列",
         [x["model"] for x in pa["配置说常驻但没起"]] == ["up_dead"],
         pa["配置说常驻但没起"])
    case("探活：按需模型没起不算错",
         [x["model"] for x in pa["按需/下线（符合配置）"]] == ["ondemand_dead"],
         pa["按需/下线（符合配置）"])
    case("探活：按需模型在跑要提一句",
         [x["model"] for x in pa["在跑但配置说没起"]] == ["ondemand_live"],
         pa["在跑但配置说没起"])
    case("探活：port 0 不参与", all(x["model"] != "zero" for v in pa.values() for x in v))
    # 负向：端口没起就不该再去问 /props——65 个模型各超时一次会把日报拖成分钟级
    asked = []
    _probe.audit(ms, listening=lambda p: False,
                 n_ctx_probe=lambda p: asked.append(p))
    case("探活：没起的端口不问 ctx", asked == [], asked)
    # ctx 不符只在两边都认得出时报
    ctxd = _probe.audit([{"id": "m", "port": 9, "state": "up", "ctx": "8192"}],
                        listening=lambda p: True, n_ctx_probe=lambda p: 4096)
    case("探活：ctx 不符能报", [x["model"] for x in ctxd["上下文不符"]] == ["m"],
         ctxd["上下文不符"])
    ctxd2 = _probe.audit([{"id": "m", "port": 9, "state": "up", "ctx": "0"}],
                         listening=lambda p: True, n_ctx_probe=lambda p: 4096)
    case("探活：ctx 配 0 不当成不符", ctxd2["上下文不符"] == [], ctxd2["上下文不符"])


def search(cfg=None) -> int:
    """读路径第①步（作用域过滤）与同义词的自测。"""
    ok, ng, skipped = 0, [], []

    def case(name, cond, got=""):
        nonlocal ok
        if cond:
            ok += 1
        else:
            ng.append(f"{name}（实际 {got!r}）")

    def skip(why: str, n: int = 1) -> None:
        skipped.append(f"{why}×{n}")

    cfg = cfg or _config.load()
    cf = _search.SearchConf(cfg)
    visible = _scope.visible

    def mk(**kw):
        return {"id": "cl_x", "status": "proposed", "evidence": "extracted",
                "source_refs": [{"kind": "fact", "id": "f"}], "subject": "s",
                "predicate": "p", "value": "v", "claim": "c", "visibility": "shared",
                "agent_id": "memt:daily", **kw}

    case("shared 谁都能读", all(visible(mk(), r) for r in ("dsh:tauri", "claude-code:x")))
    case("global 谁都能读", visible(mk(visibility="global"), "dsh:tauri"))
    case("private 只有本人", visible(mk(visibility="private", agent_id="dsh:tauri"),
                                     "dsh:tauri"))
    case("private 别人读不到", not visible(mk(visibility="private", agent_id="dsh:tauri"),
                                           "claude-code:main"))
    case("private 无 agent_id 时拒绝",
         not visible(mk(visibility="private", agent_id=""), "dsh:tauri"))
    case("team 成员可读", visible(mk(visibility="team:video"), "team:video"))
    case("team 子身份可读", visible(mk(visibility="team:video"), "team:video:1"))
    case("team 外拒绝", not visible(mk(visibility="team:video"), "dsh:tauri"))
    case("未知可见性拒绝", not visible(mk(visibility="weird"), "dsh:tauri"))
    # 字段缺失 = 未知 = 拒绝。这条不是形式主义：`rule_as_doc()` 原来没写这个字段，
    # 加闸门那一刻 41 条规矩会被全部吃掉。规则层必须显式声明 shared。
    case("可见性缺失拒绝", not visible(mk(visibility=""), "dsh:tauri"))
    case("规矩投影显式带 shared",
         _search.rule_as_doc({"id": "rule_x", "rule": "x", "domain": "d"})
         .get("visibility") == "shared")
    case("管理员读全部", visible(mk(visibility="private", agent_id="x"), _scope.MEMORY_ADMIN))
    case("READ_ALL 读全部", visible(mk(visibility="private", agent_id="x"), _scope.READ_ALL))

    # 同义表（2026-11-15）：只验「该合的合了、该分的没合」。
    # 反例比正例重要——合并错一次，两类条目从此互相污染，而且再也看不出来。
    tk = lambda s: _search.tokens(s, cf)          # noqa: E731

    def syn_case(name, canon, query, want):
        """配置里声明了这个同义词组才测。

        2026-11-17：原来这七条断言的是**出厂配置里有 `swap`/`timer`/`enabled`/`build`
        这几组**。换一份配置（通用部署不会收录「交换空间」这种词）必红，
        红的还不是机制坏了。改成「配了就验它真的合了、没配就跳过并打出来」。
        """
        if canon not in cf.synonyms:
            skipped.append(name)
            return
        case(name, (canon in tk(query)) == want, tk(query))

    syn_case("交换空间→swap", "swap", "本机有几块交换空间", True)
    syn_case("定时器→timer", "timer", "账本重建定时器几点跑", True)
    syn_case("开机自启→enabled", "enabled", "是不是开机自启的", True)
    syn_case("重建→build", "build", "账本每天重建", True)
    syn_case("图片不并入 image", "image", "把这张图片挪走", False)
    syn_case("内存不并入 memory", "memory", "内存还剩多少", False)
    syn_case("编译不并入 build", "build", "重新编译 llama.cpp", False)
    case("同一词语义只归一个规范 token",
         all(len({c for c, forms in cf.synonyms.items() if s in forms}) == 1
             for s in cf.surf_to_canon))

    # 闸门集成：被过滤掉的条目不得进索引（否则会白占 top-8 名额）
    docs = [mk(id="a"), mk(id="b", visibility="private", agent_id="other:x")]
    case("索引里过滤私有条目",
         [c["id"] for c in _search.Index(docs, cf, "dsh:tauri").docs] == ["a"],
         [c["id"] for c in _search.Index(docs, cf, "dsh:tauri").docs])
    case("管理员索引不过滤", len(_search.Index(docs, cf, _scope.MEMORY_ADMIN).docs) == 2)

    # 同事实合并（2026-11-15）：四个来源说「q30 端口 8085」只该占一个名额。
    # 在检索结果层合，不动账本——多来源是 corroborate() 的证据，删不得。
    rows = [
        (mk(id="c1", subject="q30", predicate="port", value="8085",
            agent_id="teamd:registry"), 0.9),
        (mk(id="c2", subject="q30", predicate="port", value="8085",
            agent_id="dsh:lingshu"), 0.8),
        (mk(id="c3", subject="q30", predicate="port", value="8085",
            agent_id="me:docs"), 0.7),
        (mk(id="c4", subject="glm47", predicate="port", value="8088",
            agent_id="teamd:registry"), 0.6),
    ]
    mg = _search.merge_same_fact(rows)
    case("同事实合并成一条", len(mg) == 2, [c["id"] for c, _ in mg])
    case("合并保留排名最高的那条", mg[0][0]["id"] == "c1", mg[0][0]["id"])
    case("合并不丢来源", len(mg[0][0].get("also_from") or []) == 2,
         mg[0][0].get("also_from"))
    case("合并按数值归一判同（8085 与 08085 同）",
         len(_search.merge_same_fact([
             (mk(id="p1", subject="q30", predicate="port", value="8085"), 0.9),
             (mk(id="p2", subject="q30", predicate="port", value="08085"), 0.8)])) == 1)
    # 负向：不同值不能合（8090 ≠ 8085）
    case("不同值不合并",
         len(_search.merge_same_fact([
             (mk(id="n1", subject="q30", predicate="port", value="8085"), 0.9),
             (mk(id="n2", subject="q30", predicate="port", value="8090"), 0.8)])) == 2)
    # 负向：规矩不改写（layer=rule 不参与合并），散文（无主语）也不合并
    case("规矩不参与合并",
         len(_search.merge_same_fact([
             (_search.rule_as_doc({"id": "rule_a", "rule": "r", "domain": "d"}), 0.9),
             (_search.rule_as_doc({"id": "rule_b", "rule": "r", "domain": "d"}), 0.8)])) == 2)
    case("无主语散文不参与合并",
         len(_search.merge_same_fact([
             (mk(id="s1", subject="", value="同一句话"), 0.9),
             (mk(id="s2", subject="", value="同一句话"), 0.8)])) == 2)
    # 合并结果不能污染原条目：索引里的 dict 是共享的，写进去会带到下一次检索
    orig = mk(id="m1", subject="q30", predicate="port", value="8085")
    _search.merge_same_fact([(orig, 0.9),
                             (mk(id="m2", subject="q30", predicate="port", value="8085"), 0.8)])
    case("合并不往原 dict 里写", "also_from" not in orig, orig.get("also_from"))

    # 整合（`mem dream`，2026-11-16）：从 GitHub 上的 agent-memory / MemoryOS /
    # mem0 取了三家的做法，只抄了「把复核成本降下来」这一条——产出提案，
    # 一条都不自动写。这里验的是「提议对不对」和「绝不自动写」。
    from memlib import dream as _dream
    case("片段不当路径：/unit", _dream._looks_like_fragment("/unit"))
    case("标准顶层目录不算片段", not _dream._looks_like_fragment("/tmp")
         and not _dream._looks_like_fragment(str(Path.home() / "Apps/x")))
    case("多段路径不算片段", not _dream._looks_like_fragment("/foo/bar"))

    def _dc(id_, subj, pred, val, ev="extracted", status="proposed", origin="docs-rule:port"):
        return {"id": id_, "scope": "global", "subject": subj, "predicate": pred,
                "value": val, "claim": f"{subj} {val}", "status": status, "evidence": ev,
                "origin": origin, "valid_from": "2026-11-01", "state_history": [],
                "superseded_by": "", "confidence": 0.5, "source_refs": []}
    # 跨源冲突：实测 verified 一侧 + 抽取 side，值不同、来源系统不同 → 裁弱侧
    pair = [_dc("cl_s", "q30", "port", "8085", ev="measured", status="verified",
                origin="systemd:unit-files"),
            _dc("cl_w", "q30", "port", "9999", ev="extracted", origin="docs-rule:port")]
    props = {p["target"]: p for p in _dream.collect(pair)}
    case("整合：弱侧被提议 reject", props.get("cl_w", {}).get("action") == "reject", props)
    case("整合：不强推强侧", "cl_s" not in props, list(props))
    # 负向：两侧一样强（都是 verified/extracted）时不裁——那不是脚本该判的
    both_weak = [_dc("cl_a", "q30", "port", "8085", origin="docs-rule:port"),
                 _dc("cl_b", "q30", "port", "9999", origin="claude:extracted-port")]
    case("整合：同弱不裁", _dream.collect(both_weak) == [],
         _dream.collect(both_weak))
    # 负向：同源互证不算「不同来源」
    same_src = [_dc("cl_s", "q30", "port", "8085", ev="measured", status="verified",
                    origin="docs-rule:port"),
                _dc("cl_w", "q30", "port", "9999", origin="docs-rule:port")]
    case("整合：同源不裁", _dream.collect(same_src) == [], _dream.collect(same_src))

    # 提案文件：追加、幂等、标记后 pending 不再出现
    import tempfile as _tf4
    pp = Path(_tf4.mkdtemp(prefix="dream-")) / "proposals.jsonl"
    n1 = _dream.record(pp, _dream.collect(pair))
    n2 = _dream.record(pp, _dream.collect(pair))       # 第二次不该重复写
    case("整合提案可落盘且幂等", n1 == 1 and n2 == 0, (n1, n2))
    # 整合提案进日报（2026-11-16）：`mem dream` 原来只能人手动跑，
    # 于是「报告每周列同一批问题、没人处理」照旧。接进 build --report 后才有人看得到。
    from . import report as _report          # 这个函数不共享 ledger() 里的局部导入

    class _DreamCfg:                        # 只需 path_of：_dream_section 不碰别的
        def path_of(self, key): return pp2 / "proposals.jsonl"
    pp2 = Path(_tf4.mkdtemp(prefix="dreamsec-"))
    pair2 = [_dc("cl_s", "q30", "port", "8085", ev="measured", status="verified",
                 origin="systemd:unit-files"),
             _dc("cl_w", "q30", "port", "9999", ev="extracted", origin="docs-rule:port")]
    sec3 = "\n".join(_report._dream_section(pair2, _DreamCfg(), newly_recorded=3))
    case("日报里有整合提案一节", "整合提案" in sec3, sec3[:60])
    case("日报写明没有自动应用", "没有任何一条被自动应用" in sec3, sec3[:160])
    case("日报列出提案目标与动作", "cl_w" in sec3 and "reject" in sec3, sec3[:400])
    # 负向：生成这一节**不能**顺手把提案写进文件——写操作只许发生在 build 里
    case("生成日报不写提案文件", not (pp2 / "proposals.jsonl").exists(),
         os.listdir(pp2))
    case("无配置不出整合提案一节", _report._dream_section(pair2, None) == [])
    pid = _dream.collect(pair)[0]["id"]
    case("整合提案能接受/否掉", _dream.mark(pp, pid, "accepted")
         and _dream.by_id(pp, pid)["status"] == "accepted")
    case("已处理的提案不再 pending", _dream.pending(pair, pp) == [],
         _dream.pending(pair, pp))
    case("标记不存在的提案要返回 False", not _dream.mark(pp, "pp_nope", "accepted"))

    # 只读 API（2026-11-16）：别的 agent 查账本走 HTTP。安全断言全在这里——
    # 「别人的 private 返回 404 而不是 403」这类事，写代码时记得，半年后不一定记得。
    from memlib import api as _api
    import io as _io

    # fail closed：没有 token 表 = 谁都不能读，不是"谁都能读"
    try:
        _api.load_tokens("/nonexistent/tokens.toml")
        case("API：没有 token 表就拒绝启动", False)
    except FileNotFoundError:
        case("API：没有 token 表就拒绝启动", True)
    tt = Path(_tf4.mkdtemp(prefix="apitok-"))
    (tt / "bad.toml").write_text('[[token]]\nvalue = "x"\nagent = "a b"\n', encoding="utf-8")
    try:
        _api.load_tokens(tt / "bad.toml"); case("API：agent_id 不合法要拒绝", False)
    except RuntimeError:
        case("API：agent_id 不合法要拒绝", True)
    (tt / "dup.toml").write_text(
        '[[token]]\nvalue = "x"\nagent = "a"\n[[token]]\nvalue = "x"\nagent = "b"\n',
        encoding="utf-8")
    try:
        _api.load_tokens(tt / "dup.toml"); case("API：重复 token 要拒绝", False)
    except RuntimeError:
        case("API：重复 token 要拒绝", True)
    (tt / "ok.toml").write_text(
        '[[token]]\nvalue = "tk-main"\nagent = "claude-code:main"\n', encoding="utf-8")
    toks = _api.load_tokens(tt / "ok.toml")
    case("API：token 表读得出 agent", toks.get("tk-main") == "claude-code:main", toks)
    case("API：错 token 认不出身份", _api.whoami(toks, "nope") == "")
    case("API：空 token 认不出身份", _api.whoami(toks, "") == "")
    case("API：出口再过一遍脱敏", "[已脱敏" in _api._json_bytes(
        {"v": "".join(["sk-"] + ["A1b2C3d4"] * 5)}).decode())
    case("API：拒绝监听非回环地址", _api.serve(host="0.0.0.0", port=0) == 2)

    # 路由：造一个只读的假账本（不动真账本，避免自测依赖今天的账本内容）
    class _Idx:
        def __init__(self, docs, dropped=0): self.docs = docs; self.dropped_scope = dropped

    class _ApiCfg:
        def path_of(self, k): return tt

    class _ApiLedger:
        def __init__(self, docs):
            self._d = docs; self._mtime = 0.0; self.cfg = _ApiCfg(); self.cf = None

        def index(self, reader):
            vis = [c for c in self._d if _scope.visible(c, reader)]
            return _Idx(vis, len(self._d) - len(vis))

    _docs = [
        dict(_dc("cl_a", "s", "p", "v"), visibility="shared", agent_id="me"),
        dict(_dc("cl_p", "s", "p", "只有自己人看得到"),
             visibility="private", agent_id="claude-code:main"),
        # /rules 用的两条：一条普通规矩、一条留出目标
        dict(_dc("rule_1", "写作", "规矩", "写稿前先列提纲"),
             layer="rule", visibility="shared", agent_id="me", heldout=False),
        dict(_dc("rule_h", "写作", "规矩", "留出目标不该默认列出来"),
             layer="rule", visibility="shared", agent_id="me", heldout=True),
    ]
    _BaseH = _api.make_handler(_ApiLedger(_docs), {"tk-main": "claude-code:main",
                                                   "tk-other": "dsh:lingshu",
                                                   "tk-me": "me"}, str(tt / "ok.toml"))

    class _QuietH(_BaseH):          # 自测里不需要访问日志
        def log_message(self, *a): pass

    _H = _QuietH

    def _call(method, path, token=""):
        class Sock:
            def __init__(self, req): self.req = req; self.out = _io.BytesIO()

            def makefile(self, mode="rb", *a, **k):
                return _io.BytesIO(self.req) if "r" in mode else self.out

            def sendall(self, b): self.out.write(b)

            def close(self): pass

        raw = f"{method} {path} HTTP/1.1\r\nHost: x\r\n"
        if token:
            raw += f"Authorization: Bearer {token}\r\n"
        raw += "Content-Length: 0\r\n\r\n"
        c = Sock(raw.encode())
        _H(c, ("127.0.0.1", 1), None)
        txt = c.out.getvalue().decode()
        return int(txt.split()[1]), txt

    case("API：未认证 health 只说活着", _call("GET", "/health")[0] == 200
         and "docs" not in _call("GET", "/health")[1])
    case("API：搜索要 token", _call("GET", "/search?q=x")[0] == 401)
    case("API：坏 token 401", _call("GET", "/stats", "bad")[0] == 401)
    case("API：自己的 private 读得到", _call("GET", "/claim/cl_p", "tk-main")[0] == 200)
    # 404 而不是 403：403 等于告诉对方"这条存在，只是你看不到"
    case("API：别人的 private 是 404 不是 403",
         _call("GET", "/claim/cl_p", "tk-other")[0] == 404)
    case("API：管理员读得到别人的 private",
         _call("GET", "/claim/cl_p", "tk-me")[0] == 200)
    case("API：写方法一律 405", _call("POST", "/search", "tk-main")[0] == 405)
    case("API：未知路径 404", _call("GET", "/nope", "tk-main")[0] == 404)

    # /rules（2026-11-16）：规矩要列得出来，但留出目标默认不列——
    # 谁都读得走的话，任何一次「照规矩优化检索」都会污染留出集。
    _rc, _rt = _call("GET", "/rules", "tk-main")
    _rj = json.loads(_rt.split("\r\n\r\n", 1)[1])
    case("API：/rules 默认不返回留出目标",
         _rc == 200 and {r["id"] for r in _rj["rules"]} == {"rule_1"}, _rj.get("rules"))
    case("API：/rules 非管理员要留出集是 403",
         _call("GET", "/rules?include_heldout=1", "tk-main")[0] == 403)
    _rhm, _rht = _call("GET", "/rules?include_heldout=1", "tk-me")
    case("API：/rules 管理员能要留出集",
         _rhm == 200 and {r["id"] for r in
                          json.loads(_rht.split("\r\n\r\n", 1)[1])["rules"]} >= {"rule_1", "rule_h"})
    case("API：/rules 的 domain 过滤生效",
         json.loads(_call("GET", "/rules?domain=%E5%86%99%E4%BD%9C", "tk-main")[1]
                    .split("\r\n\r\n", 1)[1])["total"] == 1)
    case("API：/rules 的 domain 过滤能过滤空",
         json.loads(_call("GET", "/rules?domain=%E4%B8%8D%E5%AD%98%E5%9C%A8", "tk-main")[1]
                    .split("\r\n\r\n", 1)[1])["total"] == 0)

    # 截至某日检索（as-of，2026-11-16）：存了不等于能问——四个字段以前没有一个查询读。
    from memlib import search as _s2
    docs = [_dc("a", "x", "port", "1", origin="o"),     # 无 valid_from 的活跃条目
            dict(_dc("b", "y", "port", "2", origin="o"), valid_from="2026-11-10"),
            dict(_dc("c", "z", "port", "3", origin="o"), valid_from="2026-10-01",
                 invalid_at="2026-11-05"),
            dict(_dc("d", "w", "port", "4", origin="o", status="superseded"),
                 state_history=[{"at": "2026-11-12", "action": "supersede"}]),
            dict(_dc("e", "v", "port", "5", origin="o", status="superseded"))]
    docs[0]["valid_from"] = ""
    kept, st = _s2.as_of_filter(docs, "2026-11-06")
    ids = {c["id"] for c in kept}
    case("as-of：还没成立的排除", "b" not in ids, ids)
    case("as-of：已失效的排除", "c" not in ids, ids)
    case("as-of：当时还活跃的保留", "d" in ids, ids)
    case("as-of：当时已作废的排除", "e" not in ids, ids)
    case("as-of：日期不明要计数", st["日期不明"] == 1, st)

    # 复核周期（TTL，2026-11-16）：分层办法 §5 写了「到期标 stale」，
    # 但 revalidate_after 一直全是 null、没有任何代码读它。这里验的是"会报警"，
    # 全用造出来的日期，不依赖今天是几号。
    from datetime import date as _date
    from memlib import ttl as _ttl

    def _t(id_, pred, d, status="proposed"):
        return {"id": id_, "predicate": pred, "value": "v", "status": status,
                "valid_from": d}

    tmap = {"path": 30, "port": 90}
    today = _date(2026, 11, 16)
    dd = _ttl.due([_t("t_old", "path", "2026-01-01"),      # 超期很久
                   # 75 天 / 90，落在「剩 1/4 周期以内」（>67.5 且 <=90）。
                   # 原来写 2026-09-10 正好是 67 天，卡在阈值边界上——
                   # 边界值用来测边界是对的，但用在这个用例里测的就不是「将到期」了。
                   _t("t_soon", "port", "2026-09-02"),
                   _t("t_new", "path", "2026-11-10"),      # 新鲜
                   _t("t_nodate", "path", ""),             # 日期不明
                   _t("t_bad", "port", "不是日期")],       # 日期不明
                  tmap, today)
    case("TTL 抓得到已过期", [x["claim"]["id"] for x in dd["已过期"]] == ["t_old"],
         [x["claim"]["id"] for x in dd["已过期"]])
    case("TTL 抓得到将到期", [x["claim"]["id"] for x in dd["将到期"]] == ["t_soon"],
         [x["claim"]["id"] for x in dd["将到期"]])
    case("TTL 新鲜的不报", all(x["claim"]["id"] != "t_new"
                              for k in ("已过期", "将到期") for x in dd[k]))
    # 算不出日期必须单列，不能悄悄当"没问题"——那是最容易骗过自己的形态
    case("TTL 日期不明单列",
         sorted(x["claim"]["id"] for x in dd["日期不明"]) == ["t_bad", "t_nodate"],
         [x["claim"]["id"] for x in dd["日期不明"]])
    # 负向：没有 TTL 的谓词不参与（默认不打扰）
    case("TTL 只作用于配置里有的谓词",
         all(x["claim"]["predicate"] in tmap for k in dd for x in dd[k]))
    # 负向：已作废的条目不再催复核
    dd2 = _ttl.due([_t("t_dead", "path", "2020-01-01", status="superseded")], tmap, today)
    case("TTL 不催已作废条目", dd2["已过期"] == [], dd2["已过期"])
    # 复核日：从 valid_from 加周期算出，同一份账本不同天算出的值相同
    case("复核日算得对",
         _ttl.revalidate_after(_t("x", "path", "2026-01-01"), tmap) == "2026-01-31",
         _ttl.revalidate_after(_t("x", "path", "2026-01-01"), tmap))
    case("无周期谓词无复核日",
         _ttl.revalidate_after(_t("x", "自启", "2026-01-01"), tmap) == "")
    # 实时派生（2026-11-16）：值每次 build 从配置/命令输出重读，TTL 判不出东西。
    # 不豁免的话，registry 96 条 + systemd 7 条会全挤进「日期不明」，
    # 把真正该复核的十几条埋掉——多了 103 行噪声，等于这个检查没人看。
    der = ("registry:", "systemd:")
    reg = _t("t_reg", "port", "2020-01-01")
    reg["origin"] = "registry:models.yaml"
    dd3 = _ttl.due([reg], tmap, today, derived_prefixes=der)
    case("实时派生不进已过期", dd3["已过期"] == [], dd3["已过期"])
    case("实时派生不进日期不明", dd3["日期不明"] == [], dd3["日期不明"])
    case("实时派生单列", [x["claim"]["id"] for x in dd3["实时派生"]] == ["t_reg"],
         [x["claim"]["id"] for x in dd3["实时派生"]])
    case("实时派生无复核日",
         _ttl.revalidate_after(reg, tmap, der) == "")
    # 负向：豁免只认配置给的前缀。同一个 origin，不传前缀就照常按日期判断——
    # 否则「豁免」会变成一把默认打开的伞，把真实的过期也一起盖住。
    case("不传前缀则照常判过期",
         [x["claim"]["id"] for x in _ttl.due([reg], tmap, today)["已过期"]] == ["t_reg"],
         _ttl.due([reg], tmap, today)["已过期"])
    # 前缀是前缀不是全等：`systemd` 也要罩住 `systemd:unit-files`
    case("前缀匹配不是全等",
         _ttl.is_derived({"origin": "systemd:unit-files"}, der)
         and not _ttl.is_derived({"origin": "docs-rule:port"}, der))

    # 探活（2026-11-16）：TTL 管不了「配置说 up、进程早死了」，只能连上去看。
    # probe 是**可选**组件（绑死 models.yaml / llama.cpp 契约），核心发行版不带。
    if _has_probe():
        _probe_cases(case)
    else:
        skip("探活断言", 11)

    # 日期归一（2026-11-16）：lingshu 的 created_at 是带小数的 Unix 时间戳，
    # 原来 `str[:10]` 截成 1793459835——看着像日期，实际是秒数，107 条全中。
    from memlib.schema import norm_date as _nd
    case("时间戳归一成日期", _nd("1793459835.57") == "2026-10-31", _nd("1793459835.57"))
    case("ISO 归一", _nd("2026-11-15T10:00:00") == "2026-11-15")
    case("中文日期归一", _nd("2026年11月5日") == "2026-11-05")
    case("认不出返回空", _nd("abc") == "" and _nd("") == "")
    # 散文来源的日期只能靠文件 mtime。读不到就必须是空（= 日期不明），
    # 不能兜底成今天——那就又变回「每次构建时钟归零」。
    import tempfile as _tf, os as _os
    from memlib.schema import mtime_date as _md
    with _tf.NamedTemporaryFile("w", suffix=".md", delete=False) as _f:
        _f.write("x"); _tmp = _f.name
    try:
        case("mtime 日期取得到", _md(_tmp) != "" and len(_md(_tmp)) == 10, _md(_tmp))
    finally:
        _os.unlink(_tmp)
    case("mtime 读不到返回空（不兜底成今天）", _md("/nonexistent/不会有这个文件.md") == "")

    total = ok + len(ng)
    tail = f"（跳过 {len(skipped)} 项：{'、'.join(skipped)}）" if skipped else ""
    print(f"memory-search 自测 {ok}/{total} 过{tail}")
    for x in ng:
        print(f"  FAIL {x}")
    return 0 if not ng else 1


def run_all(cfg=None) -> int:
    c = cfg or _config.load()
    return max(ledger(c), search(c))
