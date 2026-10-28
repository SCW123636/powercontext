"""PowerContext 状态层：任务进展的写入、重启后读取与上下文组装。

默认在本机回环地址拉起 PowerContext Server，数据落在 .sddctx/powercontext（SQLite）。
设置 SDDCTX_POWERCONTEXT_URL 可连接已经在跑的外部实例。
"""

from __future__ import annotations

import atexit
import json
import socket
import threading
import time
from pathlib import Path

import uvicorn

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime.config import ExternalSkillsConfig, InferenceConfig, RuntimeConfig
from powercontext.client import PowerContextClient
from powercontext.http import (
    CreateScopeRequest,
    ListMemoryEntriesRequest,
    PrepareContextRequest,
    RememberMemoryRequest,
)
from powercontext.server.factory import create_server_app
from powercontext.server.settings import (
    AccessControlConfig,
    BearerAuthConfig,
    HttpConfig,
    McpConfig,
    MetricsConfig,
    ServerSettings,
    TracingConfig,
)

from . import config

MARKER = "[SDDCTX]"
DEFAULT_REASON = "FleetDesk 任务状态：由 SDD 上下文模块记录"


class EmbeddedServer:
    """在调用进程内起一个 PowerContext Server，省掉评审单独起服务。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.base_url = ""
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._listener: socket.socket | None = None

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        settings = ServerSettings(
            workspace=self.data_dir,
            database=SQLiteConfig(url=f"sqlite+aiosqlite:///{self.data_dir / 'powercontext.db'}"),
            http=HttpConfig(host="127.0.0.1"),
            auth=BearerAuthConfig(enabled=False),
            access=AccessControlConfig(mode="disabled"),
            inference=InferenceConfig(),
            runtime=RuntimeConfig(schedule_seconds=None, experience_schedule_seconds=None),
            external_skills=ExternalSkillsConfig(),
            mcp=McpConfig(enabled=False),
            metrics=MetricsConfig(enabled=False),
            tracing=TracingConfig(enabled=False),
        )
        app = create_server_app(settings=settings, scheduler_path=self.data_dir / "scheduler.db")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self._listener = listener
        self.base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on"))
        self._server = server
        self._thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        self._thread.start()
        atexit.register(self.stop)
        deadline = time.monotonic() + 45
        while self._thread.is_alive() and not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            self.stop()
            raise RuntimeError("PowerContext Server 没有起来，检查数据目录权限与端口占用")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15)
        if self._listener is not None:
            self._listener.close()
        self._server = None
        self._thread = None
        self._listener = None


class TaskState:
    def __init__(self, data_dir: Path | None = None, url: str | None = None) -> None:
        self.data_dir = Path(data_dir or config.powercontext_dir())
        self.url = url
        self.client: PowerContextClient | None = None
        self.scope_id = ""
        self._server: EmbeddedServer | None = None

    async def __aenter__(self) -> TaskState:
        if self.url:
            self.client = PowerContextClient(self.url, timeout=120)
        else:
            self._server = EmbeddedServer(self.data_dir)
            self._server.start()
            self.client = PowerContextClient(self._server.base_url, timeout=120)
        await self.client.get_capabilities()
        self.scope_id = await self._ensure_scope()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        if self._server is not None:
            self._server.stop()
            self._server = None

    def _scope_book_path(self) -> Path:
        return self.data_dir / "scope.json"

    async def _ensure_scope(self) -> str:
        assert self.client is not None
        book = self._scope_book_path()
        payload = json.loads(book.read_text(encoding="utf-8")) if book.is_file() else {}
        if payload.get("scope_id"):
            return payload["scope_id"]
        created = await self.client.create_scope(
            CreateScopeRequest(
                title=config.SCOPE_TITLE,
                summary="FleetDesk 优先级过滤任务的规格、改动、验证结果与待办",
                idempotency_key="sddctx:fleetdesk:priority-filter",
            )
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        book.write_text(json.dumps({"scope_id": created.scope_id}, ensure_ascii=False), encoding="utf-8")
        return created.scope_id

    async def _remember(self, kind: str, text: str) -> dict:
        assert self.client is not None
        saved = await self.client.remember_memory(
            RememberMemoryRequest(
                scope_id=self.scope_id,
                kind=kind,
                text=f"{MARKER} {text}",
                reason=DEFAULT_REASON,
            )
        )
        return {"entry_id": saved.entry.id, "text": text}

    async def record(self, entries: list[tuple[str, str]]) -> list[dict]:
        saved = []
        for kind, text in entries:
            saved.append(await self._remember(kind, text))
        return saved

    async def load(self) -> dict:
        assert self.client is not None
        response = await self.client.list_memory_entries(
            ListMemoryEntriesRequest(scope_id=self.scope_id)
        )
        state: dict[str, list[str]] = {"done": [], "todo": [], "verification": []}
        entries = []
        for entry in getattr(response, "entries", []):
            text = getattr(entry, "text", "")
            if not text.startswith(MARKER):
                continue
            body = text[len(MARKER) :].strip()
            label, _, value = body.partition(":")
            label = label.strip()
            value = value.strip()
            bucket = {"done": "done", "todo": "todo", "verify": "verification"}.get(label)
            if bucket is None:
                continue
            state[bucket].append(value)
            entries.append({"kind": label, "text": value, "entry_id": getattr(entry, "id", "")})
        return {
            "scope_id": self.scope_id,
            "entries": entries,
            **state,
        }

    async def prepare(self, query: str, max_bytes: int = 3000) -> dict:
        assert self.client is not None
        prepared = await self.client.prepare_context(
            PrepareContextRequest(scope_id=self.scope_id, query=query, max_bytes=max_bytes)
        )
        if hasattr(prepared, "model_dump"):
            return prepared.model_dump(mode="json")
        return {"raw": str(prepared)}
