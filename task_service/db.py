"""本地 SQLite 连接管理。

完全基于 Python 标准库 sqlite3，不依赖任何外部数据库/服务。
对文件型数据库开启 WAL 与 busy_timeout，使多个执行进程可以并发读写；
``:memory:`` 模式仅供单进程测试使用。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """创建并配置一个 SQLite 连接。

    - WAL：多进程下读写不互相长期阻塞（仅文件库生效）。
    - busy_timeout：锁竞争时等待而不是立即抛 ``database is locked``。
    - foreign_keys：开启外键约束。
    - Row factory：允许按列名访问结果。
    """

    path_str = str(path)
    conn = sqlite3.connect(
        path_str,
        timeout=30.0,
        isolation_level=None,  # 显式事务，自动提交模式由我们自己控制
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    if path_str != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """事务上下文管理器。

    进入时开启一个立即事务（``BEGIN IMMEDIATE`` 在 SQLite 层面串行化
    写事务），正常退出提交，异常退出回滚，保证一次操作里的多次写入
    要么全部生效要么全部回滚。
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

