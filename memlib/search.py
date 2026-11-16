"""检索：门控 → 宽召回 → 噪声门 → 重排 → top-k。

## 参数全部来自 config.toml

`b`、`k1`、重排三权重、证据权重、同义词都在 `config.toml`。这里只放算法。
改参数前先看 `eval-gated-changes`：**在调参集上试出来的提升不算证据**，
只有留出集逐条不降才收；而且要查 top1 多样性，防止"长条目靠什么词都有抢光 top-8"。

## 事实与指令一起检索，但保留 layer

`rules.jsonl` 是"以后要怎么做"，`claims.jsonl` 是"现在是什么"，分开存、合并检索。
注入串带 `layer=rule|fact`，读的人能分清是指令还是事实。
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from . import scope
from .schema import ACTIVE, norm as _norm
from .text import norm_value

CJK = re.compile(r"[一-鿿]+")
ASCII = re.compile(r"[A-Za-z0-9_./:~-]{2,}")


class SearchConf:
    """从 config 抽出检索要用的那几项，避免把整个 Config 传进每一层。"""

    def __init__(self, cfg):
        self.k1 = float(cfg.search("k1"))
        self.b = float(cfg.search("b"))
        self.min_text = int(cfg.search("min_text"))
        self.evidence_w = cfg.evidence_weight()
        self.rerank_w = cfg.rerank_weights()
        self.rule_boost = cfg.rule_boost()
        self.synonyms = cfg.synonyms()
        self.surf_to_canon = {s: c for c, forms in self.synonyms.items() for s in forms}
        # 中文二元组的跨词垃圾（「的端」「口是」「是多」「多少」）会把真信号淹掉。
        self.stop_chars = set("的了是在和有就也这那我你他她它个上下吧呢吗啊呀哦嘛什怎么多少一不没要会能")
        self.claims_path = cfg.path_of("claims")
        self.rules_path = cfg.path_of("rules")

    # 状态集是契约的一部分，不随机器变
    drop_status = frozenset({"rejected", "superseded", "stale"})


class Index:
    def __init__(self, claims: list[dict], cf: SearchConf, reader: str = scope.READ_ALL):
        self.docs: list[dict] = []
        self.tf: list[Counter] = []
        self.reader = reader
        self.cf = cf
        self.dropped_scope = 0
        for c in claims:
            if not scope.visible(c, reader):
                self.dropped_scope += 1
                continue
            if not gate(c, reader, cf):
                continue
            self.docs.append(c)
            self.tf.append(Counter(doc_tokens(c, cf)))
        df: Counter = Counter()
        for tf in self.tf:
            df.update(tf.keys())
        n = max(1, len(self.tf))
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.avgdl = sum(sum(tf.values()) for tf in self.tf) / n if self.tf else 1.0
        # 实体门用「账本自己出现过的主语」当词典，而不是拿查询里 idf 最高的词当实体。
        # 后者会选中「本每」「张账」这类跨词垃圾二元组（它们稀少 → idf 高 → 被当成实体），
        # 结果把真答案罚下去、把乱撞的抬上来。
        self.entities: dict[str, set[str]] = {}
        for c in self.docs:
            subj = (c.get("subject") or "").strip()
            if len(subj) >= 2:
                self.entities.setdefault(subj.lower(), set(tokens(subj, cf)))

    def score(self, q: list[str], i: int) -> float:
        """BM25-lite。`b` 与 `k1` 来自 config（见 config.toml 里为什么是 0.4）。"""
        tf = self.tf[i]
        dl = sum(tf.values()) or 1
        k1, b = self.cf.k1, self.cf.b
        s = 0.0
        for t in q:
            f = tf.get(t)
            if not f:
                continue
            idf = self.idf.get(t, 0.0)
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / self.avgdl))
        return s


_HOME = str(Path.home())


def tokens(s: str, cf: SearchConf) -> list[str]:
    """中文取字符二元组（单字串退化为单字），ASCII 取词；同义词并到一个规范 token。"""
    # 家目录归一成 `~`：账本里同一个路径可能写成 `/home/xxx/...` 或 `~/...`，
    # 不归一就会变成两个 token。**取 `Path.home()` 不写死**——写死的话换台机器
    # 这一整行就是死代码（2026-11-17 之前是 `/home/me`）。
    s = (s or "").replace(_HOME, "~")
    out: list[str] = []
    runs = CJK.findall(s)
    for run in runs:
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(g for g in (run[i:i + 2] for i in range(len(run) - 1))
                       if not any(ch in cf.stop_chars for ch in g))
        for surf, canon in cf.surf_to_canon.items():
            if surf in run:
                out.append(canon)
    for w in ASCII.findall(s):
        w = w.lower()
        out.append(cf.surf_to_canon.get(w, w))
        if "/" in w:
            tail = w.rstrip("/").rsplit("/", 1)[-1]
            if len(tail) >= 2:
                out.append(tail)
        # `memt-daily.timer` 要能匹配到 `memt`：不拆连字符的话，问「memt 几点跑」
        # 永远匹配不上文档里写的 `memt-daily`
        for part in re.split(r"[-_.]", w):
            if len(part) >= 2:
                out.append(part)
    return out


def doc_text(c: dict) -> str:
    return " ".join(x for x in (c.get("subject"), c.get("predicate"), c.get("value"),
                                c.get("claim")) if x)


def doc_tokens(c: dict, cf: SearchConf) -> list[str]:
    """主语与谓词是「这条在说什么」的强信号，重复一次当加权。"""
    head = tokens(" ".join(x for x in (c.get("subject"), c.get("predicate")) if x), cf)
    return tokens(doc_text(c), cf) + head


def gate(c: dict, reader: str, cf: SearchConf) -> bool:
    """噪声门：作用域、状态、出处、长度。"""
    if not scope.visible(c, reader):
        return False
    if c.get("status") in SearchConf.drop_status:
        return False
    if not c.get("source_refs"):
        return False
    # 注意：不能拿 value 单独判长度，`q30 port 8085` 的 value 只有 4 个字符会被误杀。
    if len(doc_text(c).strip()) < cf.min_text:
        return False
    return True


def freshness(c: dict, days: float = 180.0, floor: float = 0.5) -> float:
    """线性衰减到 floor，再久不再掉（旧事实不等于错事实）。"""
    d = c.get("valid_from") or c.get("captured_at") or ""
    if not d:
        return floor
    try:
        y, m, dd = (int(x) for x in str(d)[:10].split("-"))
        age = (date.today() - date(y, m, dd)).days
    except (ValueError, TypeError):
        return floor
    return max(floor, 1.0 - (1.0 - floor) * age / days)


def rerank(idx: Index, cand: list[tuple[int, float]], top_k: int) -> list[tuple[dict, float]]:
    if not cand:
        return []
    cf = idx.cf
    w_rel, w_ev, w_fresh = cf.rerank_w
    mx = max(s for _, s in cand) or 1.0
    out = []
    for i, s in cand:
        c = idx.docs[i]
        ev = cf.evidence_w.get(c.get("evidence", ""), 0.4)
        final = w_rel * (s / mx) + w_ev * ev + w_fresh * freshness(c)
        # 2026-11-17：规矩（layer=rule）是同一条规矩的**权威版本**，
        # 文档里复述冲突的那一段只是过程记录。规则题该让规矩自己排前面。
        # 值在 config.toml 的 search.rule_boost，不写死在这里。
        if cf.rule_boost and (c.get("layer") or "fact") == "rule":
            final += cf.rule_boost
        out.append((c, final))
    out.sort(key=lambda x: (-x[1], x[0].get("id", "")))
    return out[:top_k]


def _fact_key(c: dict) -> tuple | None:
    """判「是不是同一件事」的键。返回 None 表示这条不参与合并。

    只有结构化的三元组才合并：主语、谓词都非空，且是 claims 层。
    规矩（layer=rule）与散文条目（没有主语）不合并——它们本来就是一句一段，
    合并会把两段不相干的话糅在一起。
    """
    if (c.get("layer") or "fact") != "fact":
        return None
    subj, pred = (c.get("subject") or "").strip(), (c.get("predicate") or "").strip()
    if not (subj and pred):
        return None
    return (_norm(subj), _norm(pred), norm_value(pred, c.get("value") or ""))


def merge_same_fact(rows: list[tuple[dict, float]]) -> list[tuple[dict, float]]:
    """把同一事实的多来源条目合并成一条，其余来源记在 `also_from` 里。

    ## 为什么在检索层合，不在账本里合

    多条来源说同一件事**不是冗余，是证据**：`corroborate()` 正是靠「≥2 个独立来源」
    才把条目升为 verified 的。在账本里删掉重复项，等于把这层证据抹了。
    真正的问题是**检索名额被吃掉**：查「q30 端口」，6 个名额里 4 个是同一条 8085
    的四个来源，只剩 2 个名额答别的事。

    所以只在这里合并显示，账本不动、索引不动、打分不动——
    排名还是按原样算，合并发生在取 top-k 之前，省下来的名额自动让给下一条。
    这样最大的风险面（BM25 参数）完全没碰。

    ## 保留哪一条

    保留排名最高的那条（也就是第一个遇到的），把其余来源挂到它的 `also_from`。
    不按 evidence 等级挑：排名已经综合过 evidence，再挑一次等于重复计权。
    """
    if not rows:
        return rows
    out: list[tuple[dict, float]] = []
    pos: dict[tuple, int] = {}
    for c, s in rows:
        key = _fact_key(c)
        if key is None or key not in pos:
            if key is not None:
                pos[key] = len(out)
            out.append((c, s))
            continue
        # 合并到已有的那条上。**复制一份再改**：claims 里的 dict 是索引共享的，
        # 直接写 `c["also_from"]` 会把标记泄进下一次检索。
        i = pos[key]
        base, bs = out[i]
        base = dict(base)
        base.setdefault("also_from", [])
        base["also_from"] = base["also_from"] + [{
            "id": c["id"], "agent": c.get("agent_id") or "-",
            "origin": c.get("origin") or "", "visibility": c.get("visibility"),
        }]
        out[i] = (base, bs)
    return out


def search(idx: Index, query: str, k: int = 8, wide: int = 40) -> list[tuple[dict, float]]:
    q = tokens(query, idx.cf)
    if not q:
        return []
    # 实体门：查询里点名的账本主语，文档必须也提到。
    # 不加这道，问「q30 的端口」会被一堆只提过「模型/端口」的条目挤掉——两个中等词
    # 的和能压过一个高区分度词，这是 BM25 求和的固有毛病。
    qn = (query or "").lower()
    hit_ents = [e for e in idx.entities if e in qn]
    scored = [(i, idx.score(q, i)) for i in range(len(idx.docs))]
    if hit_ents:
        need = [idx.entities[e] for e in hit_ents]
        scored = [
            (i, s * (1.0 if all(any(t in idx.tf[i] for t in terms) for terms in need) else 0.45))
            for i, s in scored
        ]
    scored = [x for x in scored if x[1] > 0]
    scored.sort(key=lambda x: (-x[1], idx.docs[x[0]].get("id", "")))
    # 先重排一个**更宽**的候选池，再合并同事实条目，最后切 k。
    # 顺序不能反：先切 k 再合并，同一事实的四条还是吃掉四个名额，
    # 合并空出来的位置不会让给下一条。
    wide_k = max(wide, k * 4)
    ranked = rerank(idx, scored[:wide], wide_k)
    return merge_same_fact(ranked)[:k]


def fmt(c: dict) -> str:
    """注入格式（§4）：每条自带 id/layer/status/evidence/日期/来源/agent/可见性。

    字段顺序**不许动**：评测按 `text` 做子串匹配，改顺序会让金标锚点漂移。
    2026-11-15 补了 `head`（主语：谓词）挂在正文前——结构化条目原来只打 value，
    注入串成了光秃秃的 `18090`，读的人不知道这是谁的 18090。
    """
    src = ""
    refs = c.get("source_refs") or []
    if refs and isinstance(refs[0], dict):
        src = refs[0].get("session") or refs[0].get("id") or ""
    # 日期分两种，**不能混着显示**（2026-11-16）：
    # `valid_from` 是这条事实自己声称的生效日期；`captured_at` 是**构建进账本的那天**。
    # 原来 `valid_from or captured_at` 让 1871 条没有日期的条目全都显示成"今天是它的日期"，
    # 读的人会以为「2026-11-16 的记录」有意义——其实只是今天跑了一次 build。
    # 没有声称日期就打 `?`：读者立刻知道这个日期不是事实的一部分。
    _vf = str(c.get("valid_from") or "")[:10]
    day = _vf or (str(c.get("captured_at") or "")[:10] + "?" if c.get("captured_at") else "")
    layer = c.get("layer") or "fact"
    agent = c.get("agent_id") or "-"
    vis = c.get("visibility") or "shared"
    head = " ".join(x for x in (c.get("subject"), c.get("predicate")) if x).strip()
    body = c.get("value") or c.get("claim")
    # 合并标记挂在**末尾**：前面那些字段的位置是评测锚点，动了会让金标匹配漂移。
    # 合并后的条目必须说清楚「还有谁也在说同一件事」，否则读者会把
    # 「只有一个来源」和「四个来源一致」当成一回事。
    also = c.get("also_from") or []
    tail = ""
    if also:
        who = "、".join(x.get("agent") or "-" for x in also)
        tail = f"（另有 {len(also)} 个来源一致：{who}）"
    return (f"[{c['id']} | {layer} | {c['status']} | {c['evidence']} | {day} | src={src}"
            f" | agent={agent} | vis={vis}] "
            f"{head + '：' if head else ''}{body}{tail}")


# ---------- 读取两层 ----------

def load_claims(cf: SearchConf) -> list[dict]:
    """主账本 + 年度分片。分片只是"挪了个文件"，检索**必须**照样看得见。

    顺序：主账本在前、分片在后；同 id 时先出现的胜（主账本是最新构建结果），
    这样原有的条目顺序完全不变——2026-11-15 搬家验收要求「逐字相同」，
    加分片不该动到任何一条的排名。依据 `10-规则与方案/记忆增长与留存策略-2026-11-17.md`。
    """
    p = cf.claims_path
    paths = [p] + sorted(p.parent.glob("claims-archive-*.jsonl"))
    if not any(x.exists() for x in paths):
        print(f"账本不存在：{p}", file=sys.stderr)
        return []
    by_id: dict[str, dict] = {}
    n = 0
    for x in paths:
        if not x.exists():
            continue
        for ln in x.read_text(errors="replace").splitlines():
            if not ln.strip():
                continue
            c = json.loads(ln)
            n += 1
            by_id[str(c.get("id") or f"#{n}")] = c
    return list(by_id.values())


def load_rules(cf: SearchConf, include_heldout: bool = True) -> list[dict]:
    """规矩（指令层）单独成库，检索时与事实层合并，但保留 layer 标记。

    2026-11-14：规矩不进 claims.jsonl——它是「以后要怎么做」，不是「现在是什么」。
    """
    p = cf.rules_path
    if not p.exists():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("status") in ("rejected", "superseded"):
            continue
        if not include_heldout and r.get("heldout"):
            continue
        out.append(r)
    return out


def rule_as_doc(r: dict) -> dict:
    """把一条规矩投影成检索文档，字段对齐 claim，便于共用一套检索。"""
    return {
        "id": r["id"], "layer": "rule",
        "scope": f"rule:{r.get('domain', 'general')}",
        "subject": r.get("domain", ""), "predicate": "规矩",
        "value": r["rule"], "claim": r["rule"] + " " + (r.get("source_quote") or ""),
        "status": r.get("status", "proposed"), "evidence": r.get("evidence", "user"),
        "valid_from": r.get("captured_at", ""),
        # 规矩是炅先生对全体 Agent 下的指令，本来就是要所有 Agent 都遵守的，
        # 所以是 shared 而不是 private。**必须显式写**：可见性缺失一律拒绝，
        # 不显式写的话这 41 条会被读路径第①步全部过滤掉（2026-11-15 实测）。
        "visibility": "shared", "agent_id": "me",
        "source_refs": [{"kind": "rule", "session": r.get("session", ""),
                         "quote": (r.get("source_quote") or "")[:80]}],
        # 2026-11-16：把 heldout 标出来。它不进 doc_text（不参与打分），
        # 但读侧需要知道「这条是评测留出目标」——/rules 靠它默认过滤，
        # 不用再回源文件按 id 反查。
        "heldout": bool(r.get("heldout")),
    }


def as_of_filter(docs: list[dict], day: str) -> tuple[list[dict], dict]:
    """只留「在 `day` 那天成立」的条目，回答「那天我们以为是什么」。

    ## 这条规矩抄自 agent-memory 的 `recall --as-of`

    它的说法是：更新不销毁历史，所以能回答「那天的真相」。本机的账本本来就有
    `valid_from` / `invalid_at` / `superseded_by` / `state_history`，
    却没有一个查询读它们——**存了不等于能问**。

    ## 判据，以及它不完美的地方

    1. `valid_from` 晚于 as-of → 那天还没成立，排除。
    2. `invalid_at` 早于等于 as-of → 那天已失效，排除。
    3. 状态不是 ACTIVE 的（rejected/superseded/stale）：看 `state_history`
       里那条状态变更的时间——晚于 as-of 就说明"当时还是活跃的"，保留。
    4. **看起来活跃、但其实没有历史记录**：本机 1871 条 `valid_from` 为空
       （§16 实测），所以"那天成立吗"对它们只能靠状态变更时间；
       两者都没有时，**保留并计入 `日期不明`**，而不是猜一个。

    返回 (保留的条目, 统计)。统计里的数要打出来：as-of 查询的结论强度
    完全取决于有多少条其实答不了。
    """
    kept, st = [], {"排除_尚未成立": 0, "排除_已经失效": 0,
                    "排除_当时已作废": 0, "保留": 0, "日期不明": 0}
    for c in docs:
        vf = (c.get("valid_from") or "")[:10]
        inv = (c.get("invalid_at") or "")[:10]
        if vf and vf > day:
            st["排除_尚未成立"] += 1
            continue
        if inv and inv <= day:
            st["排除_已经失效"] += 1
            continue
        if c.get("status") not in ACTIVE:
            # 状态变更发生在 as-of 之后 → 那天它还活着
            when = ""
            for h in reversed(c.get("state_history") or []):
                if h.get("at"):
                    when = str(h["at"])[:10]
                    break
            if when and when > day:
                kept.append(c)
            else:
                st["排除_当时已作废"] += 1
            continue
        if not vf:
            st["日期不明"] += 1
        kept.append(c)
    st["保留"] = len(kept)
    return kept, st


def load_all(cf: SearchConf, use_rules: bool = True,
             include_heldout: bool = True) -> list[dict]:
    """事实层 + 指令层。rules 放在后面只是为了 stats 好读，打分不受顺序影响。"""
    docs = load_claims(cf)
    if use_rules:
        docs = docs + [rule_as_doc(r) for r in load_rules(cf, include_heldout)]
    return docs
