"""PowerContext 记忆层。

每名车手对应一个 Scope；每次复盘的结论以 memory entry 写入。
下一场比赛再跑时，先检索同一 Scope 的历史条目，把上次的结论带进对比，
这就是「越用越懂你」的部分。

默认在本机回环地址拉起一个 PowerContext Server，数据落在 data_dir（SQLite）。
设置 LAPCOACH_POWERCONTEXT_URL 可以连接已经在跑的外部 Server。
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
    PrepareContextRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
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

DEFAULT_REASON = "LapCoach 复盘中确认的结论"


class EmbeddedServer:
    """把 PowerContext Server 跑在调用进程里，省掉评审单独起服务。"""

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
            raise RuntimeError("PowerContext Server 没有起来，检查 data_dir 权限与端口占用")

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


class Memory:
    """DriverMemory 的持有者：负责 Server 生命周期与 Scope 绑定。"""

    def __init__(self, data_dir: Path, url: str | None = None) -> None:
        self.data_dir = data_dir
        self.url = url
        self.client: PowerContextClient | None = None
        self._server: EmbeddedServer | None = None

    async def __aenter__(self) -> Memory:
        if self.url:
            self.client = PowerContextClient(self.url, timeout=120)
        else:
            self._server = EmbeddedServer(self.data_dir / "powercontext")
            self._server.start()
            self.client = PowerContextClient(self._server.base_url, timeout=120)
        await self.client.get_capabilities()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        if self._server is not None:
            self._server.stop()
            self._server = None

    def _scope_book(self) -> dict[str, str]:
        path = self.data_dir / "scopes.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_scope(self, driver: str, scope_id: str) -> None:
        book = self._scope_book()
        book[driver] = scope_id
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "scopes.json").write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")

    async def scope(self, driver: str) -> str:
        assert self.client is not None
        known = self._scope_book().get(driver)
        if known:
            return known
        created = await self.client.create_scope(
            CreateScopeRequest(
                title=f"LapCoach · {driver}",
                summary=f"{driver} 的赛道复盘记忆：每次比赛的结论、薄弱弯与改进进度",
                idempotency_key=f"lapcoach:driver:{driver}",
            )
        )
        self._save_scope(driver, created.scope_id)
        return created.scope_id

    async def remember(self, scope_id: str, kind: str, text: str, reason: str = DEFAULT_REASON) -> object:
        assert self.client is not None
        saved = await self.client.remember_memory(
            RememberMemoryRequest(scope_id=scope_id, kind=kind, text=text, reason=reason)
        )
        return saved.entry

    async def search(self, scope_id: str, query: str, limit: int = 5) -> tuple:
        assert self.client is not None
        result = await self.client.search_memory(
            SearchMemoryRequest(scope_id=scope_id, query=query, limit=limit, mode="fts")
        )
        return tuple(result.hits)

    async def context(self, scope_id: str, query: str, max_bytes: int = 2000):
        assert self.client is not None
        return await self.client.prepare_context(
            PrepareContextRequest(scope_id=scope_id, query=query, max_bytes=max_bytes)
        )
