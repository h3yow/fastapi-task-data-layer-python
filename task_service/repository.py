"""任务数据访问层。

所有并发正确性都下沉到单条条件 UPDATE 上：

- 认领：``UPDATE ... WHERE id=? AND status='pending' AND version=?``
  SQLite 对写行加锁，两个执行者并发执行时只有一个能匹配到行。
- 状态推进：WHERE 同时带 version 与 status 双重条件，
  版本不符 -> 0 行（乐观锁冲突），状态已变 -> 0 行（流转非法/重复）。

列表使用键集（keyset）分页：``WHERE id > ? ORDER BY id LIMIT ?``，
走 ``id`` 主键索引，不使用 OFFSET，多进程持续写入时翻页不会重复、
不会漏读，也不会随数据量增长退化为全表扫描。
"""

from __future__ import annotations

import base64
import sqlite3
from datetime import UTC, datetime

from task_service.db import transaction
from task_service.models import Page, Task, TaskStatus

_ROW_FIELDS = "id, title, status, owner, version, created_at, updated_at"


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        status=TaskStatus(row["status"]),
        owner=row["owner"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def encode_cursor(task_id: int) -> str:
    """把不透明游标编码为字符串。"""

    return base64.urlsafe_b64encode(str(task_id).encode()).decode()


def decode_cursor(cursor: str | None) -> int | None:
    """解码游标；非法游标抛 ValueError。"""

    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        return int(raw.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc


class TaskRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def init_schema(self) -> None:
        """建表与索引。

        - ``id`` 自增主键即分页键，单调递增、绝不复用。
        - (status, id) 复合索引支撑按状态过滤的键集分页。
        - version 列支撑乐观锁。
        """

        with transaction(self._conn):
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    title      TEXT    NOT NULL,
                    status     TEXT    NOT NULL,
                    owner      TEXT,
                    version    INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT    NOT NULL,
                    updated_at TEXT    NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_status_id
                ON tasks(status, id)
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def create(self, title: str) -> Task:
        now = self._now()
        with transaction(self._conn):
            cur = self._conn.execute(
                """
                INSERT INTO tasks (title, status, owner, version,
                                   created_at, updated_at)
                VALUES (?, ?, NULL, 1, ?, ?)
                """,
                (title, TaskStatus.PENDING.value, now, now),
            )
            task_id = cur.lastrowid
        task = self.get(task_id)
        assert task is not None
        return task

    def get(self, task_id: int) -> Task | None:
        return self.get_conn(self._conn, task_id)

    @staticmethod
    def get_conn(conn: sqlite3.Connection, task_id: int) -> Task | None:
        row = conn.execute(
            f"SELECT {_ROW_FIELDS} FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        return _row_to_task(row) if row is not None else None

    def atomic_claim(
        self, task_id: int, owner: str, expected_version: int
    ) -> bool:
        """行级原子认领。

        只在任务仍为 pending 且版本未变时把 owner 与 status 一次性写入，
        version 递增。返回是否有行被更新（并发时只可能一个为 True）。
        """

        now = self._now()
        with transaction(self._conn):
            return self.claim_conn(
                self._conn, task_id, owner, expected_version, now
            )

    @classmethod
    def claim_conn(
        cls,
        conn: sqlite3.Connection,
        task_id: int,
        owner: str,
        expected_version: int,
        now: str | None = None,
    ) -> bool:
        """在调用方给定的连接/事务内执行认领条件更新。"""

        ts = now or cls._now()
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?, owner = ?, version = version + 1,
                   updated_at = ?
             WHERE id = ? AND status = ? AND version = ?
            """,
            (
                TaskStatus.CLAIMED.value,
                owner,
                ts,
                task_id,
                TaskStatus.PENDING.value,
                expected_version,
            ),
        )
        return cur.rowcount == 1

    def atomic_transition(
        self,
        task_id: int,
        expected_version: int,
        current: TaskStatus,
        target: TaskStatus,
    ) -> bool:
        """带版本与当前状态双重条件的原子推进。

        返回 False 表示没有行匹配：调用方重新读取后区分是
        版本冲突还是状态已被他人改变。
        """

        now = self._now()
        with transaction(self._conn):
            return self.transition_conn(
                self._conn, task_id, expected_version, current, target, now
            )

    @classmethod
    def transition_conn(
        cls,
        conn: sqlite3.Connection,
        task_id: int,
        expected_version: int,
        current: TaskStatus,
        target: TaskStatus,
        now: str | None = None,
    ) -> bool:
        """在调用方给定的连接/事务内执行状态推进条件更新。"""

        ts = now or cls._now()
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?, version = version + 1, updated_at = ?
             WHERE id = ? AND status = ? AND version = ?
            """,
            (
                target.value,
                ts,
                task_id,
                current.value,
                expected_version,
            ),
        )
        return cur.rowcount == 1

    def list_page(
        self,
        limit: int,
        cursor_id: int | None,
        status: TaskStatus | None = None,
    ) -> Page:
        """键集分页。

        多取 1 行判断是否还有下一页；不使用 OFFSET，查询恒定走索引。
        持续写入的新任务 id 更大，不会插入到已翻过的页之前。
        """

        if limit < 1:
            raise ValueError("limit must be >= 1")

        params: list[object] = []
        where = []
        if cursor_id is not None:
            where.append("id > ?")
            params.append(cursor_id)
        if status is not None:
            where.append("status = ?")
            params.append(status.value)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit + 1)

        rows = self._conn.execute(
            f"SELECT {_ROW_FIELDS} FROM tasks {where_sql} "
            "ORDER BY id ASC LIMIT ?",
            params,
        ).fetchall()

        items = [_row_to_task(r) for r in rows[:limit]]
        next_cursor: str | None = None
        if len(rows) > limit:
            next_cursor = encode_cursor(items[-1].id)
        return Page(items=items, next_cursor=next_cursor)

