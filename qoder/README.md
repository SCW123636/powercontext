# LapCoach · 赛车遥测复盘助手

把每场练习赛的遥测 CSV 变成**可复用的复盘结论**：结论写进 PowerContext，
下一次练车时自动带出来，不用再靠人脑记「上次那个弯我是怎么丢的时间」。

项目网站（Qoder Sites，公开访问）：<https://lapcoach-yeo8k3u7djw.qoder.zone>

## 要解决的问题

车队（或个人练车）每场下来有几十个遥测文件，复盘时要：

- 一个个文件手算圈速差、切分段，找哪一圈更快；
- 逐弯比对刹车点、apex、出弯速度，判断时间丢在哪个弯；
- 把结论写进备忘，**下一场再从头来一遍**，换人接手就彻底断档。

前两步是纯计算，第三步是记忆问题。LapCoach 把两步都做成一条命令。

## 功能

- 解析遥测 CSV（AiM、MoTeC、RaceChrono、iRacing、Harry's Laptimer 等导出），自动分圈、切分段、
  识别弯道的刹车点 / apex / 出弯；
- 找最快圈，和参考圈逐弯对比，指出最亏的弯（含 apex 速度差、刹车点差、分段差）；
- 把结论写成 PowerContext memory：**每名车手一个 Scope**，赛道上限内的历史结论都能被检索；
- 下一次 `ingest` 先检索同赛道的历史结论，输出「记忆复用」进度（圈速、逐弯 apex 变化）；
- `ask` 用自然语言问历史结论，返回带 citation 的条目，并给出 PowerContext `prepare_context`
  组装好的、可直接塞给模型的上下文。

## 技术分工

| 组件 | 在项目里的作用 |
|---|---|
| Qoder | 需求拆解、编码、调试、Qoder Sites 建站 |
| PowerContext 1.2.0 | 记忆与上下文：车手 Scope、memory entry（measurement / observation）、FTS 检索、`prepare_context` |
| 遥测引擎 | 纯标准库 CSV 解析、分圈、分段、弯道识别（见下方来源说明） |

PowerContext 是核心依赖：去掉它，「下次自动带出上次结论」「按问题检索历史」这两个功能都不存在。

## 快速开始

需要 Python 3.11+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
cd qoder
uv sync
uv run lapcoach ingest samples/two_laps.csv --driver alex --track Zandvoort
```

第一次运行会在 `./.lapcoach/` 下把 PowerContext Server 起在本机回环地址上（SQLite 落盘），
不需要另开服务。再跑一次 `ingest`（换成第二天的数据）就能看到「记忆复用」段落：

```bash
uv run lapcoach ask "C6 上我上次损失了多少时间" --driver alex
```

`ask` 会打印命中的记忆条目、citation（entry id + revision），以及 `prepare_context` 组装出的上下文。

常用参数：

- `--data-dir`（或环境变量 `LAPCOACH_DATA_DIR`）：记忆与 Server 数据的落盘目录，默认 `./.lapcoach`；
- `--day`：比赛日期，默认今天；
- `--json`：给脚本用的结构化输出。

已经跑着 PowerContext Server 时，可以复用外部实例：

```bash
export LAPCOACH_POWERCONTEXT_URL=http://127.0.0.1:8000
```

## 目录

```
qoder/
├── lapcoach/
│   ├── cli.py         # ingest / ask 两个命令
│   ├── coach.py       # 复盘逻辑：最快圈、最亏弯、结论文本与进度对比
│   ├── memory.py      # PowerContext：嵌入式 Server、车手 Scope、记忆读写与检索
│   └── telemetry.py   # CSV → 圈 / 分段 / 弯道
├── samples/two_laps.csv
├── site/               # Qoder Sites 项目网站源码（index.html + styles.css）
├── docs/verification.md
└── docs/qoder-dev-log.md
```

## 验证

按 [docs/verification.md](docs/verification.md) 里的步骤跑，能复现两次 ingest 的完整输出、
`ask` 的检索结果和 JSON 结构。

## 来源说明

- `lapcoach/telemetry.py` 复用本队在魔搭社区 Skill「赛车遥测分析」（racing-telemetry-lap-compare）
  中验证过的引擎，改动：从独立脚本拆成可导入模块，去掉 skill 的 CLI 入口，保留全部解析与弯道识别逻辑。
- PowerContext 来自 <https://github.com/oceanbase/powercontext>，以 PyPI 包 `powercontext[cli,server]==1.2.0` 引入。
