# MiniMax 开发记录

本轮代码修改全部由 MiniMax 完成：`agent/run_agent.py` 把 SDD 上下文（规格 + 代码全文 + 测试全文）
发给 MiniMax，模型返回需要改动的完整文件，脚本落地后跑测试，失败输出再回灌，最多 3 轮。

- 模型：`MiniMax-M3`
- 端点：`POST https://api.minimaxi.com/v1/text/chatcompletion_v2`
- 参数：`temperature=0.2`、`max_tokens=16000`、`thinking={"type":"disabled"}`
- 会话记录：`evidence/minimax/session-20261028T133152Z.json`（含每轮原始请求/响应）

## 调用记录

| 轮次 | request_id | 用量 | 耗时 | finish | 目标测试 | 回归测试 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `07312c5051d3ec63d82eda4ea3df21c9` | 2917 tokens（prompt 1971 / completion 946） | 5.33 s | stop | 2 failed, 7 passed | 7 passed |
| 2 | `07312c57968730fefa9c4f50c425c64f` | 4747 tokens（prompt 3765 / completion 982） | 4.38 s | stop | 1 failed, 8 passed | 7 passed |
| 3 | `07312c5ea429fae93bea04c7cae45352` | 6592 tokens（prompt 5597 / completion 995） | 3.88 s | stop | 9 passed | 7 passed |

合计 14 256 tokens、约 13.6 s；`prompt_tokens_details.cached_tokens` 分别为 1969 / 2915 / 4745，
命中 MiniMax 侧的 prompt 缓存。

## 逐轮改动与失败回灌

**第 1 轮**：模型给出 `list_orders(status=None, priority=None)` 的大小写归一化过滤，并让
`load_csv` 在缺列或空单元格时用 `P2`、非法值报「第 N 行」。
测试结果 2 failed：

- `test_lowercase_priority_is_normalized`：`['p1', 'p0'] != ['P1', 'P0']` —— CSV 侧的优先级没有转大写；
- `test_invalid_priority_reports_csv_line_number`：异常消息里有行号，但 `InvalidPriority` 是通过
  `raise type(exc)(...)` 重新构造的，`line` 属性丢失。

**第 2 轮**：回灌上述两段失败输出 + 相关规格摘录。模型改为先 `raw.upper() or DEFAULT_PRIORITY`
再校验，`load_csv` 直接 `raise InvalidPriority(...)`。8 passed，仍剩
`assert excinfo.value.line == 3` 失败（`line` 仍为 `None`）。

**第 3 轮**：再次回灌单条失败。模型在抛异常前补 `error.line = line_number`。9 passed / 7 passed，
两条测试套件全绿。

最终改动（`evidence/patch.diff`，修改前后全文见 `evidence/snapshots/before|after/`）：

- `fleetdesk/service.py`
  - `list_orders` 新增 `priority` 参数：`strip().upper()` 归一化后匹配；
  - 非 `P0/P1/P2/P3` 输入抛 `InvalidPriority`，不静默返回空列表；
  - `status` 与 `priority` 同时给出时取交集；`priority=None` 时保持原行为。
- `fleetdesk/importer.py`
  - 新增 `DEFAULT_PRIORITY = "P2"`、`VALID_PRIORITIES`；
  - `enumerate(reader, start=2)` 记录 CSV 行号（表头为第 1 行）；
  - 缺失列 / 空单元格 → `P2`；非法值 → `InvalidPriority`，消息含文件名与行号，并设置 `.line`；
  - 合法值统一转大写后存入 `WorkOrder`。

## 上下文如何进入提示词

`render_prompt()` 组装四块内容，全部来自 seekdb 检索结果，不手工粘贴：

1. 任务描述；
2. 适用规格（带 `path#SPEC-x.y`、修订号）；
3. 相关代码文件全文（`context['files']`）；
4. 验证测试全文。

失败回灌时再追加当前测试输出与命中的规格片段，模型据此定位差异。

## 助手之外的人工修正

以下问题出在运行框架，不在模型输出，均由人工修复，不影响「代码由 MiniMax 修改」这一事实：

- PowerContext 条目读取：最初按文本里的中文冒号解析，导致状态为空；改为按 `entry.kind`
  （done / todo / verify）分桶。
- 会话落盘：`MemoryCitation` 不可 JSON 序列化；改存 `citation.entry_id` 与 `version`。
- seekdb 打开：连续进程访问偶发 `seekdb_open failed`；`Store` 增加 6 次带退避的重试。
- JSON 解析：模型输出偶有未转义换行；解析增加去围栏 + `json.loads(..., strict=False)` 兜底。
