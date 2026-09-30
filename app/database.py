"""SQLite 连接管理与建表初始化。

时间约定：所有时间统一以 UTC、定长字符串 ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` 存储，
因此字典序即时间序，便于与左闭右开有效期做字符串比较。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import get_settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 全局单调版本流水：每次成功变更产生一条记录，version 由 INTEGER PRIMARY KEY 自增
CREATE TABLE IF NOT EXISTS versions (
    version                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type             TEXT NOT NULL CHECK (event_type IN ('cert_issue','cert_correct',
                                           'cert_revoke','reading_submit',
                                           'reading_adjudication')),
    request_id             TEXT NOT NULL UNIQUE,
    request_payload_json   TEXT NOT NULL,
    fingerprint            TEXT NOT NULL,
    response_json          TEXT,
    created_at             TEXT NOT NULL
);

-- 证书主档：每个证书编号一行，保存当前生命周期状态
CREATE TABLE IF NOT EXISTS certificates (
    certificate_no        TEXT PRIMARY KEY,
    current_version_seq   INTEGER NOT NULL DEFAULT 1,
    is_revoked            INTEGER NOT NULL DEFAULT 0,
    revoked_at_version    INTEGER REFERENCES versions(version),
    revoked_reason        TEXT,
    created_at_version    INTEGER NOT NULL REFERENCES versions(version),
    updated_at_version    INTEGER NOT NULL REFERENCES versions(version)
);

-- 证书内容流水（不可变）：签发一条；每次纠正再追加一条
CREATE TABLE IF NOT EXISTS certificate_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_no      TEXT NOT NULL REFERENCES certificates(certificate_no),
    version_seq         INTEGER NOT NULL,
    device_no           TEXT NOT NULL,
    valid_from          TEXT NOT NULL,
    valid_to            TEXT NOT NULL,
    min_value           REAL NOT NULL,
    max_value           REAL NOT NULL,
    created_at_version  INTEGER NOT NULL REFERENCES versions(version),
    superseded_at_version INTEGER,  -- 被新纠正版本失效的版本；撤销亦置位
    UNIQUE (certificate_no, version_seq)
);
CREATE INDEX IF NOT EXISTS idx_certver_device_active
    ON certificate_versions(device_no, valid_from, valid_to);

-- 读数（不可变，只能追加）
CREATE TABLE IF NOT EXISTS readings (
    reading_no          TEXT PRIMARY KEY,
    device_no           TEXT NOT NULL,
    measured_at         TEXT NOT NULL,
    value               REAL NOT NULL,
    submitted_at_version INTEGER NOT NULL REFERENCES versions(version),
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_device_time ON readings(device_no, measured_at);

-- 读数人工选证裁决（只追加）：针对处于 review 的读数，人工指定一个候选证书版本。
-- 裁决是否失效不在此表登记：完全由所选证书版本的 superseded_at_version 派生，
-- 因此历史版本视角下的裁决状态天然准确，纠正/撤销不需要回写本表。
CREATE TABLE IF NOT EXISTS reading_adjudications (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_no            TEXT NOT NULL REFERENCES readings(reading_no),
    certificate_no        TEXT NOT NULL,
    certificate_seq       INTEGER NOT NULL,
    basis_version         INTEGER NOT NULL REFERENCES versions(version),  -- 裁决所依据的当前全局版本
    decision              TEXT NOT NULL CHECK (decision IN ('conforming','out_of_range')),
    reason                TEXT NOT NULL,
    created_at_version    INTEGER NOT NULL REFERENCES versions(version),  -- 裁决生效版本
    UNIQUE (id),
    FOREIGN KEY (certificate_no, certificate_seq)
        REFERENCES certificate_versions(certificate_no, version_seq)
);
CREATE INDEX IF NOT EXISTS idx_adjudications_reading
    ON reading_adjudications(reading_no, created_at_version);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    # 允许父目录在容器卷场景下自动创建
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=get_settings().busy_timeout_ms / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(get_settings().busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.isolation_level = None  # 事务边界完全由代码显式控制
    return conn


def init_db() -> None:
    """创建表结构并初始化数据库（幂等，可重复执行）。"""
    conn = _connect(get_settings().db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _migrate_event_type_check(conn)
    finally:
        conn.close()


def _migrate_event_type_check(conn: sqlite3.Connection) -> None:
    """把旧库 versions.event_type 的 CHECK 扩展为包含 reading_adjudication。

    SQLite 无法直接 ALTER 约束：旧库（仅四个事件类型的 CHECK）需要重建
    versions 表。表本身不大（每个写请求一行），在事务内一次性完成；
    新建库的 CHECK 已含新事件类型，无需重建。
    """
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='versions'"
    ).fetchone()["sql"]
    if "reading_adjudication" in sql:
        return
    # foreign_keys 不能在事务内切换；迁移期间临时关闭（此时连接刚建立、无其他事务）
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 保留子表（certificates/readings 等）中对 "versions" 的外键引用名：
        # 3.26+ 默认会把引用改写为 versions_old，打开 legacy 开关禁止该行为。
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("ALTER TABLE versions RENAME TO versions_old")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute(
            "CREATE TABLE versions ("
            "version INTEGER PRIMARY KEY AUTOINCREMENT,"
            "event_type TEXT NOT NULL CHECK (event_type IN "
            "('cert_issue','cert_correct','cert_revoke','reading_submit',"
            "'reading_adjudication')),"
            "request_id TEXT NOT NULL UNIQUE,"
            "request_payload_json TEXT NOT NULL,"
            "fingerprint TEXT NOT NULL,"
            "response_json TEXT,"
            "created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO versions (version, event_type, request_id,"
            " request_payload_json, fingerprint, response_json, created_at)"
            " SELECT version, event_type, request_id, request_payload_json,"
            " fingerprint, response_json, created_at FROM versions_old"
        )
        conn.execute("DROP TABLE versions_old")
        conn.execute("COMMIT")
        # 立即对整个库做一次外键完整性校验，再恢复连接的外键开关
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key integrity check failed: {violations!r}")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys=ON")
        raise


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """请求级只读连接。"""
    conn = _connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """请求级一致性只读快照事务（WAL deferred）。

    事务内所有 SELECT 共享同一读快照：事务开始后其他连接的提交
    （新读数提交、证书纠正 / 撤销）在本事务内一律不可见，
    保证一次取证包生成过程读到的是同一个数据库状态。
    """
    conn = _connect(get_settings().db_path)
    try:
        conn.execute("BEGIN")  # 延迟事务，首次 SELECT 时确立 WAL 读快照
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """串行化写事务：BEGIN IMMEDIATE 立刻获取 RESERVED 锁，
    避免并发写在升级锁时发生死锁；异常自动回滚。"""
    conn = _connect(get_settings().db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
