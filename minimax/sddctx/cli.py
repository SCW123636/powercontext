"""SDD 上下文模块的命令行入口。

    sddctx ingest                        # 规格/代码/测试 → seekdb
    sddctx context --task "..."          # 检索并组装开发上下文（含 PowerContext 状态）
    sddctx state --done ... --todo ...   # 写入任务状态
    sddctx reset                         # 清空本地数据，回到空状态
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys

from . import config
from .context import build, render_markdown
from .ingest import ingest
from .state import TaskState
from .store import Store


def _cmd_ingest(args: argparse.Namespace) -> int:
    with Store() as store:
        result = ingest(store)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"入库 {result['ingested']} 个条目（spec {result['counts']['spec']} / "
            f"code {result['counts']['code']} / test {result['counts']['test']}），"
            f"清理过期 {result['removed']} 个，库内共 {result['total_in_db']} 个。"
        )
    return 0


async def _gather(task: str) -> dict:
    async with TaskState() as state:
        saved = await state.load()
        prepared = await state.prepare(task)
    return build(task, state=saved, prepared=prepared)


def _cmd_context(args: argparse.Namespace) -> int:
    context = asyncio.run(_gather(args.task))
    if args.json:
        print(json.dumps(context, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(context))
    return 0


async def _record(args: argparse.Namespace) -> list[dict]:
    entries = [("done", item) for item in args.done]
    entries += [("todo", item) for item in args.todo]
    entries += [("verify", item) for item in args.verify]
    if not entries:
        return []
    async with TaskState() as state:
        return await state.record(entries)


def _cmd_state(args: argparse.Namespace) -> int:
    saved = asyncio.run(_record(args))
    print(json.dumps(saved, ensure_ascii=False, indent=2))
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    home = config.home_dir()
    if home.exists():
        shutil.rmtree(home)
    print(f"已清空 {home}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sddctx", description="SDD 开发上下文模块")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="导入规格、代码与测试到 seekdb")
    p_ingest.add_argument("--json", action="store_true")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_context = sub.add_parser("context", help="为开发任务组装上下文")
    p_context.add_argument("--task", required=True, help="开发任务描述")
    p_context.add_argument("--json", action="store_true")
    p_context.set_defaults(func=_cmd_context)

    p_state = sub.add_parser("state", help="写入任务状态到 PowerContext")
    p_state.add_argument("--done", action="append", default=[], help="已完成事项，可重复")
    p_state.add_argument("--todo", action="append", default=[], help="待办事项，可重复")
    p_state.add_argument("--verify", action="append", default=[], help="验证结果，可重复")
    p_state.set_defaults(func=_cmd_state)

    p_reset = sub.add_parser("reset", help="清空 seekdb 与 PowerContext 本地数据")
    p_reset.set_defaults(func=_cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
