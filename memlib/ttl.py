"""复核周期（TTL）：哪些主张该重新核实了（2026-11-16）。

分层办法 §5 写着「按谓词类型定 `revalidate_after`，到期标 stale，不删除」，
§7 P2 又写「到期自动标 stale，进日报」。**这两段一直是空话**——
`revalidate_after` 字段存在于 schema，但所有条目都是 `None`，没有任何代码读它。

## 这里做什么、不做什么

**做**：算出每条结构化主张该在什么时候复核，列出已经过期的。

**默认不做**：自动改状态。写操作要么由人显式打开（`[ttl.meta] auto_mark`），
要么根本不发生。理由：`stale` 意味着「检索时不再出现」，
自动把一批条目从检索面撤下来，等于机器替人做了判断。
先看清楚会撤掉什么，再决定要不要自动化。

## 为什么不是所有条目都有 TTL

只有「会随时间自然失效」的谓词才进表：路径会搬、端口会换、版本会涨。
「用户喜欢先看结论」这类偏好不会因为过了 180 天就错——给它 TTL 是制造噪声。
没在 `[ttl]` 里的谓词一律不参与，这是**默认不打扰**。
"""
from __future__ import annotations

from datetime import date

from .schema import ACTIVE, norm_date


def is_derived(claim: dict, prefixes) -> bool:
    """这条主张的来源是**每次构建都重读的机器可读源**吗（配置、命令输出）。

    ## 为什么这类条目不该有复核周期（2026-11-16）

    `registry:models.yaml` 的 96 条和 `systemd:timers` 的 7 条，值是每次 build
    从 `models.yaml` / `systemctl show` 现读的。问「这条还对不对」，
    重读一遍得到同样的答案——**TTL 在这里永远判不出东西**。

    把它们算成「日期不明」，等于用 103 条噪声把真正需要复核的 14 条埋掉。
    单独归一类，写清楚「不适用」，比混进「没法判断」诚实。

    ## 但这不等于它们没问题

    它们证明的是「配置这么写」，不是「机器实际如此」。配置和现实脱节
    （`ctx: 128k` 写着、服务只给 64k）TTL 一辈子发现不了——那要探活，不是复核。
    """
    o = (claim.get("origin") or "")
    return any(o.startswith(p) for p in prefixes)


def _stated_date(claim: dict) -> str:
    """这条主张**自己声称**的日期。只看 `valid_from`，不看 `captured_at`。

    ## 为什么不能用 captured_at 兜底（2026-11-16 抓到的真错误）

    `captured_at` 由 `make_claim` 在**构建当天**填（`date.today()`）。
    拿它当 TTL 的起点，等于每次重建都把时钟归零——**这些条目永远不会过期**。
    一个永远不会触发的检查，比没有检查更糟：它给人一种"已经管起来了"的错觉。

    实测：2073 条里 1871 条 `valid_from` 为空，全都只剩 `captured_at`。
    用兜底写法跑出来「已过期 0 条」，那个 0 是假的。

    所以：`valid_from` 为空 = 日期不明，单列出来，不猜。
    """
    return norm_date(claim.get("valid_from"))


def _age_days(claim: dict, today: date) -> int | None:
    """条目距今天数。算不出来返回 None（= 日期不明，不是 0）。"""
    d = _stated_date(claim)
    if not d:
        return None
    try:
        y, m, dd = (int(x) for x in d.split("-"))
        return (today - date(y, m, dd)).days
    except (ValueError, TypeError):
        return None


def due(claims: list[dict], ttl_map: dict[str, int],
        today: date | None = None, derived_prefixes=()) -> dict[str, list[dict]]:
    """四类结果，各自带原因。

    - `已过期`：超过周期，应当复核后重新确认（或标 stale）
    - `将到期`：还剩 1/4 周期以内，提前看一眼，别等到过期才动
    - `日期不明`：谓词有 TTL 但算不出日期——**这不是"没问题"**，
      是"没法判断"。混进已过期或悄悄跳过都不对，单列。
    - `实时派生`：来源每次构建重读（配置/命令输出），TTL 不适用，见 `is_derived`。
    """
    today = today or date.today()
    out: dict[str, list[dict]] = {"已过期": [], "将到期": [], "日期不明": [], "实时派生": []}
    for c in claims:
        pred = (c.get("predicate") or "").strip()
        if pred not in ttl_map:
            continue
        if c.get("status") not in ACTIVE:
            continue          # 已经 rejected/superseded/stale 的不再催复核
        if is_derived(c, derived_prefixes):
            out["实时派生"].append({"claim": c, "predicate": pred})
            continue
        limit = int(ttl_map[pred])
        age = _age_days(c, today)
        if age is None:
            out["日期不明"].append({"claim": c, "predicate": pred, "limit": limit})
            continue
        if age > limit:
            out["已过期"].append({"claim": c, "predicate": pred, "limit": limit,
                                "age": age, "over": age - limit})
        elif age > limit * 3 // 4:
            out["将到期"].append({"claim": c, "predicate": pred, "limit": limit,
                                "age": age, "left": limit - age})
    # 过期最久的排最前——那些最可能是"东西早就没了但没人管"
    out["已过期"].sort(key=lambda x: -x["over"])
    out["将到期"].sort(key=lambda x: x["left"])
    return out


def revalidate_after(claim: dict, ttl_map: dict[str, int], derived_prefixes=()) -> str:
    """这条主张该在哪天复核。算不出来（或不适用）返回空串。

    写进 claim 的 `revalidate_after` 字段，让「什么时候该看」跟着条目走，
    而不是每次重算——重算依赖「今天是几号」，同一份账本不同天构建会 diff。
    """
    pred = (claim.get("predicate") or "").strip()
    limit = ttl_map.get(pred)
    if not limit:
        return ""
    if is_derived(claim, derived_prefixes):
        return ""          # 每次构建重读，没有"该复核日"这回事
    d = _stated_date(claim)
    if not d:
        return ""
    try:
        y, m, dd = (int(x) for x in d.split("-"))
        from datetime import timedelta
        return (date(y, m, dd) + timedelta(days=int(limit))).isoformat()
    except (ValueError, TypeError):
        return ""
