"""端到端冒烟：从一份空配置建成能查的账本。

只用标准库（`python3 -m unittest discover -s tests`），不引第三方测试框架——
这个项目零依赖，测试也应该是零依赖，否则 CI 上要先装东西才能验证"零依赖"。

## 每一步都是子进程

不 import `memlib` 直接调函数：那样测的是"我在同一个进程里调用它"，
而使用者是通过 `mem` 命令行用的。子进程还能顺带验证入口脚本、
`--config` 参数、退出码这些真会挡住人的东西。
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "cli.py"


def write_config(base: Path, *, with_synonyms: bool = False) -> Path:
    syn = '[synonyms]\nport = ["端口", "port"]\n' if with_synonyms else ""
    cfg = base / "config.toml"
    cfg.write_text(f"""
[paths]
memory_root = "{base}"
ledger      = "{base}/ledger"
claims      = "{base}/ledger/claims.jsonl"
rules       = "{base}/ledger/rules.jsonl"
overrides   = "{base}/ledger/overrides.jsonl"
proposals   = "{base}/ledger/proposals.jsonl"
reports     = "{base}/ledger/reports"
evaluation  = "{base}/eval"
workspace   = "{base}"

[importers.docs]
enabled = true
dirs    = ["{base}/docs"]
root    = "{base}/docs"
deny    = []
log_pat = []
chunk   = 300

[search]
k1 = 1.2
b = 0.4
min_text = 6
rerank = [0.65, 0.25, 0.10]
freshness_days = 180
freshness_floor = 0.5

[search.evidence_weight]
user = 1.0
measured = 0.9
registry = 0.7
extracted = 0.5
inferred = 0.3

[identity]
default_visibility = "shared"
new_write_visibility = "private"
manual_agent = "me"

{syn}
""", encoding="utf-8")
    return cfg


def mem(cfg: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run([sys.executable, "-I", str(CLI), "--config", str(cfg), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(
            f"mem {' '.join(args)} 返回 {r.returncode}\n"
            f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")
    return r


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memledger-"))
        (self.tmp / "docs").mkdir()
        (self.tmp / "docs" / "一.md").write_text(
            "# 端口\n\n网关监听 127.0.0.1:18091，模型服务在 8085。\n", encoding="utf-8")
        self.cfg = write_config(self.tmp, with_synonyms=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestPipeline(Base):
    def test_build_then_search(self):
        self.assertFalse((self.tmp / "ledger" / "claims.jsonl").exists(),
                         "构建之前账本不该存在")
        mem(self.cfg, "build")
        rows = [json.loads(x) for x in
                (self.tmp / "ledger" / "claims.jsonl").read_text().splitlines() if x.strip()]
        self.assertGreaterEqual(len(rows), 1)
        # 每条都要契约完整：少字段的条目进了账本，后面每一层都得防它
        for r in rows:
            for k in ("id", "status", "evidence", "source_refs", "visibility"):
                self.assertIn(k, r)

        out = mem(self.cfg, "search", "网关 port").stdout
        self.assertIn("18091", out, f"检索没命中正确答案：\n{out}")

    def test_build_is_idempotent(self):
        mem(self.cfg, "build")
        a = (self.tmp / "ledger" / "claims.jsonl").read_text()
        mem(self.cfg, "build")
        b = (self.tmp / "ledger" / "claims.jsonl").read_text()
        self.assertEqual(a, b, "同样的输入重跑两次，账本变了")

    def test_process_record_not_imported(self):
        p = self.tmp / "docs" / "过程.md"
        p.write_text("> 过程记录\n\n# 这一轮做了什么\n\n端口改成了 9999。\n", encoding="utf-8")
        mem(self.cfg, "build")
        rows = [json.loads(x) for x in
                (self.tmp / "ledger" / "claims.jsonl").read_text().splitlines() if x.strip()]
        blob = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("9999", blob, "自称过程记录的文档不该进账本")

    def test_missing_config_section_says_what_to_do(self):
        bad = self.tmp / "bad.toml"
        bad.write_text("[paths]\nclaims = \"x\"\n", encoding="utf-8")
        r = mem(bad, "build", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("config.example.toml", r.stderr + r.stdout,
                      "缺段的报错要告诉人从哪里抄一份")

    def test_unknown_importer_fails_loudly(self):
        text = self.cfg.read_text().replace(
            "[importers.docs]", "[importers.nope]\nenabled = true\n\n[importers.docs]")
        self.cfg.write_text(text, encoding="utf-8")
        r = mem(self.cfg, "build", check=False)
        self.assertNotEqual(r.returncode, 0, "启用了不存在的导入器必须报错，不能静默跳过")
        self.assertIn("nope", r.stderr + r.stdout)


class TestSelfTest(Base):
    def test_selftest_green_on_bare_config(self):
        """空配置上的自检必须全绿。

        这条是 2026-11-17 加的：在那之前自检硬依赖本机的 systemd 与 `~/记忆`
        目录布局，换台机器必红——红的还不是机制坏了。
        """
        mem(self.cfg, "build")
        r = mem(self.cfg, "self-test", check=False)
        self.assertEqual(r.returncode, 0, f"自检没过：\n{r.stdout}\n{r.stderr}")
        self.assertNotIn("FAIL", r.stdout)


def _race_write(args) -> None:
    """写 `n` 行到 `tgt`。模块级函数：Pool 要 pickle 它。"""
    tgt, n = args
    sys.path.insert(0, str(ROOT))
    from memlib.atomic import write_jsonl
    write_jsonl(tgt, [{"id": f"{n}-{i}", "pad": "x" * 200} for i in range(n)])


class TestAtomic(unittest.TestCase):
    def test_concurrent_writes_never_mix(self):
        """四个进程同时写同一份账本，结果必须是其中一份**完整**的内容。

        2026-11-17 之前用固定的 `claims.jsonl.tmp`：两个 build 并发会互相截断，
        最后 replace 出来的是混合账本——不是"其中一个赢了"，是两份都不是。
        """
        tmp = Path(tempfile.mkdtemp(prefix="memledger-race-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        tgt = str(tmp / "race.jsonl")
        sizes = [200, 2000, 777, 5000]

        ctx = mp.get_context("fork")
        for _ in range(10):
            with ctx.Pool(4) as pool:
                pool.map(_race_write, [(tgt, n) for n in sizes])
            n = sum(1 for ln in Path(tgt).read_text().splitlines() if ln.strip())
            self.assertIn(n, sizes, f"得到 {n} 行，不是任何一份完整内容")


if __name__ == "__main__":
    unittest.main()
