"""非法状态流转场景：重复完成、跳级、终态再变更必须被明确拒绝并可区分原因。"""

from __future__ import annotations

import logging

import pytest

from app.errors import (
    AlreadyTerminal,
    IllegalTransition,
    RepeatedCompletion,
    StaleVersion,
    TaskNotFound,
)

log = logging.getLogger("tasks-test")


def test_happy_path_pending_running_completed(service):
    t = service.create_task("t1")
    log.info("输入: create(name='t1') => %s", t)
    claimed = service.claim_task(t["id"], "worker-1")
    log.info("输入: claim(id=%s, worker-1) => status=%s version=%s",
             t["id"], claimed["status"], claimed["version"])
    done = service.transition(t["id"], "completed", claimed["version"])
    log.info("输入: transition(%s, completed, expected_version=%s) => %s",
             t["id"], claimed["version"], done)
    # 判定依据：状态严格沿 pending->running->completed 推进，每次 version+1
    assert claimed["status"] == "running" and claimed["version"] == 2
    assert done["status"] == "completed" and done["version"] == 3


def test_skip_level_transition_rejected(service):
    t = service.create_task("skip")
    log.info("输入: 跳级 transition(id=%s, pending -> completed, v1)", t["id"])
    # 判定依据：completed 不是 pending 的合法后继，归类 illegal_transition
    with pytest.raises(IllegalTransition) as exc:
        service.transition(t["id"], "completed", 1)
    log.info("判定: 捕获 code=%s current=%s target=%s",
             exc.value.code, exc.value.current_status, exc.value.target_status)
    assert exc.value.code == "illegal_transition"


def test_running_cannot_go_back_to_pending(service):
    t = service.create_task("back")
    service.claim_task(t["id"], "w")
    log.info("输入: 逆向 transition(id=%s, running -> pending, v2)", t["id"])
    with pytest.raises(IllegalTransition) as exc:
        service.transition(t["id"], "pending", 2)
    log.info("判定: code=%s（pending 不在 running 的后继集合中）", exc.value.code)
    assert exc.value.code == "illegal_transition"


def test_terminal_status_cannot_change_again(service):
    t = service.create_task("term")
    service.claim_task(t["id"], "w")
    service.transition(t["id"], "completed", 2)
    log.info("输入: 终态变更 transition(id=%s, completed -> failed, v3)", t["id"])
    # 判定依据：当前已终态且目标与当前不同 -> already_terminal
    with pytest.raises(AlreadyTerminal) as exc:
        service.transition(t["id"], "failed", 3)
    log.info("判定: code=%s", exc.value.code)
    assert exc.value.code == "already_terminal"


def test_repeated_completion_is_distinguished(service):
    t = service.create_task("rep")
    service.claim_task(t["id"], "w")
    service.transition(t["id"], "completed", 2)
    log.info("输入: 重复完成 transition(id=%s, completed -> completed, v3)", t["id"])
    # 判定依据：目标状态与当前相同（即便在终态）-> repeated_completion，
    # 必须与 already_terminal 区分开。
    with pytest.raises(RepeatedCompletion) as exc:
        service.transition(t["id"], "completed", 3)
    log.info("判定: code=%s（目标==当前，归类为重复完成而非终态变更）", exc.value.code)
    assert exc.value.code == "repeated_completion"


def test_cancel_from_pending_and_running(service):
    t1 = service.create_task("c1")
    cancelled = service.transition(t1["id"], "cancelled", 1)
    log.info("输入: pending -> cancelled => %s", cancelled)
    assert cancelled["status"] == "cancelled"

    t2 = service.create_task("c2")
    service.claim_task(t2["id"], "w")
    cancelled2 = service.transition(t2["id"], "cancelled", 2)
    log.info("输入: running -> cancelled => %s", cancelled2)
    assert cancelled2["status"] == "cancelled"

    # 已取消也是终态
    with pytest.raises(RepeatedCompletion):
        service.transition(t2["id"], "cancelled", 3)
    with pytest.raises(AlreadyTerminal):
        service.transition(t2["id"], "running", 3)
    log.info("判定: cancelled 任务的重复取消/复活均被拒绝")


def test_unknown_status_and_missing_task(service):
    log.info("输入: transition(未知目标状态 'done')")
    with pytest.raises(IllegalTransition) as exc:
        t = service.create_task("u")
        service.transition(t["id"], "done", 1)
    assert exc.value.code == "illegal_transition"

    log.info("输入: get_task(id=404404)")
    with pytest.raises(TaskNotFound) as exc2:
        service.get_task(404404)
    assert exc2.value.code == "task_not_found"
    log.info("判定: 未知状态=illegal_transition, 不存在=task_not_found")


def test_stale_version_does_not_mutate(service):
    t = service.create_task("v")
    service.claim_task(t["id"], "w")  # v1 -> v2
    log.info("输入: transition(%s, completed, expected_version=1) 实际版本已为2", t["id"])
    with pytest.raises(StaleVersion) as exc:
        service.transition(t["id"], "completed", 1)
    log.info("判定: code=%s expected=%s actual=%s",
             exc.value.code, exc.value.expected_version, exc.value.actual_version)
    assert exc.value.code == "stale_version"
    # 被拒绝后记录保持 running/v2，没有任何半更新
    fresh = service.get_task(t["id"])
    assert fresh["status"] == "running" and fresh["version"] == 2
    log.info("判定: 拒绝后记录仍为 status=running version=2，无半更新")
