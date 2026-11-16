"""账本的**数据契约**：一条 claim 长什么样、id 怎么算、什么叫合法。

## 为什么单独一个文件

在 2026-11-15 之前，"claim 有哪些字段"这件事只存在于 `_mk()` 的 kwargs 里。
于是：新导入器少写一个字段不会报错，字段顺序变了没人知道，
`agent_id` 空着也能落库。契约写在代码里，等于没有契约。

这里把三件事定死：
1. **字段集合与默认值**（`make_claim` 的模板）
2. **id 的派生规则**（`claim_id`）——外部工具可以照着算，不必 import 整个账本
3. **合法性**（`validate`）——缺字段、状态不认识、id 对不上，都要报出来

## id 不依赖路径

`claim_id = cl_ + sha1(scope, subject|predicate|value, origin)[:12]`，
`origin` 是来源**系统名**（`docs`、`systemd:unit-files`），不是文件路径。

这条是 2026-11-15 为搬家特意确认的：**把文档从 `20-账本/` 挪到 `系统/`，
id 不变、overrides 不受影响、评测分数不动**。路径只进 `origin_ref` 做溯源。
"""
from __future__ import annotations

import hashlib
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# 相对导入在"被当作包用"时有效；直接跑脚本时走兄弟模块路径。
try:
    from .redact import redact
except ImportError:                                    # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from redact import redact                          # type: ignore

# 参与检索的状态。其余（rejected/superseded/stale）视为不存在。
ACTIVE = {"proposed", "verified", "unresolved"}
ALL_STATUS = ACTIVE | {"rejected", "superseded", "stale"}

# 证据强度排序。数值版的权重在 config.toml 的 [search.evidence_weight]。
EVIDENCE_RANK = {"user": 5, "measured": 4, "registry": 3, "extracted": 2, "inferred": 1}

# 一条 claim 的完整字段表。顺序即落盘顺序，**改动会让 claims.jsonl 全量 diff**，
# 所以不是加字段就别动它。
TEMPLATE: dict = {
    "id": "", "scope": "global", "subject": "", "predicate": "", "value": "",
    "claim": "", "status": "proposed", "evidence": "extracted",
    "confidence": 0.5, "owner": "", "origin": "", "origin_ref": "",
    "source_refs": [], "valid_from": "", "invalid_at": None,
    "revalidate_after": None, "supersedes": [], "superseded_by": "", "note": "",
    "state_history": [],
    # 2026-11-15 P0（分层管理办法 §2）：身份与可见性。
    # 这三项由 annotate_identity() 统一填，这里只是占位保证字段顺序稳定。
    "agent_id": "", "agent_basis": "", "visibility": "",
    "source_session_ids": [],
}

REQUIRED = ("id", "scope", "subject", "predicate", "value", "status", "evidence", "origin")


