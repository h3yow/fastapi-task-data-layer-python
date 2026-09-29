"""非法状态流转场景：重复完成、跳级流转、终止态再变更、越权推进。

覆盖要求：不同拒绝原因必须能被明确区分。
"""

from __future__ import annotations

from task_service.errors import (
    IllegalTransitionError,
    TaskAlreadyCompletedError,
    TaskNotClaimedError,
    TerminalStateError,
)
from task_service.models import TaskStatus as S
from task_service.service import TaskService

from .conftest import check, check_raises


def _move_to(service: TaskService, target: S, owner: str = "w1"):
    """把一个新任务推进到指定非终态，返回该状态下的任务。"""

    t = service.create_task(f"task-{target.value}")
    if target is S.PENDING:
        return t
    t = service.claim_task(t.id, owner)
    if target is S.CLAIMED:
        return t
    return service.transition(t.id, S.RUNNING, t.version, owner=owner)


def test_happy_path_flow(service: TaskService) -> None:
    t = service.create_task("flow")
    t = service.claim_task(t.id, "w1")
    check("claim 后状态", S.CLAIMED, t.status)
    t = service.transition(t.id, S.RUNNING, t.version, owner="w1")
    check("running 状态/版本", (S.RUNNING, 3), (t.status, t.version))
    t = service.transition(t.id, S.COMPLETED, t.version, owner="w1")
    check("completed 状态", S.COMPLETED, t.status)


def test_skip_level_transition_rejected(service: TaskService) -> None:
    # pending -> running（跳过 claimed）
    t = _move_to(service, S.PENDING)
    check_raises(
        "pending->running 跳级",
        IllegalTransitionError,
        service.transition, t.id, S.RUNNING, t.version,
    )
    # claimed -> completed（跳过 running）
    t2 = _move_to(service, S.CLAIMED)
    check_raises(
        "claimed->completed 跳级",
        IllegalTransitionError,
        service.transition, t2.id, S.COMPLETED, t2.version, "w1",
    )
    # running -> claimed（倒流）
    t3 = _move_to(service, S.RUNNING)
    check_raises(
        "running->claimed 倒流",
        IllegalTransitionError,
        service.transition, t3.id, S.CLAIMED, t3.version, "w1",
    )


def test_terminal_state_cannot_change(service: TaskService) -> None:
    t = _move_to(service, S.RUNNING)
    t = service.transition(t.id, S.FAILED, t.version, owner="w1")
    check("进入 failed 终态", S.FAILED, t.status)
    check_raises(
        "failed->running 终止态再变更",
        TerminalStateError,
        service.transition, t.id, S.RUNNING, t.version, "w1",
    )

    tc = _move_to(service, S.RUNNING)
    tc = service.transition(tc.id, S.CANCELLED, tc.version, owner="w1")
    check_raises(
        "cancelled->running 终止态再变更",
        TerminalStateError,
        service.transition, tc.id, S.RUNNING, tc.version, "w1",
    )


def test_repeat_completion_rejected(service: TaskService) -> None:
    t = _move_to(service, S.RUNNING)
    t = service.transition(t.id, S.COMPLETED, t.version, owner="w1")
    # 重复完成必须区别于一般终止态错误
    check_raises(
        "重复 completed",
        TaskAlreadyCompletedError,
        service.transition, t.id, S.COMPLETED, t.version, "w1",
    )


def test_only_claimant_may_advance(service: TaskService) -> None:
    t = _move_to(service, S.CLAIMED)
    check_raises(
        "非认领者推进",
        TaskNotClaimedError,
        service.transition, t.id, S.RUNNING, t.version, "intruder",
    )
    check_raises(
        "未带 owner 推进",
        TaskNotClaimedError,
        service.transition, t.id, S.RUNNING, t.version, None,
    )
