"""任务领域服务。

职责：
- 认领：把并发认领的败者归类为 :class:`TaskAlreadyClaimedError`。
- 状态推进：先做状态机校验（给出精确拒绝原因），再用带版本号的
  条件 UPDATE 落库；条件不命中时重读一行，区分版本冲突与状态冲突。
- 批量推进：整批包在一个 ``BEGIN IMMEDIATE`` 事务里，任一条失败
  整体回滚，绝不留半更新状态。
"""

from __future__ import annotations

from task_service.db import transaction
from task_service.errors import (
    IllegalTransitionError,
    InvalidStateTransitionError,
    TaskAlreadyClaimedError,
    TaskAlreadyCompletedError,
    TaskNotFoundError,
    TaskNotClaimedError,
    TerminalStateError,
    VersionConflictError,
)
from task_service.models import (
    Page,
    Task,
    TaskStatus,
    TERMINAL_STATUSES,
    ensure_can_transition,
)
from task_service.repository import TaskRepository, decode_cursor

#: 从这些状态继续推进时，调用方必须是任务的认领者。
_OWNERSHIP_REQUIRED: frozenset[TaskStatus] = frozenset(
    {TaskStatus.CLAIMED, TaskStatus.RUNNING}
)


class TaskService:
    def __init__(self, repo: TaskRepository) -> None:
        self._repo = repo

    def create_task(self, title: str) -> Task:
        title = title.strip()
        if not title:
            raise ValueError("title must not be empty")
        return self._repo.create(title)

    def get_task(self, task_id: int) -> Task:
        task = self._repo.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} not found")
        return task

    def claim_task(self, task_id: int, owner: str) -> Task:
        """认领一个 pending 任务。

        两个执行者并发调用时，底层条件 UPDATE 保证只有一个返回 True；
        失败者根据重读结果区分为：已被他人认领 / 版本过期 / 任务不存在。
        """

        if not owner:
            raise ValueError("owner must not be empty")
        task = self.get_task(task_id)
        if self._repo.atomic_claim(task_id, owner, task.version):
            return self.get_task(task_id)

        # 条件更新未命中：重新读取最新行以归类原因。
        latest = self._repo.get(task_id)
        if latest is None:
            raise TaskNotFoundError(f"task {task_id} not found")
        if latest.version == task.version and latest.status is TaskStatus.PENDING:
            # 状态与版本都没变却没更新成功：理论上不应发生（属版本竞争）。
            raise VersionConflictError(
                f"claim conflict on task {task_id} at version {task.version}"
            )
        if latest.status is not TaskStatus.PENDING:
            # 已被另一个执行者抢先认领（或进一步处理/终止）。
            raise TaskAlreadyClaimedError(
                f"task {task_id} already claimed by {latest.owner!r}, "
                f"status={latest.status.value}"
            )
        # 仍为 pending 但版本已变：快照过期。
        raise VersionConflictError(
            f"stale version {task.version} on task {task_id}; "
            f"current version is {latest.version}"
        )

    def transition(
        self,
        task_id: int,
        target: TaskStatus,
        expected_version: int,
        owner: str | None = None,
    ) -> Task:
        """按乐观锁推进任务状态。

        校验顺序：存在性 -> 版本号（先写优先）-> 认领归属 -> 状态机。
        版本号先于状态机判断：只要调用方基于的快照已过期，就明确归类为
        版本冲突，哪怕最新状态同时也已终止。
        """

        task = self.get_task(task_id)
        if task.version != expected_version:
            raise VersionConflictError(
                f"version conflict on task {task_id}: "
                f"expected {expected_version}, current {task.version}"
            )
        self._check_ownership(task, owner)
        ensure_can_transition(task.status, target)
        return self._apply_transition(task, target, expected_version)

    def _apply_transition(
        self, task: Task, target: TaskStatus, expected_version: int
    ) -> Task:
        """执行条件更新并在失败时重读、精确归类。"""

        if self._repo.atomic_transition(
            task.id, expected_version, task.status, target
        ):
            return self.get_task(task.id)

        latest = self._repo.get(task.id)
        if latest is None:
            raise TaskNotFoundError(f"task {task.id} not found")
        if latest.version != expected_version:
            # 他人先写入导致版本不符：显式报错，不允许后写覆盖先写。
            raise VersionConflictError(
                f"version conflict on task {task.id}: "
                f"expected {expected_version}, current {latest.version}"
            )
        # 版本相同但状态不符：按最新状态重新走一遍状态机以给出精确原因。
        if latest.status is TaskStatus.COMPLETED and target is TaskStatus.COMPLETED:
            raise TaskAlreadyCompletedError(
                f"task {task.id} already completed"
            )
        if latest.status in TERMINAL_STATUSES:
            raise TerminalStateError(
                f"task {task.id} is terminal: {latest.status.value}"
            )
        raise IllegalTransitionError(
            f"illegal transition on task {task.id}: "
            f"{latest.status.value} -> {target.value}"
        )

    @staticmethod
    def _check_ownership(task: Task, owner: str | None) -> None:
        if task.status in _OWNERSHIP_REQUIRED:
            if not owner or task.owner != owner:
                raise TaskNotClaimedError(
                    f"task {task.id} is owned by {task.owner!r}, "
                    f"not by {owner!r}"
                )

    def list_tasks(
        self,
        limit: int,
        cursor: str | None = None,
        status: TaskStatus | None = None,
    ) -> Page:
        cursor_id = decode_cursor(cursor)
        return self._repo.list_page(limit, cursor_id, status)

    def bulk_transition(
        self,
        changes: list[tuple[int, TaskStatus, int]],
        owner: str | None = None,
    ) -> None:
        """单事务批量状态推进。

        ``changes`` 为 ``(task_id, target_status, expected_version)``。
        所有校验在同一个写事务内完成：任何一条不存在、无权流转、
        非法流转或版本冲突，整批回滚，已写入的行也会撤销。
        """

        if not changes:
            return
        repo = self._repo
        conn = repo.conn
        with transaction(conn):
            for task_id, target, expected_version in changes:
                task = repo.get_conn(conn, task_id)
                if task is None:
                    raise TaskNotFoundError(f"task {task_id} not found")
                if task.version != expected_version:
                    raise VersionConflictError(
                        f"version conflict on task {task_id}: "
                        f"expected {expected_version}, "
                        f"current {task.version}"
                    )
                self._check_ownership(task, owner)
                ensure_can_transition(task.status, target)
                if not repo.transition_conn(
                    conn, task_id, expected_version, task.status, target
                ):
                    latest = repo.get_conn(conn, task_id)
                    if latest is not None and latest.version != expected_version:
                        raise VersionConflictError(
                            f"version conflict on task {task_id}: "
                            f"expected {expected_version}, "
                            f"current {latest.version}"
                        )
                    raise InvalidStateTransitionError(
                        f"transition failed on task {task_id}: "
                        f"{task.status.value} -> {target.value}"
                    )

