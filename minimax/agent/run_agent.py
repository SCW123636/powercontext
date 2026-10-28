"""开发助手：用 SDD 上下文 + MiniMax 完成一次真实代码修改。

流程：
1. 从 sddctx 取上下文（规格 / 代码 / 测试 / PowerContext 状态）；
2. 基线跑测试，记录失败项；
3. 把上下文交给 MiniMax（默认 MiniMax-M3），要求返回改动的完整文件；
4. 应用改动，跑目标测试与回归测试；未通过则把失败输出回灌，最多 N 轮；
5. 保存请求/响应、快照、差异、测试日志，并把任务状态写回 PowerContext。

只需要环境变量 MINIMAX_API_KEY。想离线复现时用 --replay 指向已保存的会话。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from sddctx import config
from sddctx.context import build
from sddctx.state import TaskState

MINIMAX_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com") + "/v1/text/chatcompletion_v2"
MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M3")
TARGET_TESTS = ["tests/test_priority_filter.py", "tests/test_csv_priority.py"]
REGRESSION_TESTS = ["tests/test_regression.py"]

SYSTEM_PROMPT = """你是 FleetDesk 项目的资深 Python 工程师，按规格驱动开发（SDD）流程工作。
你会收到：任务描述、适用规格、相关代码文件全文、验证测试全文。
你的输出必须是单个 JSON 对象，不要输出 Markdown 代码围栏或任何解释文字：
{"summary": "一句话说明改了什么", "files": [{"path": "相对路径", "content": "文件完整内容"}]}

