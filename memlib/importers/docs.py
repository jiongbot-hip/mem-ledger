"""我们自己写的人工文档 → claims（切块入库，一律 proposed）。

## 只收配置里列出的那些篇

不整棵 `~/记忆` 树：树下绝大多数 md 是灵枢的运行时数据，已经由 lingshu 导入器收过，
重复导入只会制造同一份东西的两个出处。

## 过程记录不是事实

两重排除，**都不靠"看结果回改配置"**：
1. 文件名前缀（`log_pat`）——早期只有这条路，且前缀写错会静默失效；
2. **文档自己声明**（`proc_mark`，头部 12 行内出现即排除）——改标题也不会漏。

2026-11-15 的教训：`灵枢-记忆分桶契约-` 末尾多一个 `-` 永不匹配，
258KB 修复流水 21 条 chunk 一直以「事实」身份躺在账本里。
分数上测不出来（从没进过任何一题的 top-8），但这违反分层办法 §1。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..schema import make_claim as _mk, mtime_date
from ..text import derive

NAME = "docs"


def _doc_paths(cfg) -> list[Path]:
    conf = cfg.importer(NAME)
    paths: list[Path] = []
    for d in conf.get("dirs", []):
        dp = Path(str(d)).expanduser()
        if dp.is_dir():
            paths.extend(sorted(dp.glob("*.md")))
    for f in conf.get("files", []):
        fp = Path(str(f)).expanduser()
        if fp.is_file():
            paths.append(fp)
    deny = list(conf.get("deny", [])) + list(conf.get("log_pat", []))
    mark = conf.get("proc_mark", "")
    out = []
    for p in paths:
        s = str(p)
        if any(bad in s for bad in deny):
            continue
        # 文档自称过程记录也排除。只看头部 12 行：
        # 正文里引用别处一句「> 过程记录」不该把整篇排除掉。
        if mark:
            try:
                head = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                                 .splitlines()[:12])
            except OSError:
                continue
            if mark in head:
                continue
        out.append(p)
    return out


def _chunk(text: str, limit: int) -> list[tuple[str, str]]:
    """按 Markdown 标题切段，超长的再按行累积。返回 (小标题, 正文)。"""
    blocks: list[tuple[str, str]] = []
    head, buf = "", []
    for ln in text.splitlines():
        if re.match(r"^#{1,4}\s", ln):
            if any(x.strip() for x in buf):
                blocks.append((head, "\n".join(buf).strip()))
            head, buf = ln.strip("# ").strip(), []
        else:
            buf.append(ln)
    if any(x.strip() for x in buf):
        blocks.append((head, "\n".join(buf).strip()))

    out: list[tuple[str, str]] = []
    for h, body in blocks:
        if len(body) <= limit:
            out.append((h, body))
            continue
        # 超长时按行累积：表格的每一行就是一条独立事实，不该和别的行粘在一起
        cur = ""
        for ln in body.splitlines():
            if cur and len(cur) + len(ln) + 1 > limit:
                out.append((h, cur.strip()))
                cur = ""
            cur += ln + "\n"
        if cur.strip():
            out.append((h, cur.strip()))
    return [(h, b) for h, b in out if len(b) >= 12]


def run(cfg) -> tuple[list[dict], dict]:
    out, stat = [], {"total": 0, "imported": 0}
    limit = int(cfg.importer(NAME).get("chunk", 300))
    home = str(Path.home())
    for p in _doc_paths(cfg):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(p).replace(home + "/", "~/")
        for i, (head, body) in enumerate(_chunk(text, limit), 1):
            stat["total"] += 1
            ref = {"kind": "doc", "path": rel, "heading": head[:80], "part": i}
            out.append(_mk(
                scope="global", subject="", predicate="", value=body[:900],
                claim=body[:900], status="proposed", evidence="extracted",
                confidence=0.6, owner="docs", origin="docs",
                origin_ref=f"{rel}#{i}",
                source_refs=[ref],
                note=f"人工文档段落：{head[:40]}",
            ))
            stat["imported"] += 1
            # 段落里可机械核对的路径/端口/版本/ctx 也单独成条，参与互证与路径复核
            fdate = mtime_date(p)   # 散文没有作者日期，只有文件 mtime（见 mtime_date 的说明）
            for ds, dp, dv in derive(body):
                out.append(_mk(
                    scope="global", subject=ds, predicate=dp, value=dv,
                    claim=body[:200], status="proposed", evidence="extracted",
                    confidence=0.6, owner="docs", origin=f"docs-rule:{dp}",
                    origin_ref=f"{rel}#{i}",
                    source_refs=[dict(ref, date_basis="mtime")],
                    valid_from=fdate,
                    note="规则抽取（非人工确认）",
                ))
    return out, stat
