"""演示规格热更新：修改规格并重新导入后，上下文输出立刻使用新内容。

流程：
1. 生成更新前上下文快照；
2. 把 SPEC-4.1 的默认优先级 P2 改成 P1，并把该条「修订」从 1 递增到 2；
3. 重新运行 sddctx ingest 导入规格；
4. 生成更新后上下文快照，断言输出里的 SPEC-4.1 已换成新内容且修订号为 2；
5. 恢复原始规格并再次导入，断言上下文回到更新前的内容。

证据写入 evidence/spec-update/。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sddctx import config  # noqa: E402

TASK = (
    "为工单列表增加优先级过滤（list_orders），"
    "并让 CSV 导入（load_csv）对缺失或非法优先级有明确行为"
)
OUT = REPO / "evidence" / "spec-update"


def run(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "sddctx.cli", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout)


def spec_item(context: dict, anchor: str) -> dict:
    for item in context["specs"]:
        if item["anchor"] == anchor:
            return item
    raise SystemExit(f"上下文中没有 {anchor}")


def mutate(spec_text: str) -> str:
    marker = "## SPEC-4.1 CSV 导入"
    start = spec_text.index(marker)
    end = spec_text.index("## SPEC-4.2", start)
    block = spec_text[start:end]
    changed = (
        block.replace("按默认优先级 `P2` 导入", "按默认优先级 `P1` 导入")
        .replace("同样按 `P2` 处理", "同样按 `P1` 处理")
        .replace("修订：1", "修订：2")
    )
    if changed == block:
        raise SystemExit("SPEC-4.1 中没有找到预期的 P2 / 修订号")
    return spec_text[:start] + changed + spec_text[end:]


def _strip_bullet(line: str) -> str:
    return line[2:] if line.startswith("- ") else line


def snippet_diff(before: str, after: str) -> str:
    lines = ["# 规格热更新差异（SPEC-4.1）", "", "```diff"]
    before_lines = [_strip_bullet(line) for line in before.strip().splitlines()]
    after_lines = [_strip_bullet(line) for line in after.strip().splitlines()]
    for line in before_lines:
        if line not in after_lines:
            lines.append(f"- {line}")
    for line in after_lines:
        if line not in before_lines:
            lines.append(f"+ {line}")
    lines += ["```", ""]
    return "\n".join(lines)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    spec_path = config.workspace_dir() / "specs" / "work-orders.md"
    original = spec_path.read_text(encoding="utf-8")

    before = run("context", "--task", TASK, "--json")
    before_spec = spec_item(before, "SPEC-4.1")
    (OUT / "before.json").write_text(
        json.dumps(before_spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    try:
        spec_path.write_text(mutate(original), encoding="utf-8")
        ingest = run("ingest", "--json")
        after = run("context", "--task", TASK, "--json")
        after_spec = spec_item(after, "SPEC-4.1")
        (OUT / "after.json").write_text(
            json.dumps(after_spec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (OUT / "diff.md").write_text(
            snippet_diff(before_spec["snippet"], after_spec["snippet"]), encoding="utf-8"
        )
        (OUT / "ingest-after-update.json").write_text(
            json.dumps(ingest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        assert before_spec["revision"] == 1, "更新前 SPEC-4.1 修订号应为 1"
        assert "默认优先级 `P2`" in before_spec["snippet"]
        assert after_spec["revision"] == 2, "重新导入后 SPEC-4.1 修订号应为 2"
        assert "默认优先级 `P1`" in after_spec["snippet"], "上下文未使用更新后的规格"
        assert "同样按 `P1` 处理" in after_spec["snippet"]
        assert "同样按 `P2` 处理" not in after_spec["snippet"]
        assert after_spec["citation"] == before_spec["citation"], "条目来源应保持稳定"
    finally:
        spec_path.write_text(original, encoding="utf-8")
        restored_ingest = run("ingest", "--json")

    restored = run("context", "--task", TASK, "--json")
    restored_spec = spec_item(restored, "SPEC-4.1")
    (OUT / "restored.json").write_text(
        json.dumps(restored_spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "ingest-restored.json").write_text(
        json.dumps(restored_ingest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert restored_spec["revision"] == 1, "恢复后 SPEC-4.1 修订号应回到 1"
    assert "默认优先级 `P2`" in restored_spec["snippet"]

    print(f"更新前：r{before_spec['revision']} 默认 P2")
    print(f"更新后：r{after_spec['revision']} 默认 P1；重新导入 {ingest['ingested']} 条")
    print(f"恢复后：r{restored_spec['revision']} 默认 P2")
    print(f"证据：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
