"""错误类型：所有拒绝场景都带有可区分的错误码。

错误码一览（测试和调用方按 code 归类失败原因）：
- task_not_found      任务不存在
- stale_version       乐观锁版本冲突（别人先写入，expected_version 已过期）
- claim_conflict      并发认领冲突：任务已不在 pending
- already_terminal    任务已处于终态（completed/failed/cancelled），拒绝再次变更
- repeated_completion 重复完成：目标状态与当前状态相同（如对 completed 再次 complete）
- illegal_transition  跳级/非法流转：目标状态不在当前状态允许的下一状态集合内
"""

from __future__ import annotations


class TaskError(Exception):
    """所有任务领域错误的基类。"""

    code: str = "task_error"

    def __init__(self, message: str = "", *, task_id: int | None = None) -> None:
        super().__init__(message)
        self.task_id = task_id

    def __str__(self) -> str:  # pragma: no cover - 仅调试展示
        prefix = f"[{self.code}]"
        if self.task_id is not None:
            prefix += f" task_id={self.task_id}"
        return f"{prefix} {super().__str__()}".rstrip()


class TaskNotFound(TaskError):
    code = "task_not_found"


class StaleVersion(TaskError):
    """expected_version 与库里当前版本不一致。"""

    code = "stale_version"

    def __init__(
        self,
        message: str = "",
        *,
        task_id: int | None = None,
        expected_version: int | None = None,
        actual_version: int | None = None,
    ) -> None:
        super().__init__(message, task_id=task_id)
        self.expected_version = expected_version
        self.actual_version = actual_version


class ClaimConflict(TaskError):
    """并发认领失败：任务已被其他执行者先行认领或已离开 pending。"""

    code = "claim_conflict"


class IllegalTransition(TaskError):
    """状态流转非法（跳级 / 目标未知等）。"""

    code = "illegal_transition"

    def __init__(
        self,
        message: str = "",
        *,
        task_id: int | None = None,
        current_status: str | None = None,
        target_status: str | None = None,
    ) -> None:
        super().__init__(message, task_id=task_id)
        self.current_status = current_status
        self.target_status = target_status


class AlreadyTerminal(IllegalTransition):
    """任务已处于终态，任何变更都被拒绝。"""

    code = "already_terminal"


class RepeatedCompletion(IllegalTransition):
    """重复完成/重复推进：目标状态与当前状态相同。"""

    code = "repeated_completion"
