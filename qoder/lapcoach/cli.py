"""LapCoach 命令行：ingest 复盘并写入记忆，ask 用记忆回答。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from . import coach
from .memory import Memory

DEFAULT_DATA_DIR = ".lapcoach"


def _memory_from(args: argparse.Namespace) -> Memory:
    data_dir = Path(os.environ.get("LAPCOACH_DATA_DIR", args.data_dir)).expanduser()
    return Memory(data_dir, url=os.environ.get("LAPCOACH_POWERCONTEXT_URL"))


async def _ingest(args: argparse.Namespace) -> int:
    analysis = coach.analyse(args.file, args.track, args.day or coach.today())
    if args.json:
        print(json.dumps({"analysis": {**analysis.__dict__, "fastest": analysis.fastest.number,
                                       "reference": analysis.reference.number,
                                       "best_s": analysis.best_s, "delta_s": analysis.delta_s}},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(coach.report(analysis))

    async with _memory_from(args) as memory:
        scope_id = await memory.scope(args.driver)
        previous = None
        for hit in await memory.search(scope_id, f"{args.track} measurement"):
            parsed = coach.parse_measurement(hit.text)
            if parsed is not None and parsed.get("track") == args.track:
                previous = parsed
                break
        if previous is not None:
            print("\n记忆复用（来自 PowerContext）：")
            for line in coach.progress_lines(previous, analysis):
                print(f"  - {line}")
        else:
            print("\n记忆复用（来自 PowerContext）：这个车手还没有同赛道的记录，本次作为基线写入。")

        entry = await memory.remember(scope_id, "measurement", coach.measurement_text(analysis, args.driver))
        print(f"\n写入 memory: {entry.citation.entry_id}")
        for text in coach.observation_texts(analysis):
            saved = await memory.remember(scope_id, "observation", text)
            print(f"写入 observation: {saved.citation.entry_id}")
        print(f"scope: {scope_id}")
    return 0


async def _ask(args: argparse.Namespace) -> int:
    query = coach.search_query(args.question)
    async with _memory_from(args) as memory:
        scope_id = await memory.scope(args.driver)
        hits = await memory.search(scope_id, query, limit=args.limit)
        prepared = await memory.context(scope_id, query)
        if args.json:
            print(json.dumps({
                "scope_id": scope_id,
                "hits": [
                    {"text": hit.text, "entry_id": hit.citation.entry_id,
                     "revision": hit.citation.memory_ref.revision}
                    for hit in hits
                ],
                "context": prepared.content,
                "context_status": prepared.status,
            }, ensure_ascii=False, indent=2))
            return 0
        if not hits:
            print(f"记忆里没有和「{args.question}」相关的结论。先跑一次 ingest 把它记下来。")
            return 0
        print(f"检索关键词：{query}")
        print(f"命中 {len(hits)} 条记忆：")
        for hit in hits:
            headline = hit.text.splitlines()[0]
            print(f"  - {headline}")
            print(f"    来源 {hit.citation.entry_id} · revision {hit.citation.memory_ref.revision}")
        print("\n整理后的回答：")
        print(f"  {hits[0].text.splitlines()[0]}")
        print("\nPowerContext 组装的上下文（可直接作为消息交给模型）：")
        print(prepared.content or "（空）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lapcoach", description="赛车遥测复盘助手（PowerContext 记忆）")
    parser.add_argument("--data-dir", default=os.environ.get("LAPCOACH_DATA_DIR", DEFAULT_DATA_DIR),
                        help="记忆与 Server 数据的落盘目录（默认 ./.lapcoach）")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="分析一圈遥测并写入记忆")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--driver", required=True)
    ingest.add_argument("--track", required=True)
    ingest.add_argument("--day", help="默认今天")
    ingest.add_argument("--json", action="store_true")

    ask = sub.add_parser("ask", help="用记忆回答复盘问题")
    ask.add_argument("question")
    ask.add_argument("--driver", required=True)
    ask.add_argument("--limit", type=int, default=5)
    ask.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "ingest":
        return asyncio.run(_ingest(args))
    return asyncio.run(_ask(args))
