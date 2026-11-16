#!/usr/bin/env python3
"""写端凭据脱敏（纯标准库、无副作用）。

用途：把会话原文 / 模型抽出的自由文本写进 `facts.jsonl`、`会话索引.jsonl|md`、
灵枢 mdcg 节点之前，先抹掉凭据形态的子串，避免真实 PAT / API key 落盘。
同一份实现在两处使用：本目录脚本，以及 dsh-memory 插件的 `md_cg/_memredact.py`
（由 `mdcg-redact-patch.py` 复制，保持逐字一致）。

约束：
- 只做字符串替换，不改结构、不改函数签名；
- 没有 I/O、没有全局可变状态；
- 非 str 输入原样返回（None、数字、dict 都不会被碰到）。

顺序有讲究：`github_pat_` 必须排在 `ghp_` 前面，否则细粒度 token 可能被截成半条。
"""
from __future__ import annotations

import re

_PATTERNS = (
    # 细粒度 PAT：github_pat_<20+ 字符>，必须先于 ghp_
    # 2026-11-14：阈值从 30 降到 20。完整的约 93 位，但会话里常见截断成
    # `github_pat_11C…` 这类 34 位的展示形式——那也是有信息量的片段，一并抹掉。
    # 锚点够硬（`github_pat_` 本身不会出现在正常文本里），降阈值不会误伤。
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    # 经典 PAT：ghp_<36 位字母数字>
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    # OpenAI / OpenRouter 等 sk- key；前置断言挡住 sk-learn、disk-usage、task-xxx 这类词内片段
    re.compile(r"(?<![A-Za-z])sk-[A-Za-z0-9_\-]{20,}"),
    # AWS access key id
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # Slack token
    re.compile(r"xox[bp]-[A-Za-z0-9-]{10,}"),
    # 讯飞 MaaS：ak-<32 位十六进制>
    re.compile(r"(?<![A-Za-z0-9])ak-[0-9a-f]{32}(?![0-9a-f])"),
    # 微信 bot token：<hex>@im.bot:<hex>
    re.compile(r"(?<![A-Za-z0-9])[0-9a-f]{8,}@im\.bot:[A-Za-z0-9]+"),
    # 2026-11-14 补：字段名带上下文的凭据。
    # 起因是 `~/.dsh/.credentials.yaml` 里一堆自定义格式（飞书 app secret、讯飞 APISecret、
    # DeepSeek 平台 token），形态就是普通 32 位串，纯靠值认不出来，一打印就漏。
    # 靠「字段名 + 分隔符」定位，不误伤正文里的裸串。
    re.compile(r"(?i)\b((?:SPARK|FEISHU|DSH|DEEPSEEK|OPENROUTER|SILICONFLOW|MOONSHOT|"
               r"ZHIPU|DASHSCOPE|ARK|VOLC)[A-Z0-9_]*?(?:APIKEY|API_KEY|APISECRET|API_SECRET|"
               r"APP_SECRET|TOKEN|SECRET|APPID)[A-Z0-9_]*)\b(\s*[:=]\s*)([\"']?)"
               r"([A-Za-z0-9_\-+/=.]{6,})\3"),
    # 裸字段名（yaml/ini 里常见的 `token:`、`secret:`、`password:`）。
    # 只在「整行以缩进开头」且「值像随机串」时才抹，避免把正文里的普通词干掉。
    #
    # 2026-11-14 修：值的字符类**必须用白名单**，不能用 `[^\s"',}]` 这种黑名单。
    # 实测拿会话存档跑时，黑名单把 JSON 转义里的反斜杠也吃了——`...TOKEN: xxx\n` 里的
    # `\n` 被当成值的一部分替换掉，JSON 当场缺个转义符，整份存档解析失败。
    # 白名单同时保证了「已经是 [已脱敏] 的值不会被二次匹配」。
    re.compile(r"(?m)^([ \t]+)(\w*(?:token|secret|password|passwd|apikey|api_key)\w*)"
               r"(\s*:\s*)([\"']?)(?=[A-Za-z0-9_\-+/=.]*[0-9])(?=[A-Za-z0-9_\-+/=.]*[A-Za-z])"
               r"([A-Za-z0-9_\-+/=.]{16,})\4"),
    # PEM 私钥块。
    # 2026-11-14 加长度上限：原先的 `[\s\S]*?` 无界，实测在会话存档里把
    # 一处 `BEGIN`（第 4402 行）和 28 行之后的另一处 `END`（第 4430 行）配成一对，
    # 中间 49KB、28 条完整记录被当成一个"私钥"吞掉，JSONL 结构当场崩掉。
    # 真实私钥不会有几十 KB；超过这个量级一定是误配。
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,20000}?-----END [A-Z ]*PRIVATE KEY-----"),
    # 2026-11-17 补：命令行形态的凭据。原先只认「值本身像 key」（sk-/ghp_/AKIA）和
    # 「yaml 字段名 + 值」，`curl "…?token=xxx"`、`-H "Authorization: Bearer xxx"`、
    # `GH_TOKEN=xxx gh …` 三类全放过——实测会话卡里一条 43 位 DSH 连接 token 原样落了盘。
    # 卡片工具 `claude-session-card` 里先补的这几条，这里收成唯一一份。
    # 值长度门槛 12 是为了不误伤 `max_tokens=4096` 这类配置文本（且 token 后面不能跟 s）。
    re.compile(r"(?i)([?&](?:token|access_token|refresh_token|id_token|api_key|apikey|"
               r"key|secret|password|passwd|sig|signature|auth)=)[^&\s\"'`\\]{4,}"),
    re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)(\bAuthorization\s*:\s*Basic\s+)[A-Za-z0-9+/=]{8,}"),
    re.compile(r"(?i)((?<![A-Za-z0-9])(?:token|secret|password|passwd|apikey|api_key|api-key|"
               r"access_key|secret_key|private_token)[\"']?\s*[:=]\s*)"
               r"([\"']?)([A-Za-z0-9._\-+/=]{12,})"),
    re.compile(r"(?i)((?<![A-Za-z0-9])--?(?:token|secret|password|passwd|api-key|apikey)\s+)"
               r"([\"']?)([A-Za-z0-9._\-+/=]{12,})"),
    re.compile(r"(://[^/\s:@]{1,64}:)[^@/\s]{1,128}(@)"),
    re.compile(r"(?i)(\b(?:X-Auth-Token|Private-Token|X-Api-Key)\s*:\s*)\S{8,}"),
)

