"""领域模型与状态机定义。

状态固定、单向流转：

    pending ──► claimed ──► running ──► completed
       │           │           │
       └─► cancelled ◄─────────┘ (claimed/running 可取消)
                   running ──► failed

completed / failed / cancelled 为终止态，不允许再次变更。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from task_service.errors import (
    IllegalTransitionError,
    TaskAlreadyCompletedError,
    TerminalStateError,
)


class TaskStatus(str, enum.Enum):
    """任务固定状态。"""

    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: 终止态集合：进入后不允许任何变更。
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

#: 合法的相邻状态流转表；表中不存在的跳转一律视为跳级/非法。
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.CLAIMED, TaskStatus.CANCELLED}
    ),
    TaskStatus.CLAIMED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED}
    ),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
}


def ensure_can_transition(
    current: TaskStatus, target: TaskStatus
) -> None:
    """校验状态流转是否合法，按拒绝原因抛出不同异常。

    - 当前/目标均为 completed         -> TaskAlreadyCompletedError（重复完成）
    - 当前已是终止态（任意目标变更）   -> InvalidStateTransitionError（终止态再变更）
    - 目标不在合法流转表中             -> InvalidStateTransitionError（跳级/非法）
    """

    if current is TaskStatus.COMPLETED and target is TaskStatus.COMPLETED:
        raise TaskAlreadyCompletedError(
            f"task already completed: {current.value} -> {target.value}"
        )
    if current in TERMINAL_STATUSES:
        raise TerminalStateError(
            f"cannot change a terminal task: {current.value} -> {target.value}"
        )
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise IllegalTransitionError(
            f"illegal transition: {current.value} -> {target.value}"
        )


@dataclass(frozen=True, slots=True)
class Task:
    """一条任务记录。"""

    id: int
    title: str
    status: TaskStatus
    owner: str | None
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Page:
    """游标分页结果；``next_cursor`` 为 None 表示已到末页。"""

    items: list[Task]
    next_cursor: str | None

