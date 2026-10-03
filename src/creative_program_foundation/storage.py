"""封装 SQLite 连接、建表和事务边界。

文件数据库使用线程局部连接：每个工作线程持有自己的连接（SQLite 连接不允许
跨线程使用），写事务通过 ``BEGIN IMMEDIATE`` 在数据库级串行化，配合条件
UPDATE 即可防止并发重复领取一类的竞争。``:memory:`` 仅供单线程测试使用。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar, Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
"""

# 领域模块在导入时注册自己的表结构，每个新连接都会完整建立。
_EXTRA_SCHEMAS: list[str] = []


def register_schema(sql: str) -> None:
    """登记一段幂等建表脚本，供之后的每个连接执行。"""

    _EXTRA_SCHEMAS.append(sql)


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    _extra_schemas: ClassVar[list[str]] = []

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        # 立即建立首个连接，让建表错误在启动时暴露，并保持原有即时建表行为。
        self.connection

    @property
    def connection(self) -> sqlite3.Connection:
        """返回当前线程专属的连接，必要时新建并完成建表。"""

        conn = getattr(self._local, "connection", None)
        if conn is None:
            # check_same_thread=False 仅为让 close() 能统一回收；连接对象
            # 始终保存在线程局部存储中，实际不会跨线程使用。
            conn = sqlite3.connect(self.path, isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.executescript(SCHEMA + "".join(_EXTRA_SCHEMAS))
            self._local.connection = conn
            self._local.applied_schemas = len(_EXTRA_SCHEMAS)
            with self._connections_lock:
                self._connections.append(conn)
        else:
            applied = getattr(self._local, "applied_schemas", 0)
            if applied < len(_EXTRA_SCHEMAS):
                conn.executescript("".join(_EXTRA_SCHEMAS[applied:]))
                self._local.applied_schemas = len(_EXTRA_SCHEMAS)
        return conn

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        connection = self.connection
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    def close(self) -> None:
        """关闭全部线程连接。"""

        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()
        self._local = threading.local()
