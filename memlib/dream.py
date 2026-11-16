"""记忆整合（`mem dream`）：把报告里的候选，变成**待你点头的裁决草案**（2026-11-16）。

## 这套逻辑是从哪来的

2026-11-16 去 GitHub 看了几个做「记忆自动进化」的项目，取了三家的做法，改成了
适合本机的口径。原样照搬的那套在这台机器上不能要，理由写在下面每一条后面。

| 来源 | 它怎么做的 | 本机怎么改 | 为什么 |
|---|---|---|---|
| `tigerless-labs/agent-memory` | 睡眠期整合 + **权限分档**：T0 自动应用，T1 只提提案，无人值守永不删除 | 只保留 T1：**整合只产提案，一条都不自动写** | 本机的规矩是「Agent 不能直接写 L1，只许 propose」。自动应用等于把这条作废 |
| `BAI-LAB/MemoryOS` | 热度 `H=α·访问次数+β·交互长度+γ·时间衰减`，LFU 淘汰 | **不采用** | 本机没有访问日志；就算有，按热度淘汰等于让"最近没人问"变成"不重要"。账本里没人问的东西，恰恰可能是下个月要用的 |
| `BAI-LAB/MemoryOS` | 相似度超阈值自动合并 | 只合**检索层**（`merge_same_fact`），账本里不合 | 多来源说同一件事是证据，不是冗余；删了等于抹掉互证 |
| `mem0ai/mem0` | 写入时 ADD/UPDATE/DELETE/NOOP 由 LLM 判 | 本机已有的 `propose/verify/supersede/reject/stale` 就是这套操作集的**人工版** | 让模型在写入路径上判"这条该删"，训练数据都没法复核它判得对不对 |

## 为什么「只产提案」不是保守，是唯一说得通的

整合真正要解决的问题是：报告每周都在列同一批「路径不存在 / 端口打架 / 缺后任」，
**但没人处理**——因为它只报告问题，不给出「点一下就完事」的下一步。
`mem dream` 把每条候选翻译成一条完整的裁决草案（target + action + why + 依据），
人只需要 `--accept`。这样做的价值在于**降低复核成本**，而不是省掉复核。

如果它自动写了，代价是：判错的时候没人看得见（没人复核过），
而且 `overrides.jsonl` 里分不清哪条是人的判断、哪条是脚本的猜测。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from . import report as _report
from .schema import ACTIVE, EVIDENCE_RANK, norm as _norm
from .store import conflicts, origin_system
from .text import norm_value


# 本机真实存在的顶层目录。单段路径若不在这个表里，就不是路径。
STD_TOP = {"/tmp", "/home", "/run", "/mnt", "/media", "/opt", "/usr", "/var",
           "/etc", "/srv", "/boot", "/dev", "/proc", "/sys", "/root", "/bin",
           "/sbin", "/lib", "/lib64", "/snap", "/nix", "/data"}


def _looks_like_fragment(v: str) -> bool:
    """单段绝对路径且顶层目录不在标准表里 → 更像抽取切出来的片段，不像路径。

    ## 为什么要单独判（2026-11-16）

    第一次跑整合就给出了 `stale /unit`：原文是 "per /unit" 这种句子，
    抽取器把它当路径收进账本。给它标 `stale`（"曾经对、现在无法确认"）是错的说法——
    它**从来就不是一条路径**，该 `reject`。两个动作的区别不是措辞问题：
    `stale` 的条目仍然算"曾经成立"，`reject` 才是"这条主张本身是错的"。
    """
    s = v.strip().rstrip("/")
    if not s.startswith("/") or s.count("/") != 1:
        return False
    return s not in STD_TOP


def _pid(target: str, action: str) -> str:
    return "pp_" + hashlib.sha1(f"{target}\x1f{action}".encode()).hexdigest()[:12]


def _push(out: list[dict], seen: set, target: str, action: str, why: str,
          kind: str, basis: str = "") -> None:
    pid = _pid(target, action)
    if (target, action) in seen:
        return
    seen.add((target, action))
    out.append({"id": pid, "kind": kind, "target": target, "action": action,
                "why": why, "basis": basis, "status": "pending"})


def collect(claims: list[dict]) -> list[dict]:
    """从当前账本算出提案草案。纯函数：不读磁盘、不写任何东西。"""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    active = [c for c in claims if c.get("status") in ACTIVE]

    # ① 路径不存在 —— `report.path_check` 已经在报，但只列事实，不给动作
    _, _, missing = _report.path_check(active)
    for c in missing:
        v = c["value"]
        if _looks_like_fragment(v):
            # 根下没有这个顶层目录，且不在标准目录表里 → 十有八九是抽取切出来的片段
            # （实测 `/unit` 来自一句 "per /unit"，被当成路径收进了账本）
            _push(out, seen, c["id"], "reject",
                  f"看起来不是真路径（根下没有这个顶层目录）：{v[:120]}",
                  kind="抽取噪声", basis="标准顶层目录（/tmp /home /run /mnt …）之外的单段路径")
        else:
            _push(out, seen, c["id"], "stale",
                  f"路径不存在（2026-11-16 磁盘检查）：{v[:120]}",
                  kind="失效路径",
                  basis="外接盘未挂载 / 按需文件已清理 / 当时临时产物，三种都要人判")

    # ② 跨源冲突：只在一侧证据明显更弱时才提，两侧同强就交给人
    for g in conflicts(active):
        if not g["hard"]:
            continue
        vals: dict[str, list[dict]] = {}
        for c in g["ranked"]:
            vals.setdefault(norm_value(c["predicate"], c["value"]), []).append(c)
        if len(vals) < 2:
            continue
        strong = [c for c in g["ranked"]
                  if c["evidence"] in ("measured", "user") and c["status"] == "verified"]
        weak = [c for c in g["ranked"]
                if c["evidence"] in ("extracted", "inferred") and c["status"] != "verified"]
        if not strong or not weak:
            continue                      # 同强或同弱：不是脚本该裁的
        best = strong[0]
        # 只裁「值真的不同」的弱侧，且来源系统不同（同源互证不算数）
        for w in weak:
            if norm_value(w["predicate"], w["value"]) == norm_value(best["predicate"], best["value"]):
                continue
            if origin_system(w["origin"]) == origin_system(best["origin"]):
                continue
            _push(out, seen, w["id"], "reject",
                  f"与实测主张 {best['id']} 不符：本条 `{w['value'][:60]}` / "
                  f"实测 `{best['value'][:60]}`",
                  kind="跨源冲突", basis=f"依据 {best['id']}（{best['evidence']}/{best['origin']}）")

    # ③ 取代链缺后任 —— 链断了就答不出「被谁取代过」
    from .store import chain_problems
    prob = chain_problems(claims, set())
    for c in prob.get("缺后任", [])[:20]:
        _push(out, seen, c["id"], "supersede",
              "标了 superseded 但没有后任 id，链在这里断",
              kind="缺后任", basis="先用 `mem propose` 写入现行值，再 `mem supersede --by` 指过去")
    return out


def pending(claims: list[dict], path: Path) -> list[dict]:
    """已有提案文件里还 pending、且目标仍 active 的那些；顺带剔掉已被裁决过的。"""
    from .store import apply_overrides
    ruled = {c["id"]: c for c in claims}
    events = _events(path)
    latest: dict[str, dict] = {}
    for e in events:
        latest[e["id"]] = e
    out = []
    for p in collect(claims):
        ev = latest.get(p["id"])
        if ev and ev.get("status") != "pending":
            continue
        c = ruled.get(p["target"])
        if not c or c.get("status") not in ACTIVE:
            continue          # 已经被裁决/已经不在活跃面，提案作废
        # 人工已经用别的动作处理过这条（比如另加的 verify），就别再提
        hist = {h.get("action") for h in (c.get("state_history") or [])}
        if p["action"] in hist:
            continue
        out.append(p)
    return out


def _events(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    out = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def record(path: Path, proposals: list[dict]) -> int:
    """把本次仍 pending 的提案追加进提案文件（已经记录过的同 id 不重复写）。

    追加而不是覆盖：和 `overrides.jsonl` 同一个理由——提案也是判断，
    「这条第二次还成立」本身是信息。
    """
    known = {e["id"] for e in _events(path)}
    n = 0
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a", encoding="utf-8") as f:
        for p in proposals:
            if p["id"] in known:
                continue
            row = dict(p, at=datetime.now().isoformat(timespec="seconds"))
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            known.add(p["id"])
            n += 1
    return n


def mark(path: Path, pid: str, status: str, why: str = "") -> bool:
    """标 accepted / rejected（追加一行状态事件），并返回是否找到。"""
    ev = _events(path)
    if not any(e["id"] == pid for e in ev):
        return False
    row = {"id": pid, "status": status, "why": why,
           "at": datetime.now().isoformat(timespec="seconds")}
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def by_id(path: Path, pid: str) -> dict | None:
    rows = [e for e in _events(path) if e["id"] == pid]
    if not rows:
        return None
    base = next((r for r in rows if r.get("target")), {})
    latest = rows[-1]
    return dict(base, status=latest.get("status", base.get("status")))
