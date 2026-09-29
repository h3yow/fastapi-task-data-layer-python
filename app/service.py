"""任务服务：状态机校验 + 乐观版本控制 + 事务编排。

拒绝原因归类（service 层负责区分，repository 层只负责原子写入）：
1. 任务不存在                 -> TaskNotFound(task_not_found)
2. 版本号过期（别人先写入）   -> StaleVersion(stale_version)，优先于状态机判断
3. 目标状态 == 当前状态       -> RepeatedCompletion(repeated_completion)
4. 已在终态还要改成别的状态   -> AlreadyTerminal(already_terminal)
5. 目标状态不是合法后继       -> IllegalTransition(illegal_transition)
6. 并发认领失败               -> ClaimConflict(claim_conflict)
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.config import db_path as default_db_path
from app.db import connect, transaction
from app.enums import TaskStatus, can_transition, is_terminal
from app.errors import (
    AlreadyTerminal,
    ClaimConflict,
    IllegalTransition,
    RepeatedCompletion,
    StaleVersion,
    TaskNotFound,
)
from app.models import Page
from app.repository import TaskRepository, decode_cursor


def _now_ns() -> int:
    return time.time_ns()


class TaskService:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else default_db_path()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _get_or_raise(self, conn: sqlite3.Connection, task_id: int):
        repo = TaskRepository(conn)
        task = repo.get(task_id)
        if task is None:
            raise TaskNotFound(f"task {task_id} does not exist", task_id=task_id)
        return repo, task

    # ---- 命令 ------------------------------------------------------------

    def create_task(self, name: str) -> dict:
        now = _now_ns()
        with transaction(self._db_path) as conn:
            task = TaskRepository(conn).insert(name=name, now=now)
        return task.to_dict()

    def claim_task(self, task_id: int, executor: str = "") -> dict:
        """pending -> running。多执行者并发调用时只有一个成功。

        判定与写入是同一条 UPDATE（原子），失败明确归类为 claim_conflict
        （任务不存在时归类为 task_not_found）。
        """
        now = _now_ns()
        with transaction(self._db_path) as conn:
            repo, task = self._get_or_raise(conn, task_id)
            if is_terminal(task.status):
                # 终态任务不能被认领
                raise AlreadyTerminal(
                    f"task is already terminal ({task.status})",
                    task_id=task_id,
                )
            if not repo.claim(task_id, now):
                # 行存在但 status 已不是 pending —— 被另一个执行者抢先。
                current = repo.get(task_id)
                raise ClaimConflict(
                    f"task already claimed by another executor "
                    f"(current status: {current.status if current else 'unknown'})",
                    task_id=task_id,
                )
            repo.insert_event(
                task_id, "pending", "running", f"claimed by {executor}" if executor else "claimed", now
            )
            updated = repo.get(task_id)
        return updated.to_dict()

    def transition(
        self,
        task_id: int,
        new_status: str,
        expected_version: int,
        note: str | None = None,
    ) -> dict:
        """带乐观版本判断的状态推进。

        校验顺序（决定失败归类）：
        存在性 -> 版本前提（expected_version 过期一律先报 stale_version）
              -> 同态重复 -> 终态变更 -> 跳级/非法流转 -> 原子写入。
        """
        target = new_status
        if target not in {s.value for s in TaskStatus}:
            raise IllegalTransition(
                f"unknown target status: {target!r}",
                task_id=task_id,
                target_status=target,
            )

        now = _now_ns()
        with transaction(self._db_path) as conn:
            repo, task = self._get_or_raise(conn, task_id)
            current = task.status

            # 版本号是调用方显式携带的前提：别人先写入后版本已变，
            # 无论目标是什么都明确归类为版本冲突，绝不后写覆盖先写。
            if task.version != expected_version:
                raise StaleVersion(
                    f"version conflict: expected {expected_version}, "
                    f"actual {task.version}",
                    task_id=task_id,
                    expected_version=expected_version,
                    actual_version=task.version,
                )

            # 目标状态与当前状态相同：重复完成/重复推进（版本是对的，
            # 但状态机不允许原地踏步）。
            if target == current:
                raise RepeatedCompletion(
                    f"task is already {current!r}; repeating the same status is rejected",
                    task_id=task_id,
                    current_status=current,
                    target_status=target,
                )

            # 目标不同，但当前已是终态：从终态再次变更。
            if is_terminal(current):
                raise AlreadyTerminal(
                    f"task already in terminal status {current!r}, "
                    f"cannot move to {target!r}",
                    task_id=task_id,
                    current_status=current,
                    target_status=target,
                )

            if not can_transition(current, target):
                raise IllegalTransition(
                    f"transition {current!r} -> {target!r} is not an allowed step",
                    task_id=task_id,
                    current_status=current,
                    target_status=target,
                )

            ok = repo.update_status(
                task_id,
                expected_status=current,
                expected_version=expected_version,
                new_status=target,
                now=now,
            )
            if not ok:
                # 读取与写入之间被其他进程抢先改了（version/status 已变）。
                fresh = repo.get(task_id)
                raise StaleVersion(
                    "version conflict detected at write time: "
                    f"row changed concurrently (actual version: "
                    f"{fresh.version if fresh else 'unknown'})",
                    task_id=task_id,
                    expected_version=expected_version,
                    actual_version=fresh.version if fresh else None,
                )

            repo.insert_event(task_id, current, target, note, now)
            updated = repo.get(task_id)
        return updated.to_dict()

    def bulk_cancel(self, tasks: dict[int, int]) -> int:
        """一次操作批量取消任务（pending/running -> cancelled）。

        参数为 {task_id: expected_version}。任一任务不满足条件，整个事务
        回滚，已执行的取消也不会留下，返回取消数量。
        """
        now = _now_ns()
        cancelled = 0
        with transaction(self._db_path) as conn:
            repo = TaskRepository(conn)
            for task_id, expected_version in tasks.items():
                _, task = self._get_or_raise(conn, task_id)
                current = task.status

                if is_terminal(current):
                    raise AlreadyTerminal(
                        f"task already terminal ({current}), bulk cancel aborted",
                        task_id=task_id,
                    )
                if not can_transition(current, TaskStatus.CANCELLED.value):
                    raise IllegalTransition(
                        f"cannot cancel task in status {current!r}",
                        task_id=task_id,
                        current_status=current,
                        target_status=TaskStatus.CANCELLED.value,
                    )
                if task.version != expected_version:
                    raise StaleVersion(
                        f"version conflict on task {task_id}: "
                        f"expected {expected_version}, actual {task.version}",
                        task_id=task_id,
                        expected_version=expected_version,
                        actual_version=task.version,
                    )
                if not repo.update_status(
                    task_id,
                    expected_status=current,
                    expected_version=expected_version,
                    new_status=TaskStatus.CANCELLED.value,
                    now=now,
                ):
                    raise StaleVersion(
                        f"write-time version conflict on task {task_id}",
                        task_id=task_id,
                    )
                repo.insert_event(task_id, current, TaskStatus.CANCELLED.value, "bulk cancel", now)
                cancelled += 1
        return cancelled

    # ---- 查询 ------------------------------------------------------------

    def get_task(self, task_id: int) -> dict:
        conn = connect(self._db_path)
        try:
            _, task = self._get_or_raise(conn, task_id)
            return task.to_dict()
        finally:
            conn.close()

    def list_tasks(self, limit: int = 20, cursor: str | None = None) -> dict:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        parsed = decode_cursor(cursor) if cursor else None
        conn = connect(self._db_path)
        try:
            page: Page = TaskRepository(conn).list_page(limit=limit, cursor=parsed)
        finally:
            conn.close()
        return {
            "items": [t.to_dict() for t in page.items],
            "next_cursor": page.next_cursor,
        }
