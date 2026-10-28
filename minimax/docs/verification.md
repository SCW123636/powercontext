# 600077 验证记录

本文给出一次从零开始的完整复现步骤与预期结果。所有证据文件都在 `evidence/` 下。

## 1. 验证对象

- 业务工程：`workspace/`（FleetDesk 工单服务，纯 Python + pytest）
- 开发任务：给 `WorkOrderService.list_orders` 增加优先级过滤；让 `load_csv` 对「缺列 / 空值 / 非法值」有明确行为
- 规格：`workspace/specs/work-orders.md`（SPEC-2.1 / 3.1 / 3.2 / 4.1 / 4.2 / 5.1）
- 代码：`workspace/fleetdesk/{models,importer,service,cli}.py`
- 测试：`workspace/tests/test_priority_filter.py`（5）、`test_csv_priority.py`（4）、`test_regression.py`（7）
- 初始数据：`workspace/data/work_orders.csv`（正常数据）、`work_orders_no_priority.csv`（缺列）、
  `work_orders_bad_priority.csv`（第 3 行非法优先级）、`work_orders_lower_priority.csv`（小写优先级）

修改前 `list_orders` 只有 `status` 参数，`load_csv` 原样保存优先级，所以 9 个功能用例全失败。

代码版本：修改前 = 提交 `13fd41cd`；修改后 = 提交 `c3bad0ba`（引入本轮代码改动与证据）。
`evidence/snapshots/before|after/` 与 `evidence/patch.diff` 对应这两个版本。

## 2. 需要导入的材料

`sddctx ingest` 会把三份材料切块后写入 seekdb。切块规则：

| 材料 | 切块方式 | 条目数 |
| --- | --- | --- |
| 规格 `specs/*.md` | 按 `## SPEC-x.y` 分节，保留「修订：N」 | 6 |
| 代码 `fleetdesk/*.py` | AST：类一条 + 每个方法一条，符号名 `Class.method` | 17 |
| 测试 `tests/test_*.py` | 每个 `test_*` 函数一条 | 16 |

合计 **39** 条，存于 seekdb 表 `sdd.documents`，带 ngram 全文索引：

```sql
CREATE TABLE `documents` (
  `id` varchar(256) NOT NULL, `kind` varchar(16) NOT NULL,
  `path` varchar(256) NOT NULL, `anchor` varchar(128) NOT NULL DEFAULT '',
  `symbol` varchar(128) NOT NULL DEFAULT '', `body` text NOT NULL,
  `revision` int(11) NOT NULL DEFAULT '1', `updated_at` varchar(32) NOT NULL DEFAULT '',
  PRIMARY KEY (`id`),
  FULLTEXT KEY `ft_body` (`body`) WITH PARSER ngram PARSER_PROPERTIES=(ngram_token_size=2)
) ORGANIZATION INDEX DEFAULT CHARSET = utf8mb4 ROW_FORMAT = DYNAMIC
  COMPRESSION = 'zstd_1.3.8' BLOCK_SIZE = 16384 TABLET_SIZE = 134217728 PCTFREE = 0
```

完整建表语句与检索命中的证据：`evidence/seekdb-query.json`。

## 3. 复现步骤

在 Linux / WSL 里执行（`pylibseekdb` 只提供 Linux 轮子；数据目录必须放本地盘）：

```bash
cd minimax
python -m venv .venv && . .venv/bin/activate && pip install -e .

export SDDCTX_HOME=/tmp/sddctx-home      # 不要放在 /mnt/d
export MINIMAX_API_KEY=<你自己的 key>

# 0) 干净状态
sddctx reset

# 1) 导入规格 / 代码 / 测试
sddctx ingest
# → 入库 39 个条目（spec 6 / code 17 / test 16）

# 2) 基线：修改前跑一遍测试
python -m pytest workspace/tests/test_priority_filter.py workspace/tests/test_csv_priority.py -q   # 9 failed
python -m pytest workspace/tests/test_regression.py -q                                              # 7 passed

# 3) 用上下文驱动 MiniMax 修改代码（真实调用，最多 3 轮）
PYTHONPATH=. python agent/run_agent.py \
  --task "为工单列表增加优先级过滤（list_orders），并让 CSV 导入（load_csv）对缺失或非法优先级有明确行为" \
  --rounds 3

# 4) 重启复用：新进程读上下文，任务状态应从 PowerContext 回来
sddctx context --task "为工单列表增加优先级过滤（list_orders），并让 CSV 导入（load_csv）对缺失或非法优先级有明确行为" \
  > evidence/context-after-restart.md

# 5) seekdb 检索证据
sddctx inspect --query "优先级过滤 CSV 导入 行号" > evidence/seekdb-query.json

# 6) 规格热更新：改 SPEC-4.1 → 重新导入 → 上下文立即使用新内容 → 恢复
python scripts/demo_spec_update.py
```

离线复现 MiniMax 那一步（不消耗额度、不需要 key）：

```bash
PYTHONPATH=. python agent/run_agent.py \
  --task "为工单列表增加优先级过滤（list_orders），并让 CSV 导入（load_csv）对缺失或非法优先级有明确行为" \
  --replay evidence/minimax/session-20261028T133152Z.json
```

