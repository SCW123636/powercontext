# Qoder 开发记录

项目全程在 Qoder（桌面端 Agent）里完成：需求拆解、写代码、装环境、跑验证、修 bug。
下面按时间顺序记录关键节点，便于核对哪些工作由 Qoder 完成。

日期：2026-10-28

## 1. 需求拆解

输入：赛题页（`oceanbase.yuque.com/.../uygu3273xzlrcmrm`）与提交要求的原文。

Qoder 读完之后给出的拆解：

- 题目要求「围绕工作提效或学习科研」做一个项目，**必须**用 seekdb 或 PowerContext 之一，
  并且网站**必须**用 Qoder Sites 发布；
- 评审会按运行说明跑通主要功能，所以要选一个「评审能在本机 5 分钟内跑起来」的技术组合；
- 选定 PowerContext 作为记忆层（这是题目的核心差异点），赛题「参考方向」里的
  「学习助手 / 科研流程工具」正好对应「记住历史结论」。

落点：把车队复盘里最重复的两件事（算圈速差、记住上周的结论）做成一条命令。

## 2. 技术选型

| 决策 | 结论 | 理由 |
|---|---|---|
| 记忆层 | PowerContext 1.2.0（PyPI `powercontext[cli,server]`） | 有 Python SDK + HTTP API；SQLite 后端可以嵌进 CLI，评审不用另起服务 |
| 记忆粒度 | 每名车手一个 Scope，条目分 `measurement` / `observation` | Scope 天然隔离不同车手；两类条目分别对应「整场结论」和「单个弯的问题」 |
| 遥测解析 | 复用本队 600075 魔搭 Skill 里验证过的引擎 | 已覆盖 AiM/MoTeC/RaceChrono 等列名与单位，避免重写解析器 |
| 交付形态 | CLI（`ingest` / `ask`） | 车队工程师本来就在终端里干活；也便于评审逐条核对输出 |

## 3. 实现过程

Qoder 依次完成：

1. 在 `2026-competition-ecosystem` 分支的 `qoder/` 目录建工程骨架（`pyproject.toml`、包结构、样例数据）；
2. 把 600075 的遥测引擎从独立脚本改成可导入模块（保留解析与弯道识别逻辑，去掉 skill 的 CLI 入口）；
3. 写 `memory.py`：在进程内拉起 PowerContext Server（回环端口 + SQLite 落盘），封装 Scope 绑定、
   `remember_memory`、`search_memory`、`prepare_context`；
4. 写 `coach.py`：最快圈、参考圈逐弯差值、结论文本（人读一句 + 机器可解析的一行 tailer）、
   与历史记忆的进度对比；
5. 写 `cli.py`：`ingest` / `ask`，支持 `--json`；
6. 在 WSL 里 `uv sync` 装依赖，跑通两次 ingest + ask，逐条对照输出修 bug。

## 4. 调试记录（Qoder 定位并修复）

| 现象 | 定位 | 修复 |
|---|---|---|
| 「记忆复用」只比较圈速，没有逐弯变化 | `parse_measurement` 按空格切分，`corners=C1=99.7 C2=161.1 …` 只解析到 C1 | tailer 里的 corners 改成逗号分隔，解析按整个字段取 |
| 第二天 C6 明显变快，进度里却显示没变 | 记录的是参考圈的 apex，不是最快圈的 apex | 新增 `corner_rows()`，统一用最快圈的弯道数据，差值仍来自参考圈 |
| 观察条目里「apex 比参考圈低 1.0 km/h」符号矛盾 | 用了 `abs()` 但固定写「低」 | 按差值符号输出「低/高」，刹车点改为「早/晚 N m」 |
| 中文问句检索不到记忆 | FTS 对 CJK 整句不命中；`C6` 这类词可以 | `ask` 先从问题里抽英数关键词（弯号、赛道名）再检索，并打印实际用的关键词 |

## 5. 验证

按 [verification.md](verification.md) 跑了：

- 第一次 ingest（无历史）→ 基线分支；
- 第二次 ingest（有历史）→ 打印「记忆复用」三条，含 `C6 apex 66.1 → 70.0 km/h（↑3.9）`；
- `ask "C6 上我上次损失了多少时间"` → 命中 3 条，带 entry_id + revision，并输出 `prepare_context` 组装的上下文；
- `--json` 结构化输出；
- 边界：空记忆、单车文件（无亏损弯 → 不写 observation）、坏 CSV（报错不写记忆）、换车手（Scope 隔离）。

## 6. 站点

Qoder Sites 建站与发布记录见本节后续提交（站点地址在项目 README 与提交材料中）。

Qoder 账号 UID：（提交时填写，用于核对站点归属与使用记录）
