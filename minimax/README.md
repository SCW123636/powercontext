# SDD 上下文管理 × MiniMax（600077）

规格驱动开发（Spec-Driven Development）的上下文管理模块：把业务工程的**规格、代码、测试**导入
seekdb，按开发任务检索出「该看哪几条规格、该改哪个函数、该跑哪些测试」，交给由 MiniMax 驱动的
编码助手真实修改代码，跑通功能测试与回归测试，并把任务状态写进 PowerContext 供下次会话复用。

业务工程是 `workspace/` 下的 FleetDesk 工单服务（Python，自带 16 个 pytest 用例）。
本轮任务是：给 `WorkOrderService.list_orders` 增加优先级过滤，并让 `load_csv` 对缺失或非法优先级有明确行为。

## 目录

```
sddctx/                 上下文模块
  config.py             路径与环境变量
  store.py              seekdb 存取（建表、全文索引、检索）
  ingest.py             规格分节 / Python AST 切块入库
  context.py            规格先行的上下文检索与组装
  state.py              PowerContext 任务状态（内嵌 server）
  cli.py                命令行入口
agent/run_agent.py      编码助手：调用 MiniMax → 解析文件 → 落地 → 跑测试 → 回灌失败信息
scripts/demo_spec_update.py  规格热更新演示（改规格 → 重新导入 → 上下文立即变化）
workspace/              业务工程 + 规格 + 测试 + 数据
evidence/               运行证据（测试日志、快照、diff、会话 JSON、检索结果）
docs/                   验证记录、开发记录、技术说明
```

## 依赖与安装

- Python ≥ 3.11（`pylibseekdb` 目前只有 Linux 轮子；macOS/Windows 请用 WSL/Linux 容器）。
- 依赖：`pylibseekdb`、`powercontext[cli,server]==1.2.0`、`httpx`。

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

或用 uv：`uv sync`（`pyproject.toml` 已声明 `[tool.uv] package = true`）。

## 配置

```bash
export SDDCTX_HOME=/tmp/sddctx-home   # seekdb 与 PowerContext 的数据目录
export MINIMAX_API_KEY=<填入你自己的 key>
```

- `SDDCTX_HOME` **必须放在 Linux 本地文件系统**。放在 `/mnt/d` 这类 9p 挂载上 seekdb 会打不开
  （`seekdb_open failed: code=-1`）。
- `MINIMAX_API_KEY` 只从环境变量读取，**不要写进仓库或文档**；仓库里不保存任何 key。
- 可选：`MINIMAX_MODEL`（默认 `MiniMax-M3`）、`MINIMAX_BASE_URL`（默认 `https://api.minimaxi.com`）、
  `MINIMAX_THINKING=auto`（默认关闭思考，开启后单轮约 200 s / 2.4 万 token，不建议）。
- `SDDCTX_WORKSPACE` 可指向别的业务工程目录，默认 `workspace/`。

## 使用

```bash
sddctx ingest                    # 规格/代码/测试 → seekdb（全量 upsert + 清理过期条目）
sddctx context --task "为工单列表增加优先级过滤…"        # 打印 Markdown 上下文
sddctx context --task "…" --json                        # 同一份上下文的机器可读版本
sddctx state --done "…" --todo "…" --verify "…"         # 写任务状态到 PowerContext
sddctx inspect --query "优先级过滤"                      # 看建表语句、条目数、一次全文检索
sddctx reset                     # 清空 seekdb 与 PowerContext 本地数据
```

编码助手（会真实调用 MiniMax 并修改 `workspace/fleetdesk/`）：

```bash
export MINIMAX_API_KEY=...
PYTHONPATH=. python agent/run_agent.py \
  --task "为工单列表增加优先级过滤（list_orders），并让 CSV 导入（load_csv）对缺失或非法优先级有明确行为" \
  --rounds 3
```

离线回放已保存的会话（不调用 MiniMax）：

```bash
PYTHONPATH=. python agent/run_agent.py --task "<同上>" --replay evidence/minimax/session-<stamp>.json
```

测试：

```bash
python -m pytest workspace/tests/test_priority_filter.py workspace/tests/test_csv_priority.py -q   # 功能
python -m pytest workspace/tests/test_regression.py -q                                              # 回归
```

## 已验证结果

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| seekdb 入库 | 39 条（规格 6 / 代码 17 / 测试 16），ngram 全文索引 | `evidence/seekdb-query.json` |
| 任务上下文 | 规格 4 / 代码 5 符号 / 测试 6，均带来源引用 | `evidence/context-after-restart.md` |
| MiniMax 修改 | 3 轮真实调用，改 `fleetdesk/importer.py`、`fleetdesk/service.py` | `evidence/minimax/session-*.json` |
| 功能测试 | 修改前 9 failed → 修改后 9 passed | `evidence/tests/before-target.log`、`round3-target.log` |
| 回归测试 | 修改前后均 7 passed | `evidence/tests/round3-regression.log` |
| 任务状态复用 | 新进程读取上下文，已完成/待办/验证结果都在 | `evidence/context-after-restart.md` |
| 规格热更新 | 改 SPEC-4.1 → 重新导入 → 上下文 r2 使用新内容；恢复后回到 r1 | `evidence/spec-update/` |

完整复现步骤见 `docs/verification.md`，逐轮开发记录见 `docs/dev-log.md`，模块设计见 `docs/tech-note.md`。

## 已知限制

- `pylibseekdb` 为嵌入式单机模式，表结构固定为 `documents`，检索是词面 ngram 全文匹配，不含语义向量。
- 代码切块到「类 / 方法」粒度，跨文件调用链不会自动展开（由规格点名的符号驱动扩展）。
- 编码助手只允许改 `fleetdesk/*.py`，禁止改测试、规格与数据；这是本任务的约束，不是通用策略。
- MiniMax 调用默认关闭思考；对复杂重构可能需要 `MINIMAX_THINKING=auto`，代价是耗时与 token 显著上升。
