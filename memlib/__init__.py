"""记忆系统核心库（2026-11-15 起）。

分层：

    config      读 config.toml —— 所有路径与参数都在那里，代码里不写死
    schema      claim 数据契约：字段、id 派生、合法性
    redact      脱敏闸门 —— 只负责找到唯一那份规则，不实现规则
    text        中文句子 → 结构化事实（别名归一、derive、值归一）
    store       账本读写、裁决（overrides）、身份标注、缺口与冲突
    report      端口索引、路径体检、冲突报告
    search      检索：门控 → BM25 → 重排 → 注入
    importers   导入器，每个一种来源。**自动发现**：包内模块有 NAME 和 run 就算，
                外加 [importers] dir 指向的外部目录。没有手写注册表，数量随配置与安装变。

设计原则（与 `00-总则/会话与记忆分层管理办法.md` 一致）：

- **事实与指令分开**：claims（现在是什么）与 rules（以后要怎么做）分开存，
  检索时合并但保留 `layer` 标记。
- **只增不删**：矛盾用 supersede / reject / stale 表达，`overrides.jsonl` 只追加。
- **过程记录不是事实**：文档讲"这一轮做了什么"就不进账本。
- **改动要过闸门**：动检索参数或行为后跑 `系统/评测/bin/gate.py check`。
"""

__all__ = ["config", "schema", "redact", "text", "store", "report", "search", "importers"]
