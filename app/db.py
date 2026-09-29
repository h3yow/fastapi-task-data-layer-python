"""SQLite 连接管理与建表。

本地多进程并发策略（不依赖任何外部数据库）：
- WAL 日志模式：读写不互斥，多个执行进程可同时读取。
- busy_timeout：写锁冲突时等待而不是立刻报 database is locked。
- 写入统一走 BEGIN IMMEDIATE：在事务开始即拿 RESERVED 写锁，
  跨进程的写操作被 SQLite 在文件锁层面串行化，不会交叉提交。
- isolation_level=None：关闭驱动隐式事务，BEGIN/COMMIT/ROLLBACK 全部显式控制。
- task_seq 表提供进程内/跨进程严格单调递增的 created_at，
  使游标分页在持续写入下也不会漏数据（配合 (created_at, id) 复合索引）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.config import db_path as default_db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    status     TEXT    NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- 游标分页的覆盖索引：按 (created_at, id) 有序，翻页走索引区间扫描。
CREATE INDEX IF NOT EXISTS idx_tasks_created_id ON tasks(created_at, id);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    note        TEXT,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id);

-- 单行序列表：跨事务/跨进程分配严格单调的 created_at。
CREATE TABLE IF NOT EXISTS task_seq (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    next_value INTEGER NOT NULL
);
INSERT OR IGNORE INTO task_seq (id, next_value) VALUES (1, 1);
"""

BUSY_TIMEOUT_MS = 10_000


def _resolve(db_path: Path | str | None) -> Path:
    return Path(db_path) if db_path is not None else default_db_path()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开一个配置好并发参数的 SQLite 连接（autocommit 模式）。"""
    path = _resolve(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """创建表、索引和序列（幂等）。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """显式写事务：BEGIN IMMEDIATE 提前取写锁，正常提交，异常回滚。

    事务体内的多次写入要么全部生效，要么在异常时全部回滚，
    不会留下半更新状态。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def allocate_created_at(conn: sqlite3.Connection) -> int:
    """在当前事务内分配下一个严格单调的 created_at（必须在写事务中调用）。"""
    row = conn.execute(
        "UPDATE task_seq SET next_value = next_value + 1 WHERE id = 1 "
        "RETURNING next_value - 1"
    ).fetchone()
    return int(row[0])
