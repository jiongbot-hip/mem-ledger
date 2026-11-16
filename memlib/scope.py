"""作用域与身份规则 —— 唯一一份（2026-11-15）

写路径（`mem-ledger.py`）和读路径（`memory-search.py`）都必须用同一套判断：
一条 claim 谁能读，写的时候和读的时候答案不一致，整套可见性就是自相矛盾的。

之所以单独抽一个文件：这两边各写一份，迟早一边改了另一边没改。
本项目刚刚因为「脱敏规则被复制成两份」漏掉一条真实 API key
（见 `~/记忆/10-规则与方案/账本身份与可见性-P0-2026-11-15.md`），
同一个错误不重复犯。
"""
from __future__ import annotations

MEMORY_ADMIN = "me"      # 记忆管理员：人，可读全部
READ_ALL = "all"         # 显式声明"以管理员身份读"，评测基线用
VIS_KNOWN = ("private", "shared", "global")


def valid_agent_id(a: str) -> bool:
    """`claude-code:main`、`dsh:tauri`、`team:video`、`me` 合法；
    空串、带空白、超过两段一律不合法——身份是权限的依据，不接受含糊值。"""
    if not a or any(ch.isspace() for ch in a) or a.count(":") > 1:
        return False
    return all(part for part in a.split(":"))


def valid_visibility(v: str) -> bool:
    if v in VIS_KNOWN:
        return True
    return v.startswith("team:") and len(v) > 5 and ":" not in v[5:]


def visible(c: dict, reader: str) -> bool:
    """这个 reader 能不能读这条 claim（分层管理办法 §2）。

    | visibility | 读 |
    |---|---|
    | private   | 仅写入者本人（agent_id 相同） |
    | shared    | 所有 Agent |
    | global    | 所有 Agent（写入侧另由 §2 限制为仅人确认） |
    | team:<n>  | 该团队 + 写入者本人 |
    | 缺失/未知 | **拒绝**（默认拒绝，不默认放开） |

    记忆管理员（`me`）与 `READ_ALL` 可读全部。

    注意：存量 2081 条全是 `shared`，所以现在这一层对所有 Agent 都是全放行。
    这是事实，不是疏漏——可见性要起作用，得先有写入端产出 `private`/`team:*`。
    """
    if reader in (READ_ALL, MEMORY_ADMIN):
        return True
    vis = (c.get("visibility") or "").strip()
    if vis in ("shared", "global"):
        return True
    writer = (c.get("agent_id") or "").strip()
    if vis == "private":
        return bool(writer) and writer == reader
    if vis.startswith("team:"):
        team = vis[5:]
        if reader == f"team:{team}" or reader.startswith(f"team:{team}:"):
            return True
        return bool(writer) and writer == reader
    return False
