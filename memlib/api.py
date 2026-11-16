"""只读记忆 API（2026-11-16）：让别的 Agent 查账本，不必 import `memlib`。

## 为什么是只读

账本有五个写入口（`propose` / `verify` / `supersede` / `reject` / `stale`），
每一个都要求写清「凭什么」。HTTP 请求里那个「凭什么」最容易退化成一句
"agent 说它查过了"。所以第一版**没有任何写端点**：读走 HTTP，写仍走 CLI，
写的人（和留痕的 `actor`）是同一个。

第二个理由：只读服务可以放心地长跑。一旦有写，它就得处理重放、并发、
幂等——那是另一套系统，不是这个账本该背的东西。

## 身份：每个 token 对应一个 agent_id

`token` 在 `~/.config/mem-api/tokens.toml`（**故意放在 `~/记忆` 之外**：
`系统/` 整个会进每日备份包，凭据放进去等于跟着包到处走）。
可见性判断走 `memlib.scope.visible()`，与 CLI、与构建同**一份**规则——
本机已经因为"脱敏规则复制成两份"漏过一次真 key，可见性不再重复这个错。

## 越权读为 0 是怎么做到的

- 每个 reader 有自己的 `Index`，`Index` 在构建时就丢弃读不到的条目；
  不是"查完再过滤"——查完再过滤会把条数、分数、名额都泄露出去。
- `GET /claim/<别人的 private id>` 返回 **404 而不是 403**：
  403 等于确认"这条存在，只是你看不到"。
- 日报里含全体条目，`/report/today` 只对 `me`（记忆管理员）开放。

## 缓存

账本每天 04:20 重建。每次请求 `stat` 一次 `claims.jsonl` 的 mtime，
变了就重建对应 reader 的索引。**不重启服务**——重启一个只在读的服务，
唯一效果是让别的 agent 的连接断一下。
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as _config
from . import scope as _scope
from . import search as _search
from .redact import redact as _redact
from .schema import norm_date as _norm_date

TOKENS_DEFAULT = Path("~/.config/mem-api/tokens.toml")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


# ------------------------------------------------------------------ token

def load_tokens(path=None) -> dict[str, str]:
    """token → agent_id。

    **fail closed**：文件不存在、解析不了、agent_id 不合法，一律抛错。
    没有 token 表就当"没人能读"，绝不退化成"谁都能读"。
    """
    p = Path(os.path.expanduser(str(path or TOKENS_DEFAULT)))
    if not p.exists():
        raise FileNotFoundError(
            f"没有 token 表：{p}。拒绝启动——没有 token 表就等于谁都能读。\n"
            f"建法见 `系统/README.md` 的「记忆 API」一节。")
    import tomllib
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise RuntimeError(f"token 表读不了：{p}：{e}")
    rows = raw.get("token") or []
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"token 表里没有 [[token]] 条目：{p}")
    out: dict[str, str] = {}
    for i, r in enumerate(rows):
        tok = str((r or {}).get("value") or "").strip()
        agent = str((r or {}).get("agent") or "").strip()
        if not tok or not agent:
            raise RuntimeError(f"第 {i + 1} 个 token 缺 value 或 agent：{p}")
        if not _scope.valid_agent_id(agent):
            raise RuntimeError(f"agent_id 不合法：{agent!r}（见 scope.valid_agent_id）")
        if tok in out:
            raise RuntimeError(f"token 重复（第 {i + 1} 个）：{p}")
        out[tok] = agent
    return out


def whoami(tokens: dict[str, str], presented: str) -> str:
    """把请求头里的 token 换成 agent_id；认不出返回空串。

    遍历全部 token 逐个 `compare_digest`，不用字典直查：
    直查的命中/未命中耗时差可以用来逐位猜 token。
    """
    if not presented:
        return ""
    hit = ""
    for tok, agent in tokens.items():
        if hmac.compare_digest(tok, presented):
            hit = agent
    return hit


# ------------------------------------------------------------------ 索引缓存

class Ledger:
    """按 reader 缓存索引，账本 mtime 变了就重建。非线程安全的部分只碰字典，加锁。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.cf = _search.SearchConf(cfg)
        self.path = Path(cfg.path_of("claims"))
        self._lock = threading.Lock()
        self._state: dict[str, tuple[float, _search.Index]] = {}
        self._mtime = -1.0

    def _mtime_now(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return -1.0

    def index(self, reader: str) -> _search.Index:
        m = self._mtime_now()
        with self._lock:
            if m != self._mtime:
                self._state.clear()          # 账本换了，所有 reader 的索引一起作废
                self._mtime = m
            got = self._state.get(reader)
            if got is not None:
                return got[1]
        # 重建放在锁外：构建一次几十毫秒，不値得挡住别的读者
        docs = _search.load_all(self.cf)
        idx = _search.Index(docs, self.cf, reader)
        with self._lock:
            if self._mtime == m:
                self._state[reader] = (m, idx)
        return idx

    def claims_all(self) -> list[dict]:
        """管理员视角的全量（跳过内存过滤）。用于 `/stats`。"""
        return _search.load_all(self.cf)


# ------------------------------------------------------------------ 主体

def _json_bytes(obj) -> bytes:
    """序列化 + 脱敏闸门。

    账本在导入时已经脱敏过；这里再过一遍是**网络出口**的冗余检查——
    多花的是一次正则扫描，换的是"万一某个导入器漏了，也不会顺着 HTTP 出去"。
    """
    body = json.dumps(obj, ensure_ascii=False, sort_keys=False)
    return _redact(body).encode("utf-8")


def make_handler(ledger: Ledger, tokens: dict[str, str], tokens_path: str):
    class H(BaseHTTPRequestHandler):
        server_version = "memapi/1"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # ---------------- 基础设施 ----------------
        def log_message(self, fmt, *args):      # 绝不打 token
            sys.stderr.write("%s %s %s\n" % (
                time.strftime("%F %T"), self.address_string(), fmt % args))

        def _send(self, code: int, obj, extra: dict | None = None):
            body = _json_bytes(obj)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _err(self, code: int, msg: str, **kw):
            self._send(code, {"error": {"status": code, "message": msg, **kw}})

        def _text(self, code: int, text: str, ctype="text/markdown; charset=utf-8"):
            body = _redact(text).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _token(self) -> str:
            h = self.headers.get("Authorization") or ""
            if h.lower().startswith("bearer "):
                return h[7:].strip()
            return (self.headers.get("X-Mem-Token") or "").strip()

        # ---------------- 路由 ----------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            path = u.path.rstrip("/") or "/"
            q = parse_qs(u.query)

            # 未认证的健康检查：只说"活着"，不带任何账本数字
            if path == "/health" and not self._token():
                return self._send(200, {"ok": True, "service": "mem-api"})

            agent = whoami(tokens, self._token())
            if not agent:
                return self._err(401, "需要 token（Authorization: Bearer … 或 X-Mem-Token）")

            if path == "/health":
                return self._send(200, {"ok": True, "service": "mem-api", "reader": agent,
                                        # docs 而不是 claims：索引里还有指令层（rules）
                                        "docs": len(ledger.index(agent).docs)})
            if path == "/search":
                return self._search(q, agent)
            if path == "/rules":
                return self._rules(q, agent)
            if path.startswith("/claim/"):
                return self._claim(path[len("/claim/"):], agent)
            if path == "/stats":
                return self._stats(agent)
            if path == "/report/today":
                return self._report(agent)
            return self._err(404, f"没有这个路径：{path}",
                             hint="可用：/health /search /rules /claim/<id> /stats /report/today")

        def do_POST(self):
            self._readonly()

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST

        def _readonly(self):
            self._err(405, "本 API 只读：没有写端点。写入走 CLI"
                           "（propose/verify/supersede/reject/stale），那里要写清「凭什么」。",
                      allow="GET, HEAD")

        # ---------------- 端点 ----------------
        def _search(self, q, agent):
            words = (q.get("q") or [""])[0].strip()
            if not words:
                return self._err(400, "缺少 q")
            try:
                k = max(1, min(int((q.get("k") or ["8"])[0]), 50))
            except ValueError:
                return self._err(400, "k 要是整数")
            idx = ledger.index(agent)
            day = ""
            if q.get("as_of"):
                day = _norm_date(q["as_of"][0]) or ""
                if not day:
                    return self._err(400, "as_of 要 YYYY-MM-DD")
            docs = idx.docs
            asof = None
            if day:
                docs, st = _search.as_of_filter(docs, day)
                asof = {"day": day, **st}
                # 重建索引：as_of 换掉了文档集，tf 得跟着换。可见性已在上面过滤过，
                # 再过滤一次是空操作——比"记得它已经被过滤过"更不容易错。
                idx = _search.Index(docs, ledger.cf, agent)
            res = _search.search(idx, words, k)
            out = {
                "query": words, "reader": agent, "k": k,
                "docs": len(idx.docs), "dropped_scope": idx.dropped_scope,
                "results": [{"score": round(s, 4), "claim": c} for c, s in res],
            }
            if asof:
                out["as_of"] = asof
                if asof.get("日期不明"):
                    out["as_of"]["note"] = ("日期不明 = 没有 valid_from 也没状态变更时间，"
                                            "没法判断那天成不成立，按保留处理——别当成证据")
            return self._send(200, out)

        def _rules(self, q, agent):
            """列规矩（指令层）。2026-11-16 新增。

            ## 为什么要这个端点

            在此之前，规矩只能靠 `/search` 撞出来——问什么返回什么，
            没法回答「一共有哪些规矩」「writing 域有哪些」。agent 想遵守规矩，
            却只能先猜到关键词。规矩本来就是要所有 agent 都遵守的
            （`rule_as_doc()` 把 visibility 固定成 shared），那就该列得出来。

            ## heldout 默认不返回

            `rules.jsonl` 里有 14 条 `heldout: true`，是检索评测的留出目标。
            谁都能读走的话，任何一次「照着规矩优化检索」都会污染留出集。
            所以默认滤掉；只有记忆管理员（`me`）能显式 `include_heldout=1`。
            """
            idx = ledger.index(agent)
            want_holdout = (q.get("include_heldout") or ["0"])[0].strip() in {"1", "true", "yes"}
            if want_holdout and agent != _scope.MEMORY_ADMIN:
                return self._err(403, "include_heldout 只对记忆管理员开放",
                                 hint="留出集被读走就没法再当留出集用")
            domain = (q.get("domain") or [""])[0].strip()
            status = (q.get("status") or [""])[0].strip()
            words = (q.get("q") or [""])[0].strip().lower()
            try:
                k = max(1, min(int((q.get("k") or ["200"])[0]), 500))
            except ValueError:
                return self._err(400, "k 要是整数")

            rows = []
            for c in idx.docs:
                if c.get("layer") != "rule":
                    continue
                if not _scope.visible(c, agent):
                    continue            # 与 /search 同一份可见性规则，不另写一套
                if domain and not str(c.get("subject") or "").startswith(domain):
                    continue
                if status and c.get("status") != status:
                    continue
                if words and words not in str(c.get("value") or "").lower():
                    continue
                if c.get("heldout") and not want_holdout:
                    continue
                rows.append(c)
            rows.sort(key=lambda c: (str(c.get("subject") or ""), str(c.get("id"))))
            total = len(rows)
            rows = rows[:k]
            return self._send(200, {
                "reader": agent, "scope": "rules", "total": total, "returned": len(rows),
                "include_heldout": want_holdout,
                "filters": {"domain": domain, "status": status, "q": words},
                "rules": rows,
            })

        def _claim(self, cid, agent):
            cid = (cid or "").strip()
            if not cid:
                return self._err(400, "缺少 claim id")
            idx = ledger.index(agent)
            for c in idx.docs:
                if c.get("id") == cid:
                    return self._send(200, {"reader": agent, "claim": c})
            # 404 而不是 403：403 等于告诉对方"这条存在但你读不到"
            return self._err(404, f"没有这条：{cid}",
                             hint="不存在，或者它对你的可见性不可读（本 API 不区分这两种情况）")

        def _stats(self, agent):
            idx = ledger.index(agent)
            from collections import Counter
            st = Counter(c.get("status") for c in idx.docs)
            ev = Counter(c.get("evidence") for c in idx.docs)
            vv = Counter(c.get("visibility") or "未标" for c in idx.docs)
            return self._send(200, {
                "reader": agent, "docs": len(idx.docs),
                "dropped_scope": idx.dropped_scope,
                "status": dict(st.most_common()), "evidence": dict(ev.most_common()),
                "visibility": dict(vv.most_common()),
                "ledger_mtime": time.strftime(
                    "%F %T", time.localtime(max(0.0, ledger._mtime))),
            })

        def _report(self, agent):
            if agent != _scope.MEMORY_ADMIN:
                return self._err(403, "日报含全体条目，只对记忆管理员开放",
                                 hint="管理员 token 对应 agent_id = me；"
                                      "要读自己的可见范围请用 /search 与 /stats")
            rp = Path(ledger.cfg.path_of("reports")) / (
                "冲突报告-" + time.strftime("%Y-%m-%d") + ".md")
            if not rp.exists():
                return self._err(404, f"今天的日报还没生成：{rp.name}（04:20 重建时产出）")
            return self._text(200, rp.read_text(encoding="utf-8"))

    return H


def serve(cfg=None, host="127.0.0.1", port=18092, tokens_path=None, quiet=False) -> int:
    """起服务（阻塞）。返回进程退出码。"""
    if host not in LOOPBACK:
        print(f"拒绝监听 {host}：只允许回环。要对外，请在前面放带认证的反向代理，"
              f"别把账本直接暴露到网络上。", file=sys.stderr)
        return 2
    try:
        tokens = load_tokens(tokens_path)
    except (FileNotFoundError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 2
    cfg = cfg or _config.load()
    ledger = Ledger(cfg)
    h = make_handler(ledger, tokens, str(tokens_path or TOKENS_DEFAULT))
    srv = ThreadingHTTPServer((host, int(port)), h)
    srv.daemon_threads = True
    who = "、".join(sorted(set(tokens.values())))
    if not quiet:
        print(f"mem-api 监听 http://{host}:{port}  凭据：{len(tokens)} 个 token / agent：{who}")
        print(f"账本：{ledger.path}（mtime 变了自动重建索引，不用重启）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("收到中断，停止。", file=sys.stderr)
    finally:
        srv.server_close()
    return 0
