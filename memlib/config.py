"""读 `系统/config.toml`，把路径与参数暴露成代码可用的对象。

## 为什么要有这一层

2026-11-15 之前，22 处路径硬编码散在 `mem-ledger.py` 里、参数散在模块常量里
（`EVIDENCE_W`、BM25 的 `b`、重排权重、`DOC_DIRS`…）。换台机器要改代码，
而改代码就会顺手动到已验证的行为。

现在代码只认 `cfg`，**全部机器相关的东西都在 config.toml 里**。

## fail closed

配置文件不存在、或必填项缺失，**直接抛错，不退回硬编码默认值**。
理由和脱敏闸门一样：默认值漂了没人会发现，而它影响的是每一道检索。
宁可起不来，也不要悄悄跑在另一套参数上。
"""
from __future__ import annotations

import tomllib
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"
EXAMPLE = CONFIG_PATH.with_name("config.example.toml")

HINT = (f"从 {EXAMPLE.name} 复制一份再改：\n"
        f"    cp {EXAMPLE} {CONFIG_PATH.name}\n"
        f"里面的注释写了每个键是什么意思。这份配置不是可选的——"
        f"所有路径与检索参数都在里面，缺一项就起不来是刻意的（见本文件开头的 fail closed）。")


class ConfigError(KeyError):
    """配置缺项。继承 KeyError：老调用方 catch KeyError 的地方不用改。"""

    def __init__(self, msg: str):
        super().__init__(msg)
        self.msg = msg

    def __str__(self) -> str:
        return self.msg


class Config:
    """config.toml 的只读视图。字段缺失直接报错，不静默兜底。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else CONFIG_PATH
        if not self.path.exists():
            raise FileNotFoundError(f"记忆系统配置不存在：{self.path}\n{HINT}")
        with self.path.open("rb") as f:
            self.raw = tomllib.load(f)

    # ---- 取值 ----

    def _req(self, table: str, key: str):
        node = self.raw
        for part in table.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ConfigError(f"config.toml 缺少 [{table}] 段\n{HINT}")
            node = node[part]
        if not isinstance(node, dict) or key not in node:
            raise ConfigError(f"config.toml 缺少 {table}.{key}\n{HINT}")
        return node[key]

    def path_of(self, key: str) -> Path:
        """`paths` 段里的路径，展开 `~`。"""
        return Path(str(self._req("paths", key))).expanduser()

    def paths(self) -> dict[str, Path]:
        return {k: Path(str(v)).expanduser()
                for k, v in self.raw.get("paths", {}).items()}

    def importer(self, name: str) -> dict:
        """某个导入器的配置段。没配就当作关闭。"""
        return self.raw.get("importers", {}).get(name, {"enabled": False}) or {}

    def importer_enabled(self, name: str) -> bool:
        return bool(self.importer(name).get("enabled"))

    def importers(self) -> list[str]:
        """按 config 里的书写顺序返回启用中的导入器名。"""
        return [n for n, d in self.raw.get("importers", {}).items()
                if isinstance(d, dict) and d.get("enabled")]

    def search(self, key: str):
        return self._req("search", key)

    def evidence_weight(self) -> dict[str, float]:
        return dict(self._req("search", "evidence_weight"))

    def rerank_weights(self) -> tuple[float, float, float]:
        w = self._req("search", "rerank")
        if len(w) != 3:
            raise ValueError(f"search.rerank 必须恰好 3 个数（相关度/证据分/新鲜度），实际 {w}")
        return float(w[0]), float(w[1]), float(w[2])

    def rule_boost(self) -> float:
        """规矩层加分。没配就是 0，老配置不需要改文件。"""
        return float(self.raw.get("search", {}).get("rule_boost", 0.0))

    def synonyms(self) -> dict[str, tuple[str, ...]]:
        return {c: tuple(forms) for c, forms in self.raw.get("synonyms", {}).items()}

    def identity(self, key: str):
        return self._req("identity", key)

    def ttl(self) -> dict[str, int]:
        """谓词 → 复核周期（天）。`[ttl.meta]` 不是谓词，排除掉。

        没列的谓词不在返回里 = 不复核。默认不打扰：只有会随时间自然失效的
        谓词才该进这张表，否则每条散文都会被要求"复核"，等于没有复核。
        """
        raw = self.raw.get("ttl", {}) or {}
        return {k: int(v) for k, v in raw.items()
                if k != "meta" and isinstance(v, (int, float))}

    def ttl_auto_mark(self) -> bool:
        meta = (self.raw.get("ttl", {}) or {}).get("meta", {}) or {}
        return bool(meta.get("auto_mark", False))

    def ttl_derived_origins(self) -> tuple[str, ...]:
        """每次构建重读的源（origin 前缀）。这些条目不参与复核周期。"""
        meta = (self.raw.get("ttl", {}) or {}).get("meta", {}) or {}
        return tuple(meta.get("derived_origins", ()) or ())

    # ---- 自检 ----

    def check(self) -> list[str]:
        """列出配置本身的问题。空列表 = 没问题。

        只查**结构性**问题（缺段、路径不存在、参数越界），不查参数好不好——
        后者得靠评测闸门，不是配置检查能判的。
        """
        bad: list[str] = []
        # 这些是本工具**自己造出来的**，第一次跑之前本来就不该存在：
        # 输出目录、账本、规矩库、裁决日志。只查它们的上级目录在不在。
        made = {"reports", "evaluation", "ledger", "proposals",
                "claims", "rules", "overrides"}
        for k, p in self.paths().items():
            if k in made:
                if not p.parent.exists():
                    bad.append(f"paths.{k} 的上级目录不存在：{p.parent}")
                continue
            # 其余是**输入**：不存在就是配置写错了或盘没挂上，必须报。
            if not p.exists():
                bad.append(f"paths.{k} 不存在：{p}")
        try:
            b = float(self.search("b"))
            if not 0.0 <= b <= 1.0:
                bad.append(f"search.b 应在 0..1，实际 {b}")
        except KeyError as e:
            bad.append(str(e))
        try:
            w = self.rerank_weights()
            if abs(sum(w) - 1.0) > 1e-6:
                bad.append(f"search.rerank 三项之和应为 1.0，实际 {sum(w):.3f}")
        except (KeyError, ValueError) as e:
            bad.append(str(e))
        if not self.importers():
            bad.append("没有任何启用中的导入器")
        for pred, days in self.ttl().items():
            if days <= 0:
                bad.append(f"ttl.{pred} 必须是正的天数，实际 {days}")
        return bad


def load(path: Path | str | None = None) -> Config:
    return Config(path)
