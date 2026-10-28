"""开发上下文组装：规格先行，再按规格点名的符号扩展代码与测试检索。

流程：
1. 用任务描述在 seekdb 全文索引里检索规格条目（SQL MATCH ... AGAINST）；
2. 从命中的规格里抽取被点名的符号（如 list_orders、load_csv）、规格编号（SPEC-x.y）；
3. 用这些符号 + 任务词检索代码与测试，合并全文分数与词面覆盖；
4. 合并 PowerContext 中保存的任务状态，输出带真实来源的上下文包。
"""

from __future__ import annotations

import re

from . import config
from .store import Store, query_tokens

LIMITS = {"spec": 4, "code": 5, "test": 6}
KIND_LABEL = {"spec": "specs", "code": "code", "test": "tests"}

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
SPEC_ID = re.compile(r"SPEC-\d+\.\d+")
STOPWORDS = {
    "the", "and", "for", "with", "none", "true", "false", "str", "int", "list",
    "dict", "path", "value", "self", "import", "from", "return", "def", "class",
    "csv", "utf", "p0", "p1", "p2", "p3",
}


def identifiers(text: str) -> set[str]:
    found = {match.group(0) for match in IDENTIFIER.finditer(text)}
    found.update(SPEC_ID.findall(text))
    return {token for token in found if token.lower() not in STOPWORDS and len(token) > 2}


def _lexical_score(body: str, tokens: list[str]) -> float:
    lowered = body.lower()
    return float(sum(lowered.count(token) for token in tokens))


def _fulltext_scores(store: Store, query: str, kind: str, limit: int) -> dict[str, float]:
    hits = store.search_fulltext(query, kind, limit)
    return {hit["id"]: hit["score"] for hit in hits}


def _rank(
    store: Store,
    kind: str,
    query: str,
    tokens: list[str],
    boost_terms: set[str],
    limit: int,
) -> list[dict]:
    docs = store.scanned(kind)
    if not docs:
        return []
    fts = _fulltext_scores(store, query, kind, max(limit * 4, 20))
    max_fts = max(fts.values(), default=0.0) or 1.0

    for doc in docs:
        body = doc["body"]
        identifier_hits = 0
        spec_hits = 0
        for term in boost_terms:
            if term in body or term.lower() in body.lower():
                identifier_hits += 1
                if SPEC_ID.fullmatch(term):
                    spec_hits += 1
        doc["score"] = round(
            3.0 * (fts.get(doc["id"], 0.0) / max_fts)
            + 1.2 * _lexical_score(body, tokens)
            + 4.0 * identifier_hits
            + 2.0 * spec_hits,
            6,
        )
        doc["match"] = "fulltext" if fts.get(doc["id"], 0.0) > 0 else "keyword"

    docs.sort(key=lambda doc: (-doc["score"], doc["id"]))
    return [doc for doc in docs[:limit] if doc["score"] > 0] or docs[:limit]


def retrieve_all(store: Store, task: str) -> dict[str, list[dict]]:
    tokens = query_tokens(task)
    specs = _rank(store, "spec", task, tokens, identifiers(task), LIMITS["spec"])
    expansions = identifiers(" ".join(doc["body"] for doc in specs)) | identifiers(task)
    expansion_query = " ".join(sorted(expansions)) or task
    code = _rank(store, "code", expansion_query, tokens + sorted(expansions), expansions, LIMITS["code"])
    tests = _rank(store, "test", expansion_query, tokens + sorted(expansions), expansions, LIMITS["test"])
    return {"spec": specs, "code": code, "test": tests}


def _citation(doc: dict) -> str:
    if doc["kind"] == "spec":
        return f"{doc['path']}#{doc['anchor']}"
    return f"{doc['path']}::{doc['symbol']}"


def _file_contents(paths: list[str], limit: int = 4) -> list[dict]:
    workspace = config.workspace_dir()
    files = []
    for rel in dict.fromkeys(paths):
        if len(files) >= limit:
            break
        path = workspace / rel
        if path.is_file():
            files.append({"path": rel, "content": path.read_text(encoding="utf-8")})
    return files


def build(task: str, state: dict | None = None, prepared: dict | None = None) -> dict:
    with Store() as store:
        retrieved = retrieve_all(store, task)
        total = store.count()
    return compose(task, retrieved, total, state, prepared)


def compose(
    task: str,
    retrieved: dict[str, list[dict]],
    total_in_db: int = 0,
    state: dict | None = None,
    prepared: dict | None = None,
) -> dict:
    items = {
        KIND_LABEL[kind]: [
            {
                "citation": _citation(doc),
                "path": doc["path"],
                "anchor": doc["anchor"],
                "symbol": doc["symbol"],
                "revision": doc["revision"],
                "score": round(doc["score"], 4),
                "match": doc["match"],
                "snippet": doc["body"],
            }
            for doc in docs
        ]
        for kind, docs in retrieved.items()
    }

    file_paths = [item["path"] for item in items["code"] + items["tests"]]
    state = state or {"done": [], "todo": [], "verification": []}
    return {
        "task": task,
        "specs": items["specs"],
        "code": items["code"],
        "tests": items["tests"],
        "state": {
            "done": state.get("done", []),
            "todo": state.get("todo", []),
            "verification": state.get("verification", []),
        },
        "files": _file_contents(file_paths),
        "powercontext": {
            "scope_id": state.get("scope_id", ""),
            "entries": state.get("entries", []),
            "prepared_context": prepared or {},
        },
        "database": {"engine": "seekdb (embedded, FULLTEXT ngram)", "documents": total_in_db},
    }


def _clip(text: str, width: int = 320) -> str:
    text = text.strip()
    return text if len(text) <= width else text[:width].rstrip() + " …"


def render_markdown(context: dict) -> str:
    lines = [f"# 开发上下文：{context['task']}", ""]
    lines.append("## 适用规格")
    for item in context["specs"]:
        lines.append(f"- `{item['anchor'] or item['citation']}` — {item['citation']} (r{item['revision']}, {item['match']})")
        lines.append("")
        lines.append("```")
        lines.append(_clip(item["snippet"]))
        lines.append("```")
    if not context["specs"]:
        lines.append("- （无命中）")

    lines += ["", "## 需要修改的代码"]
    for item in context["code"]:
        lines.append(f"- `{item['citation']}` (r{item['revision']}, {item['match']}, score {item['score']})")
    if not context["code"]:
        lines.append("- （无命中）")

    lines += ["", "## 用于验证的测试"]
    for item in context["tests"]:
        lines.append(f"- `{item['citation']}` ({item['match']}, score {item['score']})")
    if not context["tests"]:
        lines.append("- （无命中）")

    lines += ["", "## 任务状态（PowerContext）"]
    state = context["state"]
    for key, label in (("done", "已完成"), ("todo", "待办"), ("verification", "验证结果")):
        values = state.get(key) or ["（无）"]
        lines.append(f"- {label}：")
        for value in values:
            lines.append(f"  - {value}")

    lines += ["", "## 数据来源"]
    db = context["database"]
    lines.append(f"- seekdb：{db['engine']}，库内 {db['documents']} 个条目")
    lines.append(f"- PowerContext Scope：`{context['powercontext'].get('scope_id', '')}`")
    return "\n".join(lines)
