"""任务数据访问层。

并发安全要点：
- claim / update_status 都由单条带 WHERE 条件的 UPDATE 完成“判定+写入”，
  不先 SELECT 再 UPDATE，避免检查与写入之间的竞态。
- 跨进程时 BEGIN IMMEDIATE 拿到的写锁把这些 UPDATE 串行化，
  cursor.rowcount == 1 即获胜，== 0 即冲突。
- list_page 是 (created_at, id) keyset 分页，走复合索引区间扫描，
  不需要 OFFSET，不会全表扫描。
"""

from __future__ import annotations

import base64
import json
import sqlite3

from app.db import allocate_created_at
from app.models import Page, Task

_TASK_COLUMNS = "id, name, status, version, created_at, updated_at"


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def encode_cursor(created_at: int, task_id: int) -> str:
    """把 (created_at, id) 编成不透明游标字符串。"""
    raw = json.dumps([created_at, task_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[int, int]:
    """解析游标；格式非法时抛 ValueError。"""
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        created_at, task_id = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc
    if not isinstance(created_at, int) or not isinstance(task_id, int):
        raise ValueError(f"invalid cursor: {cursor!r}")
    return created_at, task_id


class TaskRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ---- 写入 ------------------------------------------------------------

    def insert(self, name: str, now: int) -> Task:
        """新建 pending 任务（须在写事务中调用；created_at 取单调序列）。"""
        created_at = allocate_created_at(self._conn)
        cur = self._conn.execute(
            "INSERT INTO tasks (name, status, version, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?)",
            (name, "pending", created_at, now),
        )
        task_id = int(cur.lastrowid)
        return self.get(task_id)  # type: ignore[return-value]

    def claim(self, task_id: int, now: int) -> bool:
        """原子认领：仅当任务仍为 pending 时把它置为 running。

        返回 True 表示当前调用者认领成功；False 表示已被别人抢先。
        """
        cur = self._conn.execute(
            "UPDATE tasks SET status = 'running', version = version + 1, "
            "updated_at = ? WHERE id = ? AND status = 'pending'",
            (now, task_id),
        )
        return cur.rowcount == 1

    def update_status(
        self,
        task_id: int,
        expected_status: str,
        expected_version: int,
        new_status: str,
        now: int,
    ) -> bool:
        """原子状态推进：同时校验当前状态与版本。

        WHERE 同时要求 status=expected_status AND version=expected_version，
        别人先写入后 version 已变大，本语句影响 0 行 -> 返回 False。
        """
        cur = self._conn.execute(
            "UPDATE tasks SET status = ?, version = version + 1, updated_at = ? "
            "WHERE id = ? AND status = ? AND version = ?",
            (new_status, now, task_id, expected_status, expected_version),
        )
        return cur.rowcount == 1

    def insert_event(
        self,
        task_id: int,
        from_status: str | None,
        to_status: str,
        note: str | None,
        now: int,
    ) -> None:
        """追加一条状态变更事件（与状态更新同事务提交/回滚）。"""
        self._conn.execute(
            "INSERT INTO task_events (task_id, from_status, to_status, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, from_status, to_status, note, now),
        )

    # ---- 读取 ------------------------------------------------------------

    def get(self, task_id: int) -> Task | None:
        row = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return _row_to_task(row) if row is not None else None

    def list_page(self, limit: int, cursor: tuple[int, int] | None) -> Page:
        """keyset 分页：取 limit+1 行判断是否还有下一页。

        排序键 (created_at, id) 全局唯一且单调，持续写入的新行只会排在
        当前页之后，向后翻页不会重复也不会漏行。
        """
        sql = (
            f"SELECT {_TASK_COLUMNS} FROM tasks "
            "WHERE (created_at, id) > (?, ?) "
            "ORDER BY created_at ASC, id ASC LIMIT ?"
        )
        if cursor is None:
            # 第一页：从比所有键都小的位置开始。
            params: tuple = (-1, -1, limit + 1)
        else:
            params = (cursor[0], cursor[1], limit + 1)

        rows = self._conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [_row_to_task(row) for row in rows]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id) if has_more else None
        )
        return Page(items=items, next_cursor=next_cursor)
