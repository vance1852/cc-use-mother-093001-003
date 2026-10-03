"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


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
-- 盲审与利益回避后台
CREATE TABLE IF NOT EXISTS review_disciplines (
    discipline_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviewers (
    reviewer_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    organization_id TEXT,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    disciplines_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    track_id TEXT NOT NULL,
    required_disciplines_json TEXT NOT NULL,
    required_reviewer_count INTEGER NOT NULL CHECK(required_reviewer_count >= 1),
    material_ref TEXT NOT NULL,
    anonymized_code TEXT NOT NULL UNIQUE,
    sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_team_members (
    member_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    person_key TEXT NOT NULL,
    role_label TEXT NOT NULL,
    organization_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(work_id, person_key)
);
CREATE TABLE IF NOT EXISTS conflict_declarations (
    declaration_id TEXT PRIMARY KEY,
    reviewer_id TEXT NOT NULL REFERENCES reviewers(reviewer_id),
    subject_type TEXT NOT NULL CHECK(subject_type IN ('person', 'organization')),
    subject_key TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    declared_at TEXT NOT NULL,
    UNIQUE(reviewer_id, subject_type, subject_key, relation_type)
);
CREATE TABLE IF NOT EXISTS rule_versions (
    rule_version TEXT PRIMARY KEY,
    body_json TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_batches (
    batch_id TEXT PRIMARY KEY,
    rule_version TEXT NOT NULL REFERENCES rule_versions(rule_version),
    status TEXT NOT NULL CHECK(status IN ('draft', 'published', 'closed')),
    relationship_snapshot_json TEXT NOT NULL,
    relationship_snapshot_hash TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS review_tasks (
    task_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES review_batches(batch_id),
    work_id TEXT NOT NULL REFERENCES works(work_id),
    reviewer_id TEXT NOT NULL REFERENCES reviewers(reviewer_id),
    discipline_id TEXT,
    slot_index INTEGER NOT NULL CHECK(slot_index >= 0),
    status TEXT NOT NULL CHECK(status IN ('assigned', 'accepted', 'submitted', 'recused',
                                         'absent', 'voided', 'reassigned', 'sealed')),
    conflict_reason TEXT NOT NULL DEFAULT '',
    locked_for_review INTEGER NOT NULL DEFAULT 0 CHECK(locked_for_review IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, work_id, reviewer_id)
);
CREATE TABLE IF NOT EXISTS review_scores (
    score_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES review_tasks(task_id),
    work_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    score_value REAL NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    voided_at TEXT,
    void_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS review_substitutes (
    substitute_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES review_batches(batch_id),
    work_id TEXT NOT NULL REFERENCES works(work_id),
    reviewer_id TEXT NOT NULL REFERENCES reviewers(reviewer_id),
    rank_order INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'promoted', 'skipped', 'sealed')),
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, work_id, reviewer_id),
    UNIQUE(batch_id, work_id, rank_order)
);
CREATE INDEX IF NOT EXISTS idx_review_tasks_reviewer ON review_tasks(reviewer_id, status);
CREATE INDEX IF NOT EXISTS idx_review_tasks_work ON review_tasks(batch_id, work_id, status);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        # 写操作在进程内串行化：配合 BEGIN IMMEDIATE，防止并发领取/提交产生重复任务或重复评分。
        self.write_lock = threading.RLock()

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。写入默认立即获取行级写锁。"""

        with self.write_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
