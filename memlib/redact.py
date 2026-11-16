"""脱敏闸门：全系统**只有一份**规则，这里只负责找到它。

## 为什么这么写

2026-11-15 之前账本自带一份 6 条规则，漏了 11-14 补上的讯飞 `ak-`、微信 bot、
飞书字段格式，实测把**一条真实讯飞 key 明文写进了 claims.jsonl**。
教训：脱敏规则只能有一份，复制一份就是迟早漏一份。

所以本模块**不实现规则**，只按顺序去找那份唯一的 `_memredact.py`。
它的真身在 `workspace/scripts/`（那是别人维护的真身，我们不搬它、只引用）。

## fail closed

找不到就抛错、拒绝构建账本。少几条规则也能跑，正是 bug 藏进去的方式。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from . import config as _config


def _candidates() -> list[Path]:
    """按优先级列出候选位置。第一项是真身，第二项是本包内的副本（若有）。"""
    out: list[Path] = []
    try:
        ws = _config.load().path_of("workspace")
        out.append(ws / "scripts" / "_memredact.py")
    except (FileNotFoundError, KeyError):
        pass
    out.append(Path(__file__).resolve().parent / "_memredact.py")
    return out


def _load():
    cands = _candidates()
    for p in cands:
        if p.exists():
            spec = importlib.util.spec_from_file_location("_memredact", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.redact
    raise RuntimeError(
        "找不到 _memredact.py（脱敏闸门）。拒绝构建账本："
        "宁可不出账本，也不出带明文凭据的账本。候选路径：" + "、".join(map(str, cands)))


redact = _load()


def redact_self_test() -> bool:
    """闸门自检：至少证明它在拦，而不是空转。

    载荷在运行时拼装，不写成字面量——否则 `.pyc` 的常量表里会留一串
    "像 key 的字符串"，每次凭据扫描都把它报成命中（2026-11-15 实测如此）。
    """
    probe = "".join(["sk-"] + ["A1b2C3d4"] * 5)
    return "[已脱敏" in redact(f"key={probe}")
