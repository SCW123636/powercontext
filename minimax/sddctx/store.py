"""seekdb 嵌入式存储：工程资料入库与全文检索。

使用 seekdb 自带的 FULLTEXT（ngram 分词）索引，不依赖外部 embedding 服务，
整个流程离线可复现。数据目录默认落在 .sddctx/seekdb。
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pylibseekdb as seekdb

from . import config

_open_lock = threading.Lock()
_opened: set[str] = set()


def sql_literal(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _open_instance(db_dir: Path) -> None:
    key = str(db_dir.resolve())
    with _open_lock:
        if key in _opened:
            return
        db_dir.mkdir(parents=True, exist_ok=True)
        last_error = None
        for attempt in range(6):
            try:
                seekdb.open(db_dir=key)
                _opened.add(key)
                return
            except Exception as exc:  # noqa: BLE001 - 上一个进程尚未完全释放时重试
                last_error = exc
                if "initialized twice" in str(exc):
                    _opened.add(key)
                    return
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"seekdb 打不开 {key}: {last_error}")


ASCII_TOKEN = re.compile(r"[a-z][a-z0-9_]{1,}")
CJK = re.compile(r"[\u4e00-\u9fff]")


def query_tokens(text: str) -> list[str]:
    """ASCII 词 + 中文二元组，用于补充 FTS 的召回。"""
    lowered = text.lower()
    tokens = set(ASCII_TOKEN.findall(lowered))
    cjk = "".join(CJK.findall(lowered))
    tokens.update(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return sorted(tokens)


class Store:
    def __init__(self, db_dir: Path | None = None, database: str = config.DB_NAME) -> None:
        self.db_dir = Path(db_dir or config.seekdb_dir())
        self.database = database
        self.conn = None

    def __enter__(self) -> Store:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def open(self) -> None:
        _open_instance(self.db_dir)
        self.conn = seekdb.connect(database="test", autocommit=True)
        self._execute(f"CREATE DATABASE IF NOT EXISTS {self.database}")
        self._execute(f"USE {self.database}")
        self._execute(
            f"""
            CREATE TABLE IF NOT EXISTS {config.TABLE} (
              id VARCHAR(256) PRIMARY KEY,
              kind VARCHAR(16) NOT NULL,
              path VARCHAR(256) NOT NULL,
              anchor VARCHAR(128) NOT NULL DEFAULT '',
              symbol VARCHAR(128) NOT NULL DEFAULT '',
              body TEXT NOT NULL,
              revision INT NOT NULL DEFAULT 1,
              updated_at VARCHAR(32) NOT NULL DEFAULT '',
              FULLTEXT INDEX ft_body(body) WITH PARSER ngram
            )
            """
        )

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _execute(self, sql: str) -> list[tuple]:
        assert self.conn is not None
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
            return list(cursor.fetchall())
        finally:
            cursor.close()

    def upsert(self, docs: list[dict]) -> int:
        for doc in docs:
            self._execute(
                f"DELETE FROM {config.TABLE} WHERE id = {sql_literal(doc['id'])}"
            )
            self._execute(
                f"""
                INSERT INTO {config.TABLE}
                  (id, kind, path, anchor, symbol, body, revision, updated_at)
                VALUES (
                  {sql_literal(doc['id'])},
                  {sql_literal(doc['kind'])},
                  {sql_literal(doc['path'])},
                  {sql_literal(doc.get('anchor', ''))},
                  {sql_literal(doc.get('symbol', ''))},
                  {sql_literal(doc['body'])},
                  {int(doc.get('revision', 1))},
                  {sql_literal(doc.get('updated_at', ''))}
                )
                """
            )
        return len(docs)

    def delete(self, ids: list[str]) -> int:
        for doc_id in ids:
            self._execute(f"DELETE FROM {config.TABLE} WHERE id = {sql_literal(doc_id)}")
        return len(ids)

    def ids(self) -> set[str]:
        return {row[0] for row in self._execute(f"SELECT id FROM {config.TABLE}")}

    def count(self, kind: str | None = None) -> int:
        where = f" WHERE kind = {sql_literal(kind)}" if kind else ""
        return self._execute(f"SELECT COUNT(*) FROM {config.TABLE}{where}")[0][0]

    def show_create(self) -> str:
        return self._execute(f"SHOW CREATE TABLE {config.TABLE}")[0][1]

    def search_fulltext(self, query: str, kind: str | None = None, limit: int = 8) -> list[dict]:
        where = [f"MATCH(body) AGAINST({sql_literal(query)})"]
        if kind:
            where.append(f"kind = {sql_literal(kind)}")
        sql = (
            f"SELECT id, kind, path, anchor, symbol, body, revision, "
            f"MATCH(body) AGAINST({sql_literal(query)}) AS score "
            f"FROM {config.TABLE} WHERE {' AND '.join(where)} "
            f"ORDER BY score DESC LIMIT {int(limit)}"
        )
        rows = self._execute(sql)
        return [
            {
                "id": r[0],
                "kind": r[1],
                "path": r[2],
                "anchor": r[3],
                "symbol": r[4],
                "body": r[5],
                "revision": r[6],
                "score": float(r[7] or 0.0),
                "match": "fulltext",
            }
            for r in rows
        ]

    def scanned(self, kind: str | None = None, limit: int = 200) -> list[dict]:
        where = f" WHERE kind = {sql_literal(kind)}" if kind else ""
        rows = self._execute(
            f"SELECT id, kind, path, anchor, symbol, body, revision FROM {config.TABLE}"
            f"{where} ORDER BY id LIMIT {int(limit)}"
        )
        return [
            {
                "id": r[0],
                "kind": r[1],
                "path": r[2],
                "anchor": r[3],
                "symbol": r[4],
                "body": r[5],
                "revision": r[6],
                "score": 0.0,
                "match": "",
            }
            for r in rows
        ]