_REPLACEMENTS = (
    "[已脱敏:github-pat]",
    "[已脱敏:github-pat]",
    "[已脱敏:sk-key]",
    "[已脱敏:aws-akid]",
    "[已脱敏:slack-token]",
    "[已脱敏:iflytek-maas]",
    "[已脱敏:wechat-bot]",
    None,   # 前缀字段名规则：保留字段名，见 redact() 里的分组替换
    None,   # 裸字段名规则：同上
    "[已脱敏:private-key]",
    r"\1[已脱敏:token-param]",     # URL 查询串：保留形参名
    r"\1[已脱敏:bearer]",
    r"\1[已脱敏:basic]",
    None,                         # token=/TOKEN=x 赋值：(名+分隔, 引号, 值)
    None,                         # --token x：(旗标+空格, 引号, 值)
    r"\1[已脱敏]\2",              # URL 里的 user:pass@
    r"\1[已脱敏]",                # 请求头
)


def redact(text: str) -> str:
    """返回脱敏后的文本；非 str 输入原样返回。"""
    if not isinstance(text, str):
        return text
    for pattern, mark in zip(_PATTERNS, _REPLACEMENTS):
        if mark is None:
            # 只抹最后一个分组（值），保留字段名、分隔符、缩进、引号，
            # 方便人看出这里原本是什么字段。分组个数随规则不同，统一取「除末位外全部」。
            def _keep(m, _mark="[已脱敏]"):
                g = m.groups()
                return "".join(g[:-1]) + _mark + (g[-2] or "")
            text = pattern.sub(_keep, text)
        else:
            text = pattern.sub(mark, text)
    return text


def redact_deep(obj):
    """递归脱敏 dict/list/tuple 里的字符串叶子，返回新对象。

    供 mdcg 插件在写盘前处理 frontmatter 用；键名不脱敏（键是 schema 名，
    不是内容），非容器非 str 原样返回，不修改调用方传入的对象。
    """
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_deep(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_deep(v) for v in obj)
    return obj
