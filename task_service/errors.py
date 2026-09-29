"""任务服务错误类型定义。

所有业务错误都继承 :class:`TaskServiceError`，调用方既可以统一捕获，
也可以按具体子类区分不同的拒绝原因：

- :class:`TaskNotFoundError`          任务不存在
- :class:`IllegalTransitionError`     跳级 / 相邻状态之外的非法流转
- :class:`TerminalStateError`         已终止任务再次被要求变更
- :class:`TaskAlreadyCompletedError`  重复完成
- :class:`TaskNotClaimedError`        执行者未认领该任务，无权推进
- :class:`TaskAlreadyClaimedError`    并发认领中的败者：任务已被他人认领
- :class:`VersionConflictError`       乐观锁版本冲突
"""

from __future__ import annotations


class TaskServiceError(Exception):
    """任务服务错误基类。"""


class TaskNotFoundError(TaskServiceError):
    """任务不存在。"""


class InvalidStateTransitionError(TaskServiceError):
    """非法状态流转基类（跳级 / 终止态再变更等）。"""


class IllegalTransitionError(InvalidStateTransitionError):
    """目标状态不是当前状态允许的相邻状态（跳级流转）。"""


class TerminalStateError(InvalidStateTransitionError):
    """任务已处于终止态，不允许再次变更。"""


class TaskAlreadyCompletedError(InvalidStateTransitionError):
    """任务已完成，重复完成/重复流转被拒绝。"""


class TaskNotClaimedError(TaskServiceError):
    """当前执行者未认领任务，无权推进状态。"""


class TaskAlreadyClaimedError(TaskServiceError):
    """并发认领失败：任务已被其他执行者认领。"""


class VersionConflictError(TaskServiceError):
    """乐观锁版本冲突：读取后已被他人先写入，当前写入被拒绝。"""

