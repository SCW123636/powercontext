# 开发上下文：为工单列表增加优先级过滤（list_orders），并让 CSV 导入（load_csv）对缺失或非法优先级有明确行为

## 适用规格
- `SPEC-4.1` — specs/work-orders.md#SPEC-4.1 (r1, fulltext)

```
## SPEC-4.1 CSV 导入

`fleetdesk.importer.load_csv(path)` 读取工单 CSV：

- 必需列：`id`、`vehicle`、`status`、`opened_on`；缺列时抛 `MissingColumnError`；
- `priority` 列可以缺失：整列缺失时所有工单按默认优先级 `P2` 导入；
- `priority` 单元格为空时同样按 `P2` 处理；
- 非法优先级按 SPEC-3.1 抛 `InvalidPriority`，并带上 CSV 行号；
- 合法优先级统一存为大写。

修订：1
```
- `SPEC-3.2` — specs/work-orders.md#SPEC-3.2 (r1, fulltext)

```
## SPEC-3.2 按优先级过滤工单

`WorkOrderService.list_orders` 增加可选参数 `priority`：

- `priority=None` 时行为与 SPEC-2.1 完全一致；
- 给定 `priority` 时只返回该优先级的工单；
- `status` 与 `priority` 同时给出时取交集；
- 传入手工构造的非法优先级（例如 `"P9"`）时必须抛 `InvalidPriority`，不允许静默返回空列表；
- 传入小写（例如 `"p1"`）时按 SPEC-3.1 归一化后比较。

修订：1
```
- `SPEC-2.1` — specs/work-orders.md#SPEC-2.1 (r1, fulltext)

```
## SPEC-2.1 列出工单

`WorkOrderService.list_orders(status=None)` 返回工单列表：

- `status=None` 时返回全部工单，保持导入顺序；
- `status` 给定时只返回状态完全相等的工单，状态取值仅限 `open`、`in_progress`、`done`。

修订：1
```
- `SPEC-3.1` — specs/work-orders.md#SPEC-3.1 (r1, fulltext)

```
## SPEC-3.1 优先级取值

- 工单优先级只有四个取值：`P0`、`P1`、`P2`、`P3`，其中 `P0` 最高。
- 任何来源的优先级都必须归一化为大写后再存储与比较；`p1`、` p1 ` 等同于 `P1`。
- 出现四个取值以外的优先级（例如 `P9`、`high`、空串）时，必须抛出 `fleetdesk.models.InvalidPriority`。
- 异常消息里要带上出错位置；来自 CSV 时必须包含该行在文件中的行号（表头是第 1 行）。

修订：1
```

## 需要修改的代码
- `fleetdesk/importer.py::load_csv` (r1, fulltext, score 51.2608)
- `fleetdesk/cli.py::main` (r1, fulltext, score 50.7582)
- `fleetdesk/service.py::WorkOrderService` (r1, fulltext, score 46.3121)
- `fleetdesk/cli.py::build_parser` (r1, fulltext, score 33.2879)
- `fleetdesk/__init__.py::__init__` (r1, fulltext, score 32.2)

## 用于验证的测试
- `tests/test_csv_priority.py::test_empty_priority_cell_defaults_to_p2` (fulltext, score 56.3944)
- `tests/test_priority_filter.py::test_status_and_priority_intersect` (fulltext, score 40.8043)
- `tests/test_csv_priority.py::test_invalid_priority_reports_csv_line_number` (fulltext, score 38.5229)
- `tests/test_regression.py::test_missing_note_column_becomes_empty` (fulltext, score 38.4937)
- `tests/test_priority_filter.py::test_unknown_priority_filter_raises` (fulltext, score 34.5591)
- `tests/test_csv_priority.py::test_missing_priority_column_defaults_to_p2` (fulltext, score 33.3778)

## 任务状态（PowerContext）
- 已完成：
  - 从 seekdb 检索上下文：规格 4 条、代码 5 个符号、测试 6 个
  - MiniMax 完成代码修改（3 轮）：fleetdesk/importer.py, fleetdesk/service.py
- 待办：
  - （无）
- 验证结果：
  - 回归测试 exit=0，7 passed in 0.19s
  - 目标测试 exit=0，9 passed in 0.28s

## 数据来源
- seekdb：seekdb (embedded, FULLTEXT ngram)，库内 39 个条目
- PowerContext Scope：`scp_3dc0maejq0pv751729ebb5nv4d`
