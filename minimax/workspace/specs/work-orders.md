# FleetDesk 工单规格

本文件是 FleetDesk 的行为规格。每条规格有稳定编号（`SPEC-x.y`），代码与测试都引用这些编号。
规格变更时递增条目内的「修订」号，重新导入上下文后应立刻生效。

## SPEC-2.1 列出工单

`WorkOrderService.list_orders(status=None)` 返回工单列表：

- `status=None` 时返回全部工单，保持导入顺序；
- `status` 给定时只返回状态完全相等的工单，状态取值仅限 `open`、`in_progress`、`done`。

修订：1

## SPEC-3.1 优先级取值

- 工单优先级只有四个取值：`P0`、`P1`、`P2`、`P3`，其中 `P0` 最高。
- 任何来源的优先级都必须归一化为大写后再存储与比较；`p1`、` p1 ` 等同于 `P1`。
- 出现四个取值以外的优先级（例如 `P9`、`high`、空串）时，必须抛出 `fleetdesk.models.InvalidPriority`。
- 异常消息里要带上出错位置；来自 CSV 时必须包含该行在文件中的行号（表头是第 1 行）。

修订：1

## SPEC-3.2 按优先级过滤工单

`WorkOrderService.list_orders` 增加可选参数 `priority`：

- `priority=None` 时行为与 SPEC-2.1 完全一致；
- 给定 `priority` 时只返回该优先级的工单；
- `status` 与 `priority` 同时给出时取交集；
- 传入手工构造的非法优先级（例如 `"P9"`）时必须抛 `InvalidPriority`，不允许静默返回空列表；
- 传入小写（例如 `"p1"`）时按 SPEC-3.1 归一化后比较。

修订：1

## SPEC-4.1 CSV 导入

`fleetdesk.importer.load_csv(path)` 读取工单 CSV：

- 必需列：`id`、`vehicle`、`status`、`opened_on`；缺列时抛 `MissingColumnError`；
- `priority` 列可以缺失：整列缺失时所有工单按默认优先级 `P2` 导入；
- `priority` 单元格为空时同样按 `P2` 处理；
- 非法优先级按 SPEC-3.1 抛 `InvalidPriority`，并带上 CSV 行号；
- 合法优先级统一存为大写。

修订：1

## SPEC-4.2 导入回归

- 既有数据文件 `data/work_orders.csv`（含 `priority` 列）导入结果不得变化：id、vehicle、status、priority、opened_on、note 六列逐字段保持；
- `note` 列缺失时为空串；
- 状态值导入时转小写。

修订：1

## SPEC-5.1 统计

`WorkOrderService.counts_by_status()` 固定返回 `open`、`in_progress`、`done` 三个键，
没有工单的状态计为 0。

修订：1
