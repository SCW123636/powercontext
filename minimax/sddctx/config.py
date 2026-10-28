"""路径与运行参数。"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def workspace_dir() -> Path:
    return Path(os.environ.get("SDDCTX_WORKSPACE", PACKAGE_ROOT / "workspace")).resolve()


def home_dir() -> Path:
    return Path(os.environ.get("SDDCTX_HOME", PACKAGE_ROOT / ".sddctx")).resolve()


def seekdb_dir() -> Path:
    return home_dir() / "seekdb"


def powercontext_dir() -> Path:
    return home_dir() / "powercontext"


DB_NAME = "sdd"
TABLE = "documents"
SCOPE_TITLE = "SDD 上下文 · FleetDesk 优先级过滤"
