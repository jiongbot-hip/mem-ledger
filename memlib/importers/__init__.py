"""导入器注册表。

## 协议

每个导入器就是一个模块，暴露两样东西：

    NAME = "docs"                                   # 与 config.toml 的 [importers.<name>] 对应
    def run(cfg) -> (rows: list[dict], stat: dict)  # stat 至少含 total / imported

`run` 只读外部来源、**不写账本**——写入永远由 `store`/`build` 统一做。
这条是分层管理办法「写路径唯一提交者」的落地：导入器产候选，提交者做去重与落盘。

## 加一个新来源，两种方式

包内的：在本目录建模块，照上面的协议写 `run(cfg)`，**不需要改这个文件**——
`REGISTRY` 是扫出来的。

包外的：在 `config.toml` 写

    [importers]
    dir = "~/.config/mem-ledger/importers"

然后把 `.py` 文件丢进去。第三方导入器不用 fork 这个库，也不用装成包。
同名时包内优先，并在第二个位置才用外部的——这样升级库不会把用户的导入器覆盖掉。

## 判据来源要排除自己上一轮的产物

如果导入器靠"账本里提到过 X"来决定收什么，第一次导入后账本里就有了 X，
下一轮它会"证明"自己该被导入——清单只增不减。
`systemd` 导入器踩过这个坑，见它的 docstring。
"""
from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from pathlib import Path


def _builtin() -> dict[str, object]:
    """扫本包，凡是模块级有 `NAME` 和 `run` 的都算导入器。

    不再手写一张登记表：2026-11-17 之前加一个来源要改两处（建模块 + 登记），
    漏了第二处就在 `build` 时才报错，而错误信息指向的是 config 不是代码。
    """
    out: dict[str, object] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        name = getattr(mod, "NAME", None)
        if isinstance(name, str) and callable(getattr(mod, "run", None)):
            out[name] = mod
    return out


def _external(cfg) -> dict[str, object]:
    """`[importers].dir` 下的一批 `.py`，按文件路径加载。

    加载失败**直接抛**，不跳过：一个写着 `enabled = true` 却加载不起来的导入器，
    静默跳过会让账本少一整类来源，而 build 的输出仍然是"成功"。
    """
    d = (cfg.raw.get("importers") or {}).get("dir")
    if not d:
        return {}
    base = Path(str(d)).expanduser()
    if not base.is_dir():
        raise FileNotFoundError(
            f"config.toml 的 importers.dir 不是目录：{base}\n"
            f"要么建它，要么删掉这一行（删掉就等于不用外部导入器）。")
    out: dict[str, object] = {}
    for p in sorted(base.glob("*.py")):
        if p.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"memledger_ext_{p.stem}", p)
        if spec is None or spec.loader is None:
            raise ImportError(f"外部导入器加载不了：{p}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        name = getattr(mod, "NAME", None)
        if not isinstance(name, str) or not callable(getattr(mod, "run", None)):
            raise TypeError(
                f"外部导入器 {p} 缺 NAME 或 run(cfg)；协议见 memlib/importers/__init__.py")
        out[name] = mod
    return out


def registry(cfg=None) -> dict[str, object]:
    """包内 + 外部，外部同名不覆盖包内。"""
    reg = dict(_builtin())
    if cfg is not None:
        for name, mod in _external(cfg).items():
            reg.setdefault(name, mod)
    return reg


def enabled_modules(cfg) -> list[tuple[str, object]]:
    """按 config 里的书写顺序，返回启用中的 (名字, 模块)。

    config 写了一个没有实现的导入器名 → 直接报错，不静默跳过。
    """
    reg = registry(cfg)
    out = []
    for name in cfg.importers():
        mod = reg.get(name)
        if mod is None:
            raise KeyError(
                f"config.toml 启用了未实现的导入器 {name!r}；"
                f"已实现：{', '.join(sorted(reg))}。"
                f"外部导入器放 importers.dir 指向的目录。")
        out.append((name, mod))
    return out
