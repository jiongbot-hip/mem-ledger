"""账本读写：裁决（overrides）、跨源互证、身份标注、缺口清单、冲突检测。

## 只增不删

矛盾不靠删除解决，靠状态表达：`proposed → verified / rejected / superseded / stale`。
`overrides.jsonl` **只追加**——要撤回一条裁决，加一条反向 action，不要删行。
这条不是洁癖：账本是生成物，删掉的裁决下次 `build` 就回来了，而删除动作本身不可追溯。

## 身份是推导的，缺口清单就是用来暴露这件事的

三种 `agent_basis`，含义不同，**不能混着看**：

| 值 | 含义 | 谁造成 |
|---|---|---|
| `recorded` | 写入时显式记下了是谁 | `propose --as` |
| `unrecorded` | 写入端没写身份，只是默认成 `me` | `propose` 没传 `--as` |
| `derived-origin` | 导入器按 origin 反推，本来就无从记录 | memt / 灵枢 / docs / … |

`gaps()` 只把 **`unrecorded`** 列为「身份未记录」——那才是能修的缺口；
`derived-origin` 单列为「身份为推导」，是已知限制，按来源汇总展示。

（2026-11-15 之前，`unrecorded` 被错标成 `recorded`：默认值冒充了记录值，
2 万多条里唯一一条真缺口因此看不出来。）

## 规则只有一份

可见性判断在 `memlib/scope.py`，读路径和写路径共用。这里不复制——
写的时候说能读、读的时候说不能读，整套可见性就自相矛盾。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import scope
from .schema import ACTIVE, EVIDENCE_RANK, make_claim as _mk, norm as _norm, norm_date
from .text import norm_value

# 存量 2066 条写入时没有身份字段，只能从 origin 反推。规则集中在这里，
# 要改「谁是 memt」只改这张表，不改散落各处的 importer。
AGENT_BY_ORIGIN = {
    "memt": "memt:daily",          # memt-daily.timer 23:55 抽取
    "lingshu": "dsh:lingshu",      # DSH dsh-memory 插件
    "claude": "claude-code:main",
    "registry": "teamd:registry",  # models.yaml 的配置声明
    "docs": "me:docs",             # ~/记忆 与 系统档案 里的人写文档
    "manual": "me",                # overrides.jsonl 里的人裁
}

MANUAL_AGENT = scope.MEMORY_ADMIN


def origin_system(origin: str) -> str:
    """origin 的「系统」部分：`memt-rule:port` → `memt`，`docs-rule:path` → `docs`。

    2026-11-15 修：原来 corroborate 直接 `origin.split(":")[0]`，
    把 `memt` 和 `memt-rule` 当成两个独立来源——同一个系统的规则抽取
    和原始事实互相"互证"，等于自己给自己发 verified。同一处口径统一走这里。
    """
    base = (origin or "").split(":")[0]
    return base[:-5] if base.endswith("-rule") else base


def read_jsonl(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    out = []
    for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if ln.strip():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def shard_files(ledger: Path | str) -> list[Path]:
    """账本分片：`20-账本/claims-archive-<年>.jsonl`，按年一份。

    2026-11-17 起「结案且超过 30 天」的条目从主账本挪到这里。分片**不是删除**：
    `memlib/search.load_claims` 会一起读，检索结果不变；`mem shard` 是唯一写入口，
    `build` 只负责把已经在分片里的同状态条目排除出主文件，免得重建时又长回去。
    """
    return sorted(Path(ledger).glob("claims-archive-*.jsonl"))


def load_archived(ledger: Path | str) -> dict[str, str]:
    """id → 归档时的 status。build 用它对账：状态变了（被重新打开）就留在主账本。"""
    out: dict[str, str] = {}
    for p in shard_files(ledger):
        for c in read_jsonl(p):
            if c.get("id"):
                out[str(c["id"])] = str(c.get("status") or "")
    return out


def apply_overrides(claims: list[dict], overrides: Path | str,
                    legacy_visibility: str, new_write_visibility: str) -> tuple[list[dict], list[str]]:
    """把 `overrides.jsonl` 的裁决应用到 claims 上。返回 (claims, 日志)。

    `propose` 是唯一会**新增**条目的动作，其余都只改现有条目的状态。
    """
    log: list[str] = []
    path = Path(overrides)
    if not path.exists():
        return claims, log
    idx = {c["id"]: c for c in claims}
    for ln in path.open(encoding="utf-8", errors="ignore"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            log.append(f"overrides.jsonl 有坏行，已跳过：{ln[:60]}")
            continue
        act = o.get("action")
        if act == "propose":
            # 2026-11-15：写入端开始**记录**身份，不再全靠推导。
            paid = (o.get("agent_id") or "").strip()
            pvis = (o.get("visibility") or "").strip()
            if paid and not scope.valid_agent_id(paid):
                log.append(f"propose 的 agent_id 不合法（{paid!r}），已跳过")
                continue
            if pvis and not scope.valid_visibility(pvis):
                log.append(f"propose 的 visibility 不合法（{pvis!r}），已跳过")
                continue
            c = _mk(scope=o.get("scope") or "global",
                    subject=o.get("subject") or "", predicate=o.get("predicate") or "",
                    value=o.get("value") or o.get("claim") or "",
                    claim=o.get("claim") or o.get("value") or "",
                    status="proposed", evidence=o.get("evidence", "extracted"),
                    confidence=0.6, owner=o.get("owner") or "me",
                    origin="manual", origin_ref=o.get("at", ""),
                    # 人工写入的日期（2026-11-16）：`mem propose` 会写。
                    # 老记录没有这项时退回这条裁决自己的 `at`——它冻结在追加日志里，
                    # 不像 captured_at 每次 build 重置（那就等于时钟归零）。
                    valid_from=norm_date(o.get("valid_from")) or norm_date(o.get("at")),
                    source_refs=[{"kind": "manual", "at": o.get("at", ""),
                                  "basis": o.get("basis", "")}])
            if paid:
                c["agent_id"] = paid
                c["agent_basis"] = "recorded"   # 这一条的身份是写入时记下的
                c["visibility"] = pvis or new_write_visibility
            else:
                # 没写 --as：**没有人记录过这条是谁写的**。
                # 旧实现让 annotate_identity 看到 origin=manual 就标 recorded，
                # 于是一个"默认值 me"被当成"记录值 me"——身份是猜的，却显示为已记录。
                # 现在明确标成 unrecorded，进「身份未记录」清单。
                c["agent_basis"] = "unrecorded"
            if c["id"] in idx:
                # 同 id 说明同一主张已存在。若现有那条对提议者不可见，
                # **不能**悄悄跳过——那等于让一个人合法提出的事实凭空消失。
                prev = idx[c["id"]]
                if paid and not scope.visible(prev, paid):
                    log.append(
                        f"propose {c['id']} 与你提出的完全一致，但已存在的那条是 "
                        f"{prev.get('visibility')}/{prev.get('agent_id')}，对你不可见；"
                        "未做任何合并，请人工裁决")
                else:
                    log.append(f"propose 已存在同主张，跳过：{c['id']}")
                continue
            claims.append(c)
            idx[c["id"]] = c
            log.append(f"propose {c['id']}")
            continue
        c = idx.get(o.get("target", ""))
        if not c:
            log.append(f"裁决目标不存在，已跳过：{o.get('target')}")
            continue
        if act == "verify":
            c["status"] = "verified"
            c["evidence"] = o.get("evidence", "user")
            c["confidence"] = max(c["confidence"], 0.9)
            # 复核就是「那天确认过」（2026-11-16）。不盖日期，TTL 对这条
            # 只能永远报「日期不明」——复核了等于没复核。老记录没这一项就不动它。
            _d = norm_date(o.get("valid_from"))
            if _d:
                c["valid_from"] = _d
        elif act == "reject":
            c["status"] = "rejected"
        elif act == "stale":
            c["status"] = "stale"
        elif act == "supersede":
            c["status"] = "superseded"
            c["invalid_at"] = o.get("at") or o.get("date")
            c["superseded_by"] = o.get("by", "") or ""
        c.setdefault("state_history", []).append(
            {"at": o.get("at") or o.get("date") or "", "action": act,
             "basis": o.get("basis", ""), "actor": o.get("actor", "me")})
        log.append(f"{act} {c['id']}")
    return claims, log


def corroborate(claims: list[dict]) -> int:
    """同一 subject+predicate+value 出现在 ≥2 个独立来源 → 按总则升格 verified。"""
    groups: dict[tuple, list[dict]] = {}
    for c in claims:
        if not (c["subject"] and c["predicate"]) or c["status"] not in ACTIVE:
            continue
        groups.setdefault((_norm(c["subject"]), _norm(c["predicate"]),
                           norm_value(_norm(c["predicate"]), c["value"])), []).append(c)
    n = 0
    for group in groups.values():
        origins = {origin_system(c["origin"]) for c in group}
        if len(origins) >= 2:
            for c in group:
                if c["status"] == "proposed":
                    c["status"] = "verified"
                    c["confidence"] = max(c["confidence"], 0.8)
                    c["note"] = (c["note"] + "；跨源互证").strip("；")
            n += 1
    return n


def annotate_identity(claims: list[dict], legacy_visibility: str) -> list[dict]:
    """补 agent_id / agent_basis / visibility / source_session_ids。

    三条都是**推导值，不是写入记录**，`agent_basis=derived-origin` 标明这一点。
    谁写的这件事，本机现在没有一处真正记录过——这正是缺口清单要暴露的。

    source_session_ids 从 source_refs 里能推到 L0 的 session 派生，
    推不出来就是空列表，**不猜**。
    """
    for c in claims:
        origin = c.get("origin") or ""
        base = origin_system(origin)
        if not c.get("agent_id"):
            c["agent_id"] = AGENT_BY_ORIGIN.get(base, f"unknown:{origin}")
        if not c.get("agent_basis"):
            # **不覆盖 apply_overrides 已经标好的值**（recorded / unrecorded）。
            # 能走到这里的 manual 条目，是 overrides 里老格式（没有 agent_id 字段）
            # 留下的，写入时本来就没人写明是谁——记为 unrecorded，不冒充推导也不冒充记录。
            c["agent_basis"] = ("unrecorded" if base == "manual" else "derived-origin")
        if not c.get("visibility"):
            c["visibility"] = legacy_visibility
        sids: list[str] = []
        for ref in c.get("source_refs") or []:
            if not isinstance(ref, dict):
                continue
            sid = ref.get("session")
            if sid and sid not in sids:
                sids.append(str(sid))
        c["source_session_ids"] = sids
    return claims


def gaps(claims: list[dict]) -> dict[str, list[dict]]:
    """缺口清单（分层管理办法 §7 P0 第三项）。

    - 无出处：source_refs 为空，或没有任何可定位的指针（path/id/session/at 之外全是空）。
    - 作用域越界：`visibility=global` 却不是记忆管理员写的（§2：global 读权限由人确认）。
    - 身份未记录：本该在写入时记下的身份没有记下（`propose` 类）。**这才是要修的**。
    - 身份为推导：导入器的身份只能从 origin 反推。这是**已知限制**，不是缺陷。

    ## 2026-11-15 修一个真 bug：查错了字段

    旧实现查的是 `scope == "global"`。但 §2 的读写表里，「由记忆管理员确认」限定的是
    **`visibility`**；`scope` 的定义是「关于什么」（global / project:xxx / framework:xxx），
    跟谁能写没有关系。

    实测后果：1633 条 memt 导入的事故 `scope=global`、`visibility=shared`，完全合法，
    却被整批判成「越界」——这张清单一度 99% 是噪声，等于失效。

    **没有改数据去迁就规则**：`scope` 是 claim id 的组成字段，改它会让 1633 个 id 全变，
    连带 7 条人工裁决（4 memt + 2 memt-rule + 1 指向 memt）静默失效。错的字段，不是错的数据。

    ## 无身份为什么拆成两条

    分层办法 §7 P0 备注写得很清楚：五个 importer 不是"某个 Agent 在写"，
    而是"定时器在抽取"，硬编一个身份是假的。所以这些条目的「推导身份」是设计，不是漏洞。
    把它们和「本该记却漏记」混在一起，前者 2123 条会把后者淹掉——
    一个永远非空的告警等于没有告警。
    """
    out: dict[str, list[dict]] = {
        "无出处": [], "作用域越界": [], "身份未记录": [], "身份为推导": [],
    }
    for c in claims:
        refs = [r for r in (c.get("source_refs") or []) if isinstance(r, dict)]
        locatable = [r for r in refs
                     if any(r.get(k) for k in ("id", "path", "session", "at"))]
        if not locatable:
            out["无出处"].append(c)

        aid = c.get("agent_id") or ""
        # 「越界」= Agent 自己把可见性写成 global。`me:*`（人写的文档）不算越界——
        # 它是人写的东西，不是 Agent 越权。
        if c.get("visibility") == "global" and not (aid == MANUAL_AGENT
                                                    or aid.startswith(MANUAL_AGENT + ":")):
            out["作用域越界"].append(c)

        # 身份：`manual` 来自 overrides 的 propose，那是写入端，
        # 记下身份是它的责任；没记下就是真缺口。
        if c.get("agent_basis") == "recorded":
            continue
        if origin_system(c.get("origin") or "") == "manual":
            out["身份未记录"].append(c)
        else:
            out["身份为推导"].append(c)
    return out


def conflicts(claims: list[dict]) -> list[dict]:
    """同 subject+predicate、不同 value、都还 active → 冲突候选。"""
    groups: dict[tuple, list[dict]] = {}
    for c in claims:
        if not (c["subject"] and c["predicate"]) or c["status"] not in ACTIVE:
            continue
        groups.setdefault((_norm(c["subject"]), _norm(c["predicate"])), []).append(c)
    out = []
    for (subj, pred), group in groups.items():
        vals: dict[str, list[dict]] = {}
        pred = group[0]["predicate"]
        for c in group:
            vals.setdefault(norm_value(pred, c["value"]), []).append(c)
        if len(vals) <= 1:
            continue
        # 冲突等级：值里含路径/端口/版本/数字 → 可直接比对，高风险
        hard = any(re.search(r"(/\S+|\b\d{2,5}\b|v?\d+\.\d+)", c["value"]) for c in group)
        ranked = sorted(group, key=lambda c: (
            -EVIDENCE_RANK.get(c["evidence"], 0), c["valid_from"] or "0000"), reverse=False)
        out.append({
            "subject": group[0]["subject"], "predicate": group[0]["predicate"],
            "hard": hard, "ranked": ranked,
            "values": {v: len(g) for v, g in vals.items()},
        })
    out.sort(key=lambda x: (not x["hard"], -len(x["ranked"])))
    return out


def chain_problems(claims: list[dict], rule_ids: set[str]) -> dict[str, list]:
    """取代链的健康检查（2026-11-15）。

    ## 为什么要有这个

    分层办法 §8 的验收第 1 条写着「任意一条注入，能在 3 秒内答出：谁写的、
    凭什么、什么时候失效、**被谁取代过**」。前三个字段都有人读，唯独
    `superseded_by` 写进去以后**没有任何代码读过它**——写的时候记了，读的时候
    没人问，等于没记。这类问题不会报错，只会安静地给不出答案。

    ## 四项检查

    - `缺后任`：`status=superseded` 但 `superseded_by` 为空。链从这里断了，
      回答不了「被谁取代」。注意 `basis` 里往往写了人话（「当前等价路径 …」），
      但那是散文，不是指针，程序读不到。
    - `指向不存在`：`superseded_by` 在 claims 和 rules 两个命名空间里都找不到。
      跨命名空间是**合法**的（一条事实可以被一条规矩取代），所以两个都要查。
    - `成环`：沿链走下去回到起点。截断时若不检测，`while` 会挂住。
    - `多重活跃`：同一 (subject, predicate, value) 有多于一条 active 条目。
      **这不是缺陷，是跨源互证的证据**：`corroborate()` 会把同组所有条目一起
      升成 verified，两组来源各一条就是两条 active。
      （原先这里写「违反 §8 验收第 2 条」，2026-11-16 已把 §8 的措辞改成与实现一致：
      检索面上同一事实只出现一行，账本里允许多条 verified。
      仍单列出来，是因为它会吃掉检索名额——处理在 `merge_same_fact`。）
    - `残留旧值`：一条已被判 superseded 的**具体值**（只查路径/端口这类可逐字
      比对的），仍有多条 active 条目在说同一个值。这是 2026-11-15 实测发现的：
      把「无限画布在 /mnt/ubuntu-vhdx/...」标成 superseded 之后，问「无限画布在哪」
      仍会把旧路径排在前面返回——因为同一个旧路径另有一条 memt 抽取的散文条目
      还活着。**supersede 只作用于一个 id，不作用于「这个值」。**
    """
    ids = {c["id"] for c in claims}
    by_id = {c["id"]: c for c in claims}
    out: dict[str, list] = {"缺后任": [], "指向不存在": [], "成环": [],
                            "残留旧值": [], "多重活跃": []}

    link: dict[str, str] = {}
    for c in claims:
        if c.get("status") != "superseded":
            continue
        nxt = (c.get("superseded_by") or "").strip()
        if not nxt:
            out["缺后任"].append(c)
            continue
        if nxt not in ids and nxt not in rule_ids:
            out["指向不存在"].append(c)
            continue
        if nxt in ids:
            link[c["id"]] = nxt

    # 一个环只报一条代表（取环上 id 最小的那条）。逐节点各报一次会让
    # 一条 A↔B 的环数成 2 条——计数虚高，看的人会去修第二个"问题"。
    reported: set[str] = set()
    for start in link:
        path, cur = [start], start
        while cur in link:
            cur = link[cur]
            if cur in path:
                ring = path[path.index(cur):]
                rep = min(ring)
                if rep not in reported:
                    reported.add(rep)
                    out["成环"].append(by_id[rep])
                break
            path.append(cur)

    groups: dict[tuple, list[dict]] = {}
    for c in claims:
        if not (c["subject"] and c["predicate"]) or c["status"] not in ACTIVE:
            continue
        groups.setdefault((_norm(c["subject"]), _norm(c["predicate"]),
                           norm_value(c["predicate"], c["value"])), []).append(c)
    for g in groups.values():
        if len(g) > 1:
            out["多重活跃"].extend(g[:1])     # 每组只报一条代表，不把整组铺开

    # 残留旧值：只比对逐字可判的值（路径/端口/版本），散文不参与——
    # 否则一句"旧路径是 X"的长文也会被算成"仍在断言 X"，变成噪声。
    dead: list[tuple[str, str, str]] = []      # (id, value, 原始 claim)
    for c in claims:
        if c.get("status") != "superseded":
            continue
        v = (c.get("value") or "").strip()
        if len(v) >= 8 and re.search(r"(^/|^~|:\d{2,5}$|^v?\d+\.\d+)", v):
            dead.append((c["id"], v, c.get("claim") or ""))
    for dead_id, val, _ in dead:
        still = [c for c in claims
                 if c["id"] != dead_id and c["status"] in ACTIVE
                 and val in (c.get("value", "") + " " + c.get("claim", ""))
                 # 只报「这条主张说的就是这个路径」，不报「长文里提到过它」。
                 # 否则一段 400 字的能力描述里带了一个旧路径也会被算成仍在断言它，
                 # 而把它整条作废会连带丢掉描述里其它有用的东西。
                 and len((c.get("value") or "") + (c.get("claim") or "")) < 2 * len(val) + 80]
        for c in still:
            out["残留旧值"].append({"id": c["id"], "superseded_id": dead_id,
                                  "dead_value": val, "status": c["status"],
                                  "origin": c.get("origin", ""),
                                  "claim": c.get("claim", "")})

    return out


def chain_of(claims: list[dict], start: str) -> list[str]:
    """沿 `superseded_by` 往下走，返回 [起点, 后任, 后任的后任, …]。

    `superseded_by` 可能指向另一条 claim，也可能指向一条 rule（跨命名空间）。
    走到 claim 命名空间之外就停——规矩没有自己的取代链。
    """
    by_id = {c["id"]: c for c in claims}
    path, cur, guard = [start], start, 0
    while cur in by_id and guard < 64:
        nxt = (by_id[cur].get("superseded_by") or "").strip()
        if not nxt or nxt in path:
            break
        path.append(nxt)
        cur, guard = nxt, guard + 1
    return path
