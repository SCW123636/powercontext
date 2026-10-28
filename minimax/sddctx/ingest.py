"""把工程资料切块并写入 seekdb。

- 规格：按二级标题切成条目，条目编号作为 anchor；
- 代码：按顶层函数/类切块，符号名用于定位；
- 测试：按 test_* 函数切块。
切块保留原文，检索结果可直接作为上下文引用。
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .store import Store

REVISION = re.compile(r"修订[:：]\s*(\d+)")
SPEC_ID = re.compile(r"SPEC-\d+\.\d+")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def chunk_specs(path: Path) -> list[dict]:
    rel = path.relative_to(config.workspace_dir()).as_posix()
    text = path.read_text(encoding="utf-8")
    chunks: list[dict] = []
    current: list[str] = []
    anchor = ""

    def flush() -> None:
        if not current:
            return
        body = "\n".join(current).strip()
        if not body:
            return
        spec_id = SPEC_ID.search(body)
        revision = REVISION.search(body)
        key = spec_id.group(0) if spec_id else (anchor or f"section-{len(chunks) + 1}")
        chunks.append(
            {
                "id": f"spec:{rel}#{key}",
                "kind": "spec",
                "path": rel,
                "anchor": key,
                "symbol": "",
                "body": body,
                "revision": int(revision.group(1)) if revision else 1,
                "updated_at": _now(),
            }
        )

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            current = [line]
            anchor = ""
        elif current:
            current.append(line)
    flush()
    return chunks


def _source_of(lines: list[str], node: ast.AST) -> str:
    start = node.lineno - 1
    for decorator in getattr(node, "decorator_list", []):
        start = min(start, decorator.lineno - 1)
    end = node.end_lineno or node.lineno
    return "\n".join(lines[start:end])


def chunk_python(path: Path, kind: str) -> list[dict]:
    rel = path.relative_to(config.workspace_dir()).as_posix()
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [
            {
                "id": f"{kind}:{rel}",
                "kind": kind,
                "path": rel,
                "anchor": "",
                "symbol": path.stem,
                "body": source,
                "revision": 1,
                "updated_at": _now(),
            }
        ]

    chunks = []
    module_doc = ast.get_docstring(tree) or ""
    prefix = f"# 模块说明：{module_doc}\n\n" if module_doc else ""
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        name = node.name
        if kind == "test" and not name.startswith("test_"):
            continue
        body = prefix + _source_of(lines, node)
        chunks.append(
            {
                "id": f"{kind}:{rel}::{name}",
                "kind": kind,
                "path": rel,
                "anchor": "",
                "symbol": name,
                "body": body,
                "revision": 1,
                "updated_at": _now(),
            }
        )
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                qualified = f"{name}.{member.name}"
                chunks.append(
                    {
                        "id": f"{kind}:{rel}::{qualified}",
                        "kind": kind,
                        "path": rel,
                        "anchor": "",
                        "symbol": qualified,
                        "body": prefix + _source_of(lines, member),
                        "revision": 1,
                        "updated_at": _now(),
                    }
                )

    if not chunks:
        chunks.append(
            {
                "id": f"{kind}:{rel}",
                "kind": kind,
                "path": rel,
                "anchor": "",
                "symbol": path.stem,
                "body": source,
                "revision": 1,
                "updated_at": _now(),
            }
        )
    return chunks


def collect(workspace: Path | None = None) -> list[dict]:
    workspace = workspace or config.workspace_dir()
    docs: list[dict] = []
    for path in sorted((workspace / "specs").glob("*.md")):
        docs.extend(chunk_specs(path))
    for path in sorted((workspace / "fleetdesk").glob("*.py")):
        docs.extend(chunk_python(path, "code"))
    for path in sorted((workspace / "tests").glob("test_*.py")):
        docs.extend(chunk_python(path, "test"))
    return docs


def ingest(store: Store, workspace: Path | None = None) -> dict:
    docs = collect(workspace)
    store.upsert(docs)
    present = {doc["id"] for doc in docs}
    stale = sorted(store.ids() - present)
    if stale:
        store.delete(stale)
    return {
        "ingested": len(docs),
        "removed": len(stale),
        "counts": {
            kind: sum(1 for doc in docs if doc["kind"] == kind)
            for kind in ("spec", "code", "test")
        },
        "total_in_db": store.count(),
    }


def main() -> None:
    with Store() as store:
        print(json.dumps(ingest(store), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
