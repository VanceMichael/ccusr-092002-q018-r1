"""SQLite 连接与建库。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from scripts.migrate import migrate


def connect(database_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    path = Path(database_path or os.getenv("DATABASE_PATH", "data/app.sqlite3"))
    migrate(path)  # 幂等：只追加未应用的迁移
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
