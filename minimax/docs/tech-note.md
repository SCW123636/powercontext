# 技术说明

## 一句话

规格与代码测试进 seekdb 做全文检索，任务进展进 PowerContext 做跨会话状态，检索结果交给 MiniMax
改代码，测试结果再回灌——形成「规格 → 上下文 → 修改 → 验证 → 状态」的闭环。

## 分层

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 存储层 | seekdb（`pylibseekdb` 嵌入式） | 规格 / 代码 / 测试切块存储与 ngram 全文检索 |
| 上下文层 | `sddctx/ingest.py`、`context.py` | 切块入库、按任务检索、组装带引用的上下文 |
| 状态层 | PowerContext 1.2.0（内嵌 uvicorn） | 保存已完成 / 待办 / 验证结果，跨进程读取 |
| 执行层 | `agent/run_agent.py` | 调 MiniMax、落地文件、跑测试、失败回灌 |
| 演示层 | `sddctx/cli.py`、`scripts/` | ingest / context / state / inspect / reset / 规格热更新 |

## 数据流

```
specs/*.md ─┐
fleetdesk/*.py ─┼─ ingest（分节 / AST 切块）─→ seekdb.documents（39 条，FTS ngram）
tests/*.py ─┘                                        │
                                                     ▼
任务描述 ────────────────→ context.build ─→ 规格 4 + 代码 5 + 测试 6
                                                     │        ▲
                                                     │        └── PowerContext 状态（scope 持久化）
                                                     ▼
                                          MiniMax-M3（返回完整文件）
                                                     │
                                    落地 fleetdesk/*.py → pytest 目标 + 回归
                                                     │
                                          失败输出 + 规格片段回灌（≤3 轮）
                                                     ▼
                                     状态写回 PowerContext（done / todo / verify）
```

## seekdb 存储设计

- 单表 `documents`，主键 `id = "<kind>:<path>#<anchor>"` 或 `"<kind>:<path>::<symbol>"`。
  重新导入时按 id upsert，并删除源文件已不存在的条目（`removed` 计数）。
- 全文索引 `FULLTEXT KEY ft_body(body) WITH PARSER ngram (ngram_token_size=2)`：中文按二字切分，
  无需外部 embedding 服务，离线可复现。
- 选择裸 SQL 而非 `pyseekdb` collection API：collection 写入要求 embedding 函数，评测环境不宜依赖
  模型下载；裸 SQL 只依赖本地引擎。
- 嵌入式模式要求数据目录在 Linux 本地盘（`SDDCTX_HOME`），9p 挂载会打开失败。

## 上下文选择

1. **规格先行**：用任务描述对 `kind='spec'` 做 `MATCH ... AGAINST`，再叠加词面覆盖，
   取前 4 条。
2. **符号扩展**：从命中规格里抽取出 `list_orders`、`load_csv`、`SPEC-3.2` 这类标识符，
   并入任务词一起检索代码与测试；规格点名过的符号权重更高。
3. **打分**：`3 × 归一化全文分 + 1.2 × 词面命中数 + 4 × 标识符命中 + 2 × SPEC 编号命中`，
   同分按 id 排序，保证可重复。
4. **输出**：规格给正文，代码/测试给引用（`path::symbol`），并附上文件全文（≤4 个）供模型直接改；
   每条都带 `path#anchor` / `path::symbol` 与修订号，来源可追溯。

## 状态设计（PowerContext）

- 每个 `SDDCTX_HOME` 一个 scope，`scope.json` 记住 `scope_id`，模块重启后继续用同一个 scope。
- 写入用 `remember_memory(kind=done|todo|verify)`，文本统一加 `[SDDCTX]` 前缀；读取时按
  `entry.kind` 分桶，不依赖文本内容解析。
- 组装上下文时还会调用 `prepare_context(query=任务)`，把 PowerContext 自己挑出的相关记忆放进
  `powercontext.prepared_context` 字段。
- 内嵌 server 绑 `127.0.0.1:0` 随机端口，进程退出即停，评审无需单独部署。

## 为什么默认关闭思考

MiniMax 开启思考时单轮约 200 s、2.4 万 token，且容易把 16k 输出预算耗在推理上导致 `finish=length`、
正文为空。本任务是「按规格补一个小功能」，关闭思考后 3 轮共 14 256 tokens、13.6 s 完成。
需要复杂重构时可用 `MINIMAX_THINKING=auto` 打开。

## 边界

- 检索是词面匹配，不做语义泛化；同义改写（如「紧急程度」↔「优先级」）要靠规格里显式写词。
- 代码只切到类/方法，跨文件调用链不自动展开，由规格中的符号名驱动。
- 编码助手只写 `fleetdesk/*.py` 并做路径包含校验，防止模型改测试或写到仓库外。
