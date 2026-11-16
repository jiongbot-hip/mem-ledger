# mem-ledger

一份只增不删的主张账本。把散在文档、笔记、各个 AI 框架里的记忆，归一成可检索、
可追溯、可裁决的 JSONL。

零第三方依赖，Python 3.11+，标准库就够。

```
$ mem search "网关在哪个端口"
0.875  [cl_7a4d163e968d | fact | verified | measured | 2026-11-14 | src=... | agent=me | vis=shared]
       网关 port：18091
```

## 为什么要有这个东西

大多数 AI 记忆方案最后都变成一句话的堆。用的时候才发现三个问题一起出现：

1. 不知道这条是什么时候的。三年前的端口和昨天的端口长得一样。
2. 不知道可不可信。模型自己编的一句，和运维实测的一句，排在一起。
3. 不知道它是从哪来的。答错了没法回查，只能整体重来。

这套做法只干一件事：每条记忆都必须带出处、日期和证据等级，
矛盾不许覆盖，只许显式地判某条失效。

## 设计：一账四层

```
L0  证据      会话原文、原始文件。只读，永不修改。
      ↓ 导入器（各写各的，只读来源，不写账本）
L1  主张账本  claims.jsonl / rules.jsonl。唯一可写的真源。
      ↓ 派生
投影          检索索引、冲突报告、缺口清单、HTTP API、MCP。
      ↓ 消费
L2  工作记忆  带 TTL 的短期状态，过期重算。
```

判断一样东西该放哪一层，只有一个问题：删掉它能不能重建。

- 账本删了，可以从 L0 重新 build 出来，所以账本是投影的源头，但不是"所有记忆的真源"。
- `overrides.jsonl`（人的裁决）删了重建不出来，所以它必须单独备份。

## 三条硬规矩

一、事实与指令分开。
`claims.jsonl` 是"现在是什么"，`rules.jsonl` 是"以后要怎么做"。混在一个库里，
冲突裁决会把"日报必须用本地模型"和"日报当前用云端"当成一对矛盾事实来判。

二、只增不删。
矛盾用 `supersede` / `reject` / `stale` 表达，都带理由，都留历史。
人的裁决写在 `overrides.jsonl`，只追加；要撤回就再追加一条反向裁决，不删行。

三、过程记录不是事实。
讲"这一轮做了什么"的文档不进账本。它会被当成当前状态检索出来，
而读者无从分辨那是一次失败尝试还是最终结论。用文件头自标记排除，不靠改文件名。

## 快速开始

```bash
git clone https://github.com/jiongbot-hip/mem-ledger.git
cd mem-ledger
cp config.example.toml config.toml
# 改 [paths] 里的路径，改 [importers.docs] 的 dirs 指向你自己的 markdown

python3 cli.py config --check
python3 cli.py build
python3 cli.py search "你想问的"
python3 cli.py stats
```

装成命令（可选）：

```bash
pip install -e .
mem build && mem search "你想问的"
```

## 命令

```
mem build   [--report] [--strict]      重建账本（写路径的唯一提交者）
mem search  <词> [-k 8] [--as-of 日期] 检索；--as-of 回答"那天我们以为是什么"
mem stats                              条数、状态、证据、来源分布
mem query   <词>                       子串回查，不是检索，只是翻账本
mem gaps                               三张缺口清单：无出处 / 越界 / 漏记
mem report  [--out FILE]               冲突报告
mem verify  <id> --basis …             记一条裁决：这条我核实过
mem supersede <旧> --by <新> --reason … 这条被新的取代了
mem reject  <id> --why …               这条主张本身是错的
mem stale   <id> --why …               曾经对、现在没法确认
mem chain   [id]                       取代链检查，或查一条链
mem propose --claim …                  写一条待裁决的主张
mem ttl                                按谓词类型报告哪些该复核了
mem dream                              从冲突里生成整合提案（不自动应用）
mem eval    --out FILE                 对评测集吐预测
mem api     [--port 18092]             只读 HTTP 接口
mem config  [--check]                  看当前配置
mem self-test                          全部自检
```

## 数据契约

一条 claim 长这样：

```json
{
  "id": "cl_7a4d163e968d",
  "scope": "global",
  "subject": "gateway",
  "predicate": "port",
  "value": "18091",
  "claim": "网关监听 127.0.0.1:18091",
  "status": "verified",
  "evidence": "measured",
  "visibility": "shared",
  "agent_id": "me",
  "valid_from": "2026-11-14",
  "captured_at": "2026-11-17",
  "source_refs": [{"kind": "fact", "id": "f_123", "quote": "..."}],
  "revalidate_after": null
}
```

几个字段值得单独说：