def sha(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def norm(s: str) -> str:
    """比较用的归一化：去空白、全角标点、大小写。只用于判同，不改原值。"""
    if not s:
        return ""
    s = s.strip().lower()
    s = s.replace("　", " ")
    s = re.sub(r"\s+", "", s)
    s = s.replace("：", ":").replace("，", ",").replace("。", ".")
    return s


def norm_date(v) -> str:
    """把各种日期写法归一成 `YYYY-MM-DD`。认不出来就返回空串。

    ## 为什么需要这个（2026-11-16）

    lingshu 的 frontmatter 里 `created_at` 是**带小数的 Unix 时间戳**
    （`1793459835.57`）。导入器原来写 `str(...)[:10]`，把 `1793459835.57`
    截成 `1793459835`——**看起来像日期，实际是秒数**，107 条全是这样。

    `str[:10]` 对 `2026-11-15T10:00:00` 是对的，对 `1793459835.57` 是错的。
    这种错误不会报错，只会在算「多久以前」时算出天文数字。所以归一化收在这里，
    所有导入器共用一份——各写各的迟早又不一样。
    """
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    # Unix 时间戳（秒，可带小数）——判据是「纯数字且长度够」，
    # 不能只看前 4 位像不像年份：1793459835 的前 4 位 1793 也像年份。
    if re.fullmatch(r"\d{9,13}(\.\d+)?", s):
        try:
            ts = float(s)
            if ts > 1e11:            # 毫秒
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def mtime_date(path) -> str:
    """文件的最后修改日（`YYYY-MM-DD`）。读不到返回空串。

    ## 用在哪、为什么可以这么用（2026-11-16）

    从**散文**里抽出来的事实（记忆文件、文档）没有作者写的日期，
    唯一能拿到的时间戳就是文件 mtime。用它当 `valid_from` 有一个已知弱点：

    **它只会让事实显得更新，不会显得更旧**——改个错别字也会把整篇的日期推到今天，
    因此 TTL 可能**晚报**某条路径早已失效。

    但它和 `captured_at` 不是一回事：`captured_at` 每次 build 都归零（结构上永远不触发），
    mtime 只在人真的动了这个文件时动。所以它是一个粗但真实的上界。
    引用方必须把 `date_basis: mtime` 一起记下，读账本的人才知道这个日期是什么。
    """
    try:
        import os
        return datetime.fromtimestamp(os.stat(path).st_mtime).strftime("%Y-%m-%d")
    except (OSError, TypeError, ValueError):
        return ""


def claim_id(scope: str, subject: str, predicate: str, value: str, origin: str) -> str:
    if subject or predicate:
        key = f"{norm(subject)}|{norm(predicate)}|{norm(value)}"
    else:
        key = f"text|{norm(value)[:120]}"
    return "cl_" + sha(scope, key, origin)


def make_claim(**kw) -> dict:
    """按模板造一条 claim。脱敏在算 id 之前——否则带 token 的值会进 id。

    **必须复制可变字段**：`dict(TEMPLATE)` 是浅拷贝，
    `state_history`/`source_refs`/`supersedes`/`source_session_ids` 这几个 list
    会被全库 2117 条共享。2026-11-15 搬家时就是这么炸的：
    所有裁决都追加到同一个列表，每条 claim 都显示全部 11 条 state_history。
    oracle 逐条比对当场抓到——这也是为什么搬家前要先存一份旧账本。
    """
    claim = {k: (list(v) if isinstance(v, list) else v) for k, v in TEMPLATE.items()}
    claim.update(kw)
    for field in ("claim", "value", "subject", "predicate", "note", "origin_ref"):
        if isinstance(claim.get(field), str) and claim[field]:
            claim[field] = redact(claim[field])
    # source_refs 里的 id/session 也可能带凭据（2026-11-14 实测确实有），一并脱敏
    if claim.get("source_refs"):
        claim["source_refs"] = [
            {k: (redact(v) if isinstance(v, str) else v) for k, v in ref.items()}
            if isinstance(ref, dict) else redact(str(ref))
            for ref in claim["source_refs"]
        ]
    claim["id"] = claim["id"] or claim_id(
        claim["scope"], claim["subject"], claim["predicate"], claim["value"], claim["origin"])
    claim.setdefault("captured_at", date.today().isoformat())
    return claim


def validate(claim: dict) -> list[str]:
    """检查一条 claim 是否合契约。返回问题清单，空 = 合格。

    只在 `build --strict` 与自测里用；正常 build 不逐条校验（2117 条太慢），
    但导入器写完应当拿它跑一遍样例。
    """
    bad: list[str] = []
    for f in REQUIRED:
        if f not in claim:
            bad.append(f"缺字段 {f}")
    if claim.get("status") and claim["status"] not in ALL_STATUS:
        bad.append(f"状态不认识：{claim['status']!r}")
    if claim.get("evidence") and claim["evidence"] not in EVIDENCE_RANK:
        bad.append(f"证据等级不认识：{claim['evidence']!r}")
    cid = claim.get("id") or ""
    if not cid.startswith("cl_"):
        bad.append(f"id 不以 cl_ 开头：{cid!r}")
    elif claim.get("origin"):
        expect = claim_id(claim.get("scope", ""), claim.get("subject", ""),
                          claim.get("predicate", ""), claim.get("value", ""),
                          claim["origin"])
        if cid != expect:
            bad.append(f"id 与内容不符（应是 {expect}）")
    for f in ("supersedes", "source_refs", "source_session_ids", "state_history"):
        if not isinstance(claim.get(f, []), list):
            bad.append(f"{f} 应为列表")
    return bad
