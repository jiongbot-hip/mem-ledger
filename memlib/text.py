"""中文句子 → 结构化事实：主语清洗、实体别名归一、四类值抽取、值归一。

## 这一层是整个账本最"不能手滑"的地方

`derive()` 决定一段文字里**哪些东西会被当成事实收进账本**。改一个边界条件，
收进来的事实就变，claim id 跟着变，overrides 对不上，评测分数漂移。
所以本文件是从 `mem-ledger.py` **逐字搬过来的**，没有"顺手优化"。

搬家时的验收：`build` 出的 2117 条 claim，`(id, subject, predicate, value, status, origin)`
必须与搬家前逐条一致。跑不过就是搬错了，不许调。

## 只抽可机械比对的四类

端口、路径、版本、上下文长度。**宁可漏，不要凭句式猜**——
猜错的事实比没有事实更糟，因为它会以"已验证"的身份参与检索。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    from .redact import redact
    from .schema import norm as _norm
except ImportError:                                    # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from redact import redact                          # type: ignore
    from schema import norm as _norm                   # type: ignore

# 不能当主语的代词/状态词
STOP_SUBJ = {"它", "他", "她", "这个", "那个", "这里", "那里", "现在", "目前", "然后",
             "所以", "我们", "你们", "他们", "本机", "系统", "全部", "另外", "因此",
             "其中", "同时", "最后", "首先", "刚才", "上面", "下面", "这次",
             "位于", "路径", "目录", "版本", "端口", "位置", "本体", "服务",
             # 协议名 / 状态词，不是被指代的实体
             "https", "http", "socks5", "socks", "estab", "upstream", "downstream",
             "webserver.", "proxy", "manual", "auto", "none"}

# 只抽可机械比对的四类；宁可漏，不要凭句式猜。匹配前先去掉空白（中文夹 ASCII 很常见）。
RULES = [
    # 「X 端口为 8085」「X 的端口 8085」
    ("port", re.compile(
        r"(?P<s>[\w一-龥./+-]{2,30}?)(?:的)?(?:监听)?(?:端口|port)(?:号)?"
        r"(?:是|为|改成|改为|在|:|：|=|＝)?(?P<v>\d{2,5})(?![\d.])")),
    # 「teamd.service）监听 0.0.0.0:18090」「桥 ... 监听 127.0.0.1:18091」
    ("port", re.compile(
        r"(?P<s>[\w一-龥./+-]{2,30}?)[`'\"）)\s，,、]{0,4}"
        r"(?:（[^）]{0,40}）|\([^)]{0,40}\))?[`'\"\s，,、]{0,4}"
        r"(?:监听|指向|跑在|服务在|位于)[`'\"\s]{0,4}"
        r"(?:(?:\d{1,3}\.){3}\d{1,3}|localhost|0\.0\.0\.0|\[::\])"
        r":(?P<v>\d{2,5})(?!\d)")),
    # 「（qwen30/:8085、qwen35-4b-q4/:8110）」这种紧凑写法
    ("port", re.compile(
        r"(?P<s>[\w一-龥./+-]{2,30})/:(?P<v>\d{2,5})(?![.\d])")),
    # 「X 位于 /path」「X 的路径为 ~/path」
    ("path", re.compile(
        r"(?P<s>[\w一-龥./+-]{2,30}?)(?:的)?(?:路径|目录|源码位置|位置|本体|位于)"
        r"(?:是|为|在|:|：)?(?P<v>[~/](?:[^\s，。；、）)\]\"'`]+))")),
    # 「X 版本为 12.1.4」
    ("version", re.compile(
        r"(?P<s>[\w一-龥./+-]{2,30}?)(?:的)?(?:版本|version)(?:号)?"
        r"(?:是|为|就|:|：)?(?P<v>v?\d+(?:\.\d+){1,3})")),
    # 「X 的上下文为 8K」「q30 262144→实际 32768」取箭头后的实际值
    ("ctx", re.compile(
        r"(?P<s>[\w一-龥./+-]{2,30}?)(?:的)?(?:上下文|ctx|context)(?:长度|窗口)?"
        r"(?:是|为|扩到|提到|:|：|=)?(?P<v>\d+(?:\.\d+)?[KkMm]?)")),
    # 「q30 的 ctx 262144→实际 32768」；ctx 字样允许落在主语前同一句内
    ("ctx", re.compile(
        r"(?P<s>[\w一-龥./+-]{2,30}?)[\s`'\"的]{0,4}\d{2,7}(?:→|->)[\s`'\"]{0,4}"
        r"(?:实际|实测)?[\s`'\"]{0,4}(?P<v>\d{2,7})")),
]

NOUN_SUFFIX = ("程序", "服务", "文件", "目录", "脚本", "容器", "驱动", "模型",
               "系统", "工具", "插件", "进程", "应用", "网站", "仓库", "镜像",
               "接口", "网关", "桥", "面板", "数据库", "实例", "节点")

# 同一组件的不同叫法（q30 / qwen30 / qwen3-30b-a3b / qwen30.service）必须归一，
# 否则「memt 说 q30 在 8085」和「registry 说 q30 在 8085」永远互证不上。
ALIASES: dict[str, str] = {}


def clean_subj(s: str) -> str:
    s = s.strip(" 　，,。:：的、`*_「」【】[]()（）\"'")
    s = re.sub(r"^(?:而|并且|同时|目前|现在|已经|还|也|都)", "", s)
    s = re.sub(r"^(?:本地|本机|当前|这台的?)", "", s)
    if "/" in s:                       # 路径主体只留文件名
        s = s.rstrip("/").rsplit("/", 1)[-1]
    s = re.sub(r"^(?:位于|路径|目录|端口)", "", s)
    s = re.sub(r"^\d{2,5}端口的?", "", s)
    if len(s) < 2 or s.lower() in STOP_SUBJ or s.endswith(("端口",)):
        return ""
    return s[:40]


def alias_key(s: str) -> str:
    k = _norm(s)
    k = re.sub(r"\.service$", "", k)
    k = re.sub(r"\.(gguf|safetensors)$", "", k)
    return k


def load_aliases(registry: Path | str | None = None) -> dict[str, str]:
    """从 models.yaml 抽 id/logical/service/modelctl/display，合成别名 → 规范 id。

    `registry` 由调用方从 config 传入；不传则不做任何归一（ALIASES 为空）。
    这一点很重要：**别名表来自配置，不是硬编码**，所以换台机器不会用错别名。

    必须 `expanduser()`：config 里写的是 `~/…`，`Path("~/x")` 是字面量不是家目录，
    少了这一步就静默读到空表——2026-11-15 差分测试当场抓到（别名 153 → 0）。
    """
    global ALIASES
    ALIASES = {}
    if registry is None:
        return ALIASES
    registry = Path(str(registry)).expanduser()
    if not registry.exists():
        print(f"警告：别名表来源不存在，主语将不做归一：{registry}", file=sys.stderr)
        return ALIASES
    cur: dict = {}

    def flush():
        if not cur.get("id"):
            return
        canon = cur["id"]
        names = [cur.get("id"), cur.get("logical"), cur.get("service"),
                 cur.get("modelctl")]
        disp = cur.get("display", "")
        if disp:
            # 展示名开头的英文/数字 token 往往是另一个叫法
            names.append(re.split(r"[\s（(]", disp)[0])
        for n in names:
            if not n:
                continue
            k = alias_key(n)
            if len(k) < 2:
                continue
            ALIASES.setdefault(k, canon)

    for ln in Path(registry).read_text(encoding="utf-8", errors="ignore").splitlines():
        if re.match(r"^\s*-\s*id:", ln):
            flush()
            cur = {"id": ln.split(":", 1)[1].strip().strip("\"'")}
            continue
        m = re.match(r"^\s*(logical|service|modelctl|display):\s*(.+?)\s*$", ln)
        if m and cur.get("id"):
            cur[m.group(1)] = m.group(2).strip().strip("\"'")
    flush()
    return ALIASES


def canon_subj(s: str) -> str:
    """把主语归一到规范名；认不出就原样返回。"""
    if not s:
        return s
    k = alias_key(s)
    if k in ALIASES:
        return ALIASES[k]
    # 「qwen30」 vs 「q30」这类缩写差异：去掉数字前的字母串再试
    for alias, canon in ALIASES.items():
        if len(alias) >= 3 and len(k) >= 3 and (k in alias or alias in k):
            if abs(len(k) - len(alias)) <= 4:
                return canon
    return s


def plausible_subj(s: str) -> bool:
    """主语是否像实体。规则抽取只收像实体的，宁可漏。"""
    if not s:
        return False
    # 命令行片段、权重文件名不是实体
    if "--" in s or s.startswith("/") or s.endswith("/"):
        return False
    if re.search(r"\.(gguf|safetensors)$", s, re.I):
        return False
    if alias_key(s) in ALIASES:
        return True
    if re.search(r"[A-Za-z]{2}", s):          # fastboot / ghidra / comfyui-strix-halo
        return True
    if s.endswith(NOUN_SUFFIX):               # 无限画布主程序 / 日报目录
        return True
    return False


def norm_value(pred: str, value: str) -> str:
    """只用于判同：端口去前导零、ctx 统一成字节数、版本去 v、路径去尾斜杠。"""
    v = (value or "").strip()
    if pred == "port":
        m = re.search(r"\d+", v)
        return str(int(m.group())) if m else _norm(v)
    if pred == "ctx":
        m = re.match(r"^(\d+(?:\.\d+)?)\s*([KkMm]?)$", v.replace(" ", ""))
        if m:
            n = float(m.group(1))
            unit = m.group(2).lower()
            if unit == "k":
                n *= 1024
            elif unit == "m":
                n *= 1024 * 1024
            return str(int(n))
        return _norm(v)
    if pred == "version":
        return _norm(re.sub(r"^v", "", v))
    if pred == "path":
        return _norm(v.rstrip("/"))
    return _norm(v)


def derive(text: str) -> list[tuple[str, str, str]]:
    """从一句话里抽 (subject, predicate, value)。规则保守，只收四类。

    同时跑「去空白」和「保留空白」两遍：前者治中文夹 ASCII，后者保住词边界，
    否则 `q30 262144→实际 32768` 会被去空白粘成 `q30262144` 而漏抽。
    """
    raw = redact(text or "")
    out, seen = [], set()
    for t in (re.sub(r"\s+", "", raw), raw):
        _derive_in(t, out, seen)
    return out


def _derive_in(t: str, out: list, seen: set) -> None:
    for pred, rx in RULES:
        for m in rx.finditer(t):
            subj = canon_subj(clean_subj(m.group("s")))
            val = m.group("v").rstrip("，。；、）)]\"'`")
            if not plausible_subj(subj):
                continue
            if pred == "port":
                start, end = m.start("v"), m.end("v")
                # 排除 IP 的末段：127.0.0.1:1080 里的 127、1080 都不是端口主张
                if t[start - 1:start] == "." or t[end:end + 1] == ".":
                    continue
                if not (1024 <= int(val) <= 65535):
                    continue
            elif pred == "path":
                if not re.search(r"([A-Za-z0-9._/@+%~-]|/)$", val):
                    val = re.sub(r"[一-鿿]+$", "", val)
                if not re.search(r"([A-Za-z0-9._/@+%~-]|/)$", val) or len(val) < 5:
                    continue
                val = val.rstrip("/")
            elif pred == "ctx":
                val = re.sub(r"\s+", "", val)
                # 上下文窗口至少 512；小的多半是把端口/条数误认成 ctx
                n = norm_value("ctx", val)
                if not n.isdigit() or int(n) < 512:
                    continue
                # 箭头写法噪声大：要求同一句里确实在谈 ctx/-c/上下文
                lo = max(t.rfind("。", 0, m.start()), t.rfind("\n", 0, m.start()),
                         t.rfind("；", 0, m.start()))
                win = t[lo + 1 if lo >= 0 else max(0, m.start() - 80):m.start()]
                if ("→" in t[m.start():m.end()] or "->" in t[m.start():m.end()]) and \
                        not re.search(r"ctx|上下文|\-c(?![a-z])|context", win):
                    continue
            key = (_norm(subj), pred)
            if key in seen:
                continue
            seen.add(key)
            out.append((subj, pred, val))
    return out