`status` 只增不减：`proposed`（抽取结果，没人核实过）、`verified`（核实过）、
`rejected`（判错了）、`superseded`（被新的取代）、`stale`（曾经对，现在确认不了）。

`evidence` 是证据等级，参与打分：`user` > `measured` > `registry` > `extracted` > `inferred`。
用户亲口说的最重，模型推断的最轻，这个顺序直接写进重排权重。

`valid_from` 和 `captured_at` 是两个东西。前者是这条事实自己声称的生效日期，
后者是构建进账本的那天。两者混着显示，1871 条没有日期的条目会全部显示成"今天"，
读的人会以为那个日期有意义。

`visibility` 四档：`private` / `shared` / `team:<名>` / `global`。
字段缺失等于未知等于拒绝。这条不是形式主义：规矩层投影原来没写这个字段，
加上作用域闸门的瞬间，41 条规矩会被全部吃掉。

## 检索怎么工作

```
查询 → 同义词归一 → 中文二元组切分 → BM25-lite 打分
     → 实体门（查询点名的账本主语，文档必须也提到）
     → 重排：0.65 相关度 + 0.25 证据分 + 0.10 新鲜度
     → 合并同一事实的多来源条目
     → top-k
```

三个刻意的设计：

注入串自带 `id / layer / status / evidence / 日期 / 出处 / agent / 可见性`。
读的人（或模型）能分清这是指令还是事实、是核实过的还是抽取的。

同一事实的多个来源在检索层合并，账本里不动。多条来源说同一件事不是冗余，
是跨源互证的证据；在账本里删掉等于把证据抹了。合并只发生在取 top-k 之前，
省下来的名额自动让给下一条。

`--as-of 2026-11-01` 回答"那天账本上是什么"。存了历史不等于能问历史。

## 配置

所有机器相关的东西都在 `config.toml`，代码里不写死。缺一项直接报错，不给默认值。
默认值漂了没人会发现，而它影响的是每一次检索。

`config.example.toml` 里每个键都写了为什么是这个值。那些值是原语料上实测出来的，
不是通用最优解。换语料请自己建评测集，别照抄数字。

## 往外接

`mem api` 起一个只读 HTTP 服务（默认 `127.0.0.1:18092`），token 绑 agent 身份，
没有写端点，拒绝绑定 `0.0.0.0`。GET `/search`、`/rules`、`/claim/<id>`、`/stats`。

别的框架要读这份账本，包一层 MCP 就行。stdio 上每行一个 JSON-RPC 消息，
暴露四五个只读工具。这样各家原生记忆库一个字节都不用动，互通靠读同一份账本。

一个必须守住的细节：身份缺失、token 不对、服务不通，一律报错，绝不返回空结果。
空结果会被模型当成"账本里确实没有"，那是制造假阴性。

## 加一个数据来源

导入器就是一段协议：

```python
NAME = "my_source"
def run(cfg):
    return [claim_dict, ...], {"total": 100, "imported": 42}
```

放两个位置之一：

- 包内：`memlib/importers/my_source.py`，自动被发现，不用登记。
- 包外：把 `.py` 丢进 `config.toml` 里 `importers.dir` 指向的目录。

然后 `[importers.my_source] enabled = true`，跑 `mem build --report` 看导入多少条，
再跑 `mem gaps` 看它有没有制造无出处条目。

导入器只读来源，不写账本。落盘永远由 `build` 统一做，这是"写路径唯一提交者"。

有一个坑要避开：判据不能拿自己上一轮的产物。如果导入器靠"账本里提到过 X"
来决定收什么，第一次导入后账本里就有了 X，下一轮它会自己证明自己该被导入，
清单只增不减。

## 它不做什么

诚实比功能表重要。下面这些是刻意没做的，不是没来得及：

不做双向同步。两套异构记忆库同步一次丢一次字段。互通靠读同一份账本。

不做自动裁决。冲突只报告、只给提案，`override` 必须是人点头。

不做 embedding。整份账本 2000 多条的规模上，BM25 加实体门够用，
而向量检索会把"为什么这条排第一"变成一个查不了的黑盒。规模上万之后再谈。

不做多用户。没有账号体系，身份靠 token 映射。要多人用请自己加一层。

不做前端。输出是纯文本和 JSON。

## 测试

```bash
python3 -m unittest discover -s tests -v   # 端到端：建账本、检索、自检、并发写
python3 cli.py self-test                   # 内置自检，含负向断言
```

自检会打印跳过了多少项。跳过不等于通过。没配 systemd 导入器的机器上，
相关断言会明确标成跳过，而不是"空过"。

## 许可

MIT。见 `LICENSE`。

开发细节、模块地图、改检索参数之前必须走的那道闸门，见 `docs/开发手册.md`。