规则：
- 只修改实现代码；禁止修改测试、规格和数据文件。
- 只能返回需要改动的文件，content 是改动后的完整文件内容。
- 必须同时满足规格与测试，并保持现有 public API 的兼容性。
- 路径使用相对 workspace 的 POSIX 风格，例如 fleetdesk/service.py。"""


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


RUN_LOG = None


def log(message: str) -> None:
    print(message, flush=True)
    if RUN_LOG is not None:
        with RUN_LOG.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")


async def gather_context(task: str) -> dict:
    async with TaskState() as state:
        saved = await state.load()
        prepared = await state.prepare(task)
    return build(task, state=saved, prepared=prepared)


def render_prompt(context: dict) -> str:
    parts = [f"# 任务\n{context['task']}\n", "# 适用规格\n"]
    for item in context["specs"]:
        parts.append(f"## {item['anchor']}（来源 {item['citation']}，修订 r{item['revision']}）\n{item['snippet']}\n")
    parts.append("\n# 代码文件\n")
    for file in context["files"]:
        if file["path"].startswith("tests/"):
            continue
        parts.append(f"## {file['path']}\n```python\n{file['content']}\n```\n")
    parts.append("\n# 验证测试\n")
    for file in context["files"]:
        if not file["path"].startswith("tests/"):
            continue
        parts.append(f"## {file['path']}\n```python\n{file['content']}\n```\n")
    parts.append(
        "\n# 输出\n按任务与规格修改实现代码，返回约定的 JSON。"
        "需要改的文件：服务层与 CSV 导入层；不要动测试。"
    )
    return "\n".join(parts)


def call_minimax(messages: list[dict], api_key: str, max_tokens: int = 16000) -> dict:
    payload = {"model": MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
    if os.environ.get("MINIMAX_THINKING", "disabled") != "auto":
        payload["thinking"] = {"type": "disabled"}
    started = time.monotonic()
    with httpx.Client(timeout=300) as client:
        response = client.post(
            MINIMAX_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    elapsed = time.monotonic() - started
    response.raise_for_status()
    body = response.json()
    if body.get("base_resp", {}).get("status_code") not in (0, None):
        raise RuntimeError(f"MiniMax 返回错误：{body['base_resp']}")
    choice = (body.get("choices") or [{}])[0]
    content = choice.get("message", {}).get("content", "")
    return {
        "finish_reason": choice.get("finish_reason", ""),
        "request": payload,
        "response": body,
        "content": content,
        "usage": body.get("usage", {}),
        "latency_s": round(elapsed, 3),
        "request_id": body.get("id", ""),
    }


def parse_files(content: str) -> tuple[dict, list[dict]]:
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("模型输出里没有 JSON 对象")
    fragment = text[start : end + 1]
    try:
        data = json.loads(fragment)
    except json.JSONDecodeError:
        data = json.loads(fragment, strict=False)
    files = data.get("files") or []
    if not files:
        raise ValueError("模型没有返回任何文件改动")
    return data, files


def apply_files(files: list[dict], evidence_dir: Path) -> list[str]:
    workspace = config.workspace_dir()
    changed = []
    for item in files:
        rel = str(item["path"]).replace("\\", "/").lstrip("/")
        if not rel.startswith("fleetdesk/") or not rel.endswith(".py"):
            raise ValueError(f"拒绝写入非实现代码路径：{rel}")
        target = (workspace / rel).resolve()
        if workspace.resolve() not in target.parents:
            raise ValueError(f"路径越界：{rel}")
        if target.is_file():
            backup = evidence_dir / "snapshots" / "before" / rel
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")
        changed.append(rel)
    return changed


def run_pytest(paths: list[str], log_path: Path, label: str) -> dict:
    command = [sys.executable, "-m", "pytest", *paths, "-q"]
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=config.workspace_dir(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.monotonic() - started
    output = completed.stdout + completed.stderr
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"$ {' '.join(command)}\nexit={completed.returncode}\n\n{output}",
        encoding="utf-8",
    )
    return {
        "command": command,
        "exit_code": completed.returncode,
        "output": output,
        "duration_s": round(elapsed, 3),
        "label": label,
    }


def write_diff(files: list[str], evidence_dir: Path, before_root: Path) -> str:
    import difflib

    workspace = config.workspace_dir()
    chunks = []
    for rel in files:
        after = (workspace / rel).read_text(encoding="utf-8")
        before_file = before_root / rel
        before = before_file.read_text(encoding="utf-8") if before_file.is_file() else ""
        diff = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        chunks.append("".join(diff))
    text = "".join(chunks)
    (evidence_dir / "patch.diff").write_text(text, encoding="utf-8")
    return text


def feedback_message(result: dict, context: dict) -> str:
    output = result["output"]
    if len(output) > 4000:
        output = output[:2000] + "\n...[截断]...\n" + output[-2000:]
    specs = "\n\n".join(
        f"{item['anchor']}（{item['citation']}）：\n{item['snippet']}" for item in context["specs"]
    )
    return (
        "上一版改动没有通过测试。请对照规格逐条核对，修正后输出完整的改动文件 JSON"
        "（仍然是 {\"summary\", \"files\"} 结构，files 里给改动后的完整内容）：\n\n"
        f"```\n{output}\n```\n\n# 适用规格\n{specs}"
    )


async def record_state(context: dict, changed: list[str], target: dict, regression: dict, rounds: int) -> dict:
    done = [
        f"从 seekdb 检索上下文：规格 {len(context['specs'])} 条、代码 {len(context['code'])} 个符号、测试 {len(context['tests'])} 个",
        f"MiniMax 完成代码修改（{rounds} 轮）：{', '.join(changed)}",
    ]
    verification = [
        f"目标测试 exit={target['exit_code']}，{target['output'].strip().splitlines()[-1] if target['output'].strip() else ''}",
        f"回归测试 exit={regression['exit_code']}，{regression['output'].strip().splitlines()[-1] if regression['output'].strip() else ''}",
    ]
    todo = [] if target["exit_code"] == 0 and regression["exit_code"] == 0 else ["修复仍未通过的测试"]
    async with TaskState() as state:
        saved = await state.record(
            [("done", item) for item in done]
            + [("todo", item) for item in todo]
            + [("verify", item) for item in verification]
        )
        snapshot = await state.load()
    return {"saved": saved, "state": snapshot}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SDD 开发助手")
    parser.add_argument("--task", required=True)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--replay", help="回放已保存的会话 JSON（不调用 MiniMax）")
    parser.add_argument("--evidence", default=str(config.PACKAGE_ROOT / "evidence"))
    args = parser.parse_args(argv)

    evidence_dir = Path(args.evidence)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    globals()["RUN_LOG"] = evidence_dir / "run.log"
    RUN_LOG.write_text("", encoding="utf-8")

    log("[1/6] 组装开发上下文")
    context = asyncio.run(gather_context(args.task))
    (evidence_dir / "seekdb-context.json").write_text(
        json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"      规格 {len(context['specs'])} / 代码 {len(context['code'])} / 测试 {len(context['tests'])}；文件 {[f['path'] for f in context['files']]}")

    log("[2/6] 基线测试（修改前）")
    before = run_pytest(TARGET_TESTS, evidence_dir / "tests" / "before-target.log", "target-before")
    before_reg = run_pytest(REGRESSION_TESTS, evidence_dir / "tests" / "before-regression.log", "regression-before")
    log(f"      目标测试 exit={before['exit_code']}，回归测试 exit={before_reg['exit_code']}")

    session_path = evidence_dir / "minimax" / f"session-{stamp}.json"

    def save_session() -> None:
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    session = {
        "started_at": stamp,
        "model": MODEL,
        "endpoint": MINIMAX_URL,
        "task": args.task,
        "context_summary": {
            "specs": [item["citation"] for item in context["specs"]],
            "code": [item["citation"] for item in context["code"]],
            "tests": [item["citation"] for item in context["tests"]],
        },
        "baseline": {"target": before, "regression": before_reg},
        "rounds": [],
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_prompt(context)},
    ]

    changed: list[str] = []
    target = before
    regression = before_reg
    rounds_used = 0

    if args.replay:
        log(f"[3/6] 回放会话 {args.replay}")
        replayed = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        last = replayed["rounds"][-1]
        files = last["parsed"]["files"]
        session["rounds"].append({"round": 0, "replayed_from": str(args.replay)})
        rounds_used = len(replayed.get("rounds", []))
        changed = apply_files(files, evidence_dir)
        target = run_pytest(TARGET_TESTS, evidence_dir / "tests" / "replay-target.log", "target")
        regression = run_pytest(REGRESSION_TESTS, evidence_dir / "tests" / "replay-regression.log", "regression")
        log(f"      测试：目标 exit={target['exit_code']}，回归 exit={regression['exit_code']}")
    else:
        api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
        if not api_key:
            log("缺少 MINIMAX_API_KEY，无法调用 MiniMax")
            return 2
        files = []
        for round_no in range(1, args.rounds + 1):
            log(f"[3/6] MiniMax 第 {round_no} 轮")
            call = call_minimax(messages, api_key)
            rounds_used = round_no
            messages.append({"role": "assistant", "content": call["content"]})
            try:
                parsed, files = parse_files(call["content"])
            except (ValueError, json.JSONDecodeError) as exc:
                session["rounds"].append({"round": round_no, **call, "error": str(exc)})
                save_session()
                log(f"      解析失败：{exc}（finish={call['finish_reason']}, {len(call['content'])} chars）")
                return 3
            session["rounds"].append({"round": round_no, **call, "parsed": parsed})
            save_session()
            log(f"      request_id={call['request_id']} usage={call['usage'].get('total_tokens')} tokens，{call['latency_s']}s，finish={call['finish_reason']}")
            log(f"      改动文件：{parsed.get('summary', '')}")

            changed = apply_files(files, evidence_dir)
            target = run_pytest(TARGET_TESTS, evidence_dir / "tests" / f"round{round_no}-target.log", "target")
            regression = run_pytest(REGRESSION_TESTS, evidence_dir / "tests" / f"round{round_no}-regression.log", "regression")
            log(f"      测试：目标 exit={target['exit_code']}，回归 exit={regression['exit_code']}")
            if target["exit_code"] == 0 and regression["exit_code"] == 0:
                break
            if round_no < args.rounds:
                messages.append({"role": "user", "content": feedback_message(target, context)})

    log("[4/6] 生成差异与快照")
    diff = write_diff(changed, evidence_dir, evidence_dir / "snapshots" / "before")
    after_root = evidence_dir / "snapshots" / "after"
    for rel in changed:
        target_file = config.workspace_dir() / rel
        backup = after_root / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target_file, backup)

    log("[5/6] 写回 PowerContext 任务状态")
    state_result = asyncio.run(record_state(context, changed, target, regression, rounds_used))
    (evidence_dir / "powercontext-state.json").write_text(
        json.dumps(state_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    session.update(
        {
            "finished_at": now_stamp(),
            "changed_files": changed,
            "target": target,
            "regression": regression,
            "state": state_result["state"],
            "diff": diff,
        }
    )
    save_session()
    log(f"[6/6] 会话记录：{session_path}")

    ok = target["exit_code"] == 0 and regression["exit_code"] == 0
    log(f"结果：目标测试 exit={target['exit_code']}，回归测试 exit={regression['exit_code']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
