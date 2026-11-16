"""原子落盘：唯一临时名 + fsync + `os.replace`。

## 为什么不用固定的 `.tmp`

2026-11-17 之前 build 写的是 `claims.jsonl.tmp`。三个入口都可能同时跑：
每日 04:20 的 `mem-ledger.timer`、15 分钟一次的 `mem-ledger-sync.timer`、手工敲的
`mem build`。三者写同一个临时文件，后启动的那个会把前一个写到一半的内容覆盖掉，
最后 `replace` 出来的是**混合账本**——不是"其中一个赢了"，是两份都不是。

唯一临时名把这一层消掉了：每个进程写自己的文件，`os.replace` 是原子的，
账本要么是 A 的完整结果、要么是 B 的完整结果。

## 这一层不做的：全局互斥

唯一临时名不阻止"后写完的覆盖先写完的"。对账本来说这仍然正确——build 幂等、
输入是同一批来源，谁赢都对。**不该在这里加锁**：加了会让定时器和手工命令互相等待，
而 04:20 和 15 分钟两个定时器本来就该错开，那是部署策略（见 `mem-ledger-sync.service`
的 `ExecCondition` 与 `flock`），不是库的职责。

## fsync 不是摆设

不 fsync 的话 `replace` 只保证目录项原子，断电后可能得到一个空文件或旧内容。
账本是可重建的，但 `overrides.jsonl` 是人手写的裁决，**重建不出来**。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def replace_text(path: Path | str, text: str, *, fsync: bool = True) -> None:
    """把 `text` 原子地写成 `path`。目录不存在会建。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 写失败不能留下半截临时文件（下次 build 的 glob / 备份会把它当账本）
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_jsonl(path: Path | str, rows, *, fsync: bool = True) -> None:
    """逐行写 JSON 再原子替换。行分隔符固定 `\\n`，不随平台变。"""
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    replace_text(path, body, fsync=fsync)


def append_jsonl(path: Path | str, rows) -> int:
    """只追加。返回写入条数。

    **不替换整文件**是刻意的：`overrides.jsonl` 一类的只追加日志，
    用读-改-写会让并发写者丢行。追加模式由内核保证单次 `write` 不交错，
    前提是每条一行且小于 PIPE_BUF（4096）——调用方要保证这一点。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
        f.flush()
        os.fsync(f.fileno())
    return n
