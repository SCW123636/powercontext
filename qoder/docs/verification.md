# 验证样例

环境：WSL2 Ubuntu 22.04、Python 3.12、uv 0.13.5、PowerContext 1.2.0（PyPI）。
所有命令都在 `qoder/` 目录下执行。

## 0. 准备

```bash
cd qoder
uv sync
rm -rf .lapcoach          # 从空记忆开始，便于复现
```

初始数据：`samples/two_laps.csv`（同一天的两圈，第 2 圈在 C6 亏 0.200s）、
`samples/two_laps_day2.csv`（第二天的两圈，C6 apex 抬高约 4 km/h）。

## 1. 第一次复盘：写入基线

```bash
uv run lapcoach ingest samples/two_laps.csv --driver alex --track Zandvoort --day 2026-10-28
```

预期输出（实测）：

```
Zandvoort · 2026-10-28 · two_laps.csv
| lap | time (s) | length (m) | max km/h | mean km/h |
|---|---|---|---|---|
| 1 | 59.8 | 3239.9 | 241.2 | 195.1 |
| 2 | 59.6 | 3238.0 | 241.2 | 195.7 |

最快 2 圈 59.600s，比参考 1 圈快 0.200s
| corner | apex (m) | apex km/h | d apex | d brake (m) | d time (s) |
|---|---|---|---|---|---|
...
| 6 | 2361.2 | 66.1 | -5.3 | -1.9 | 0.2 |
...
最亏：C6 +0.200s, C4 +0.100s

记忆复用（来自 PowerContext）：这个车手还没有同赛道的记录，本次作为基线写入。

写入 memory: mem_ent_54ef5f732969436a92195118ec0cbc75
写入 observation: mem_ent_e3189c79f5e649b48b7735fb0d3a8d1e
写入 observation: mem_ent_f6271051df194a6c9390eccbe14367f0
scope: scp_007b6pv551yqk2nvb6yw6fh7fg
```

此时磁盘上出现：

```
.lapcoach/
├── powercontext/        # 嵌入式 Server 的工作目录 + SQLite（powercontext.db）
└── scopes.json          # 车手 → Scope 绑定
```

## 2. 第二天复盘：记忆被复用

新进程重新运行时，Server 用同一个 SQLite，Scope 由 `scopes.json` 定位：

```bash
uv run lapcoach ingest samples/two_laps_day2.csv --driver alex --track Zandvoort --day 2026-10-29
```

关键输出（实测）：

```
最快 2 圈 59.380s，比参考 1 圈快 0.411s
...
最亏：C4 +0.096s, C8 +0.003s, C3 +0.002s

记忆复用（来自 PowerContext）：
  - 上次记忆：2026-10-28 在 Zandvoort 最快 59.600s
  - 本场最快 59.380s，比上次 快 0.220s
  - C6 apex 66.1 → 70.0 km/h（↑3.9）
```

「上次记忆」这一段不是本地缓存：它来自 `search_memory` 命中的第一天 measurement 条目，
上面的 C6 变化量就是对这条记忆里 `corners=...` 的解析。第一天不存在记忆时，第 1 步会走
「本次作为基线写入」分支 —— 两条分支都实测过。

## 3. 用自然语言查历史结论

```bash
uv run lapcoach ask "C6 上我上次损失了多少时间" --driver alex
```

预期输出（实测，节选）：

```
检索关键词：C6
命中 3 条记忆：
  - Zandvoort 2026-10-28：第 2 圈最快 59.600s，比第 1 圈快 0.200s，最亏的是 C6（+0.200s）。
    来源 mem_ent_5c3e70e882964c629a59b4d4aff1d16a · revision 7
  - Zandvoort 2026-10-28：C6 是全圈最亏的弯，apex 66.1 km/h，比参考圈低 5.3 km/h，刹车点早 2 m（+0.200s）。下一场先练这个弯。
    来源 mem_ent_c5331ad231e54bb4834b6e175d50c081 · revision 7
  ...

PowerContext 组装的上下文（可直接作为消息交给模型）：
PowerContext prepared untrusted historical context.
...
BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1
{"trust":"untrusted_history","items":[{"citation":{...},"content":"...","truncated":false}, ...]}
END_POWERCONTEXT_PREPARED_CONTEXT_V1
```

说明两点口径：

- 记忆检索走 FTS，中文整句不能直接命中，所以 `ask` 先抽问题里的英数关键词（`C6`、赛道名等）再检索；
  不含关键词的问题会退回整句检索。
- 每条命中都带 `entry_id` 与 `revision`，可以用 `powercontext` 的接口按 citation 回查原文。

## 4. 结构化输出

```bash
uv run lapcoach ask "C6 apex" --driver alex --json
```

返回 `{"scope_id": ..., "hits": [{"text","entry_id","revision"}...], "context": "...", "context_status": "ready"}`，
退出码 0，便于脚本/Agent 消费。

## 5. 边界情况

| 场景 | 实测结果 |
|---|---|
| 记忆为空时检索 | 打印「记忆里没有和「…」相关的结论。先跑一次 ingest 把它记下来。」，退出码 0 |
| 新数据没有可比的亏损弯 | `measurement` 照写，`observation` 条数为 0（只在 `d time > 0` 时产生） |
| CSV 缺时间列 / 速度列（或有效行 < 20） | 解析阶段直接报错并列出找过的列名，不写记忆 |
| 同一 `--driver` 重复 ingest | 追加新条目，旧条目仍可被检索（revision 递增） |
| 换一个 `--driver` | 新 Scope，检索不到别人的记忆（Scope 隔离） |
