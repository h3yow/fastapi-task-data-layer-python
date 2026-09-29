"""任务状态枚举与合法流转表。

固定状态机：

    pending ──claim──▶ running ──complete──▶ completed   (终态)
       │                  │
       │                  ├────fail──────▶ failed        (终态)
       │                  │
       └────cancel────────┴──cancel──────▶ cancelled     (终态)

规则：
- 只允许沿上面的边推进；pending → completed 这类跳级一律拒绝。
- completed / failed / cancelled 为终态，终态上的任何变更都拒绝。
"""

from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 每个状态允许前往的下一状态集合。终态对应空集合。
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

#: 终态集合：进入后不可再变更。
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


def is_terminal(status: str) -> bool:
    """判断状态字符串是否为终态。"""
    try:
        return TaskStatus(status) in TERMINAL_STATUSES
    except ValueError:
        return False


def can_transition(current: str, target: str) -> bool:
    """判断 current -> target 是否为合法的相邻流转。"""
    try:
        current_status = TaskStatus(current)
        target_status = TaskStatus(target)
    except ValueError:
        return False
    return target_status in ALLOWED_TRANSITIONS.get(current_status, frozenset())