## 4. 预期结果

### 4.1 基线 vs 修改后

| 用例 | 修改前 | 修改后 |
| --- | --- | --- |
| `test_priority_filter.py::test_filter_by_priority` | FAIL | PASS |
| `test_priority_filter.py::test_status_and_priority_intersect` | FAIL | PASS |
| `test_priority_filter.py::test_none_priority_keeps_full_list` | FAIL | PASS |
| `test_priority_filter.py::test_unknown_priority_filter_raises` | FAIL | PASS |
| `test_priority_filter.py::test_lowercase_priority_filter_is_normalized` | FAIL | PASS |
| `test_csv_priority.py::test_missing_priority_column_defaults_to_p2` | FAIL | PASS |
| `test_csv_priority.py::test_empty_priority_cell_defaults_to_p2` | FAIL | PASS |
| `test_csv_priority.py::test_invalid_priority_reports_csv_line_number` | FAIL | PASS |
| `test_csv_priority.py::test_lowercase_priority_is_normalized` | FAIL | PASS |
| 合计 | **9 failed**（0.96 s） | **9 passed**（0.28 s） |
| 回归 `test_regression.py` | 7 passed | **7 passed**（0.19 s） |

证据：`evidence/tests/before-target.log`、`evidence/tests/round3-target.log`、
`evidence/tests/round3-regression.log`；改动内容见 `evidence/patch.diff`，
修改前后的完整文件见 `evidence/snapshots/before|after/`。

### 4.2 上下文输出

`sddctx context` 必须包含四段：适用规格、需要修改的代码、用于验证的测试、任务状态（PowerContext），
且每条都带真实来源引用（`specs/work-orders.md#SPEC-4.1`、`fleetdesk/importer.py::load_csv` 等）。
本次输出：规格 4 条、代码 5 个符号、测试 6 个，见 `evidence/context-after-restart.md`。

本任务上下文必须包含的来源（评审按此核对）：

| 类别 | 必须出现的来源 |
| --- | --- |
| 规格 | `specs/work-orders.md#SPEC-4.1`、`#SPEC-3.2`、`#SPEC-2.1`、`#SPEC-3.1` |
| 代码 | `fleetdesk/importer.py::load_csv`、`fleetdesk/service.py::WorkOrderService`（类条目含 `list_orders` 源码）、`fleetdesk/cli.py::main` |
| 测试 | `tests/test_csv_priority.py::test_invalid_priority_reports_csv_line_number`、`test_missing_priority_column_defaults_to_p2`、`test_empty_priority_cell_defaults_to_p2`、`tests/test_priority_filter.py::test_status_and_priority_intersect`、`test_unknown_priority_filter_raises` |

### 4.3 任务状态复用

`evidence/context-after-restart.md` 是在**独立新进程**里生成的，其中「任务状态（PowerContext）」
应有 2 条已完成、验证结果 2 条（目标测试 / 回归测试 exit=0），说明状态确实落盘并可跨进程读取。
写入明细见 `evidence/powercontext-state.json`，准备上下文的结果见同文件 `prepared_context` 字段。

### 4.4 规格热更新

`scripts/demo_spec_update.py` 会：

1. 记录更新前 SPEC-4.1（r1，默认 `P2`）；
2. 把它改成默认 `P1` 并把该条修订号改成 2；
3. 重新 `sddctx ingest`；
4. 断言上下文里的 SPEC-4.1 变成 r2、默认 `P1`，且引用路径不变；
5. 恢复原规格并再次导入，断言回到 r1。

差异见 `evidence/spec-update/diff.md`，前后快照见 `before.json` / `after.json` / `restored.json`。

## 5. 重置方法

```bash
sddctx reset                     # 删除 SDDCTX_HOME（seekdb + PowerContext）
git checkout -- workspace/       # 业务工程回到未修改状态
```

重置后重新执行第 3 节步骤即可得到同样的结果；`evidence/` 不会被自动删除，便于对照。

## 6. 与题目验收点的对应

| 验收点 | 对应实现 / 证据 |
| --- | --- |
| 选一个可运行业务工程并有测试 | `workspace/fleetdesk` + 16 个 pytest 用例 |
| 导入规格、代码、测试 | `sddctx ingest` → seekdb 39 条，`evidence/seekdb-query.json` |
| 按任务选择上下文 | `sddctx/context.py` 规格先行 + 符号扩展，`evidence/context-after-restart.md` |
| 上下文交给 MiniMax 助手真实改代码 | `agent/run_agent.py`，3 轮 request_id/usage 见 `evidence/minimax/session-*.json` |
| 改完跑功能 + 回归测试 | 9 passed / 7 passed，`evidence/tests/` |
| 保存任务状态、重启后复用 | `sddctx/state.py`（PowerContext），`evidence/context-after-restart.md` |
| 上下文引用真实来源 | 每条带 `path#anchor` 或 `path::symbol` 与修订号 |
| 规格更新并重新导入后输出使用更新内容 | `scripts/demo_spec_update.py`，`evidence/spec-update/` |
