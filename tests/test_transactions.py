"""事务原子性场景：一次操作里的多次写入要么全部生效要么全部回滚。"""

from __future__ import annotations

import logging

from app.db import connect
from app.errors import AlreadyTerminal, StaleVersion, TaskNotFound

log = logging.getLogger("tasks-test")


def _event_count(service, task_id: int) -> int:
    conn = connect(service.db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_bulk_cancel_all_or_nothing_on_terminal(service):
    t1 = service.create_task("a")
    t2 = service.create_task("b")
    t3 = service.create_task("c")
    # t3 先完成，进入终态
    service.claim_task(t3["id"], "w")
    service.transition(t3["id"], "completed", 2)

    log.info("输入: bulk_cancel({%s:1, %s:1, %s:3})，其中 %s 已 completed",
             t1["id"], t2["id"], t3["id"], t3["id"])
    try:
        service.bulk_cancel({t1["id"]: 1, t2["id"]: 1, t3["id"]: 3})
        raise AssertionError("应当抛出 already_terminal")
    except AlreadyTerminal as exc:
        log.info("判定依据: 批量操作中止，code=%s task_id=%s",
                 exc.code, exc.task_id)

    # 判定依据：t1/t2 虽在同一事务里被先行 UPDATE，回滚后必须仍是 pending/v1，
    # 不允许留下“半更新”的 cancelled 状态；t3 维持批量前的 completed/v3。
    assert service.get_task(t1["id"])["status"] == "pending"
    assert service.get_task(t2["id"])["status"] == "pending"
    t3_fresh = service.get_task(t3["id"])
    assert t3_fresh["status"] == "completed" and t3_fresh["version"] == 3
    assert _event_count(service, t1["id"]) == 0
    assert _event_count(service, t2["id"]) == 0
    log.info("判定: 回滚后 t1/t2 仍 pending/v1 且无事件，t3 维持 completed/v3，无半更新")


def test_bulk_cancel_commit_when_all_valid(service):
    t1 = service.create_task("a")
    service.claim_task(t1["id"], "w")  # running v2
    t2 = service.create_task("b")  # pending v1

    count = service.bulk_cancel({t1["id"]: 2, t2["id"]: 1})
    log.info("输入: bulk_cancel 两个合法任务 => 取消数量=%s", count)
    assert count == 2
    assert service.get_task(t1["id"])["status"] == "cancelled"
    assert service.get_task(t2["id"])["status"] == "cancelled"
    # 判定依据：事件与状态在同一事务提交，数量一致
    assert _event_count(service, t1["id"]) == 2  # claim + cancel
    assert _event_count(service, t2["id"]) == 1  # cancel
    log.info("判定: 全部提交，事件与状态一致（2 条 / 1 条）")


def test_bulk_cancel_rolls_back_on_version_conflict(service):
    t1 = service.create_task("a")
    t2 = service.create_task("b")
    # t1 在批量前被别人推进到 v2
    service.claim_task(t1["id"], "other")
    log.info("输入: bulk_cancel 携带过期版本 {%s:1(实际2), %s:1}", t1["id"], t2["id"])
    try:
        service.bulk_cancel({t1["id"]: 1, t2["id"]: 1})
        raise AssertionError("应当抛出 stale_version")
    except StaleVersion as exc:
        log.info("判定依据: code=%s task_id=%s actual=%s",
                 exc.code, exc.task_id, exc.actual_version)
    assert service.get_task(t1["id"])["status"] == "running"
    assert service.get_task(t2["id"])["status"] == "pending"
    assert _event_count(service, t2["id"]) == 0
    log.info("判定: 版本冲突导致整批回滚，t2 未被取消、无残留事件")


def test_bulk_cancel_missing_task_rolls_back(service):
    t1 = service.create_task("a")
    log.info("输入: bulk_cancel 包含不存在的 id=9999")
    try:
        service.bulk_cancel({t1["id"]: 1, 9999: 1})
        raise AssertionError("应当抛出 task_not_found")
    except TaskNotFound as exc:
        log.info("判定依据: code=%s task_id=%s", exc.code, exc.task_id)
    assert service.get_task(t1["id"])["status"] == "pending"
    log.info("判定: 缺失任务导致整批回滚，已处理的 t1 未变更")


def test_event_written_together_with_transition(service):
    t = service.create_task("ev")
    assert _event_count(service, t["id"]) == 0
    service.claim_task(t["id"], "w1")
    service.transition(t["id"], "failed", 2, note="boom")
    log.info("输入: claim + transition(failed, note='boom')")
    assert _event_count(service, t["id"]) == 2
    conn = connect(service.db_path)
    try:
        rows = conn.execute(
            "SELECT from_status, to_status, note FROM task_events "
            "WHERE task_id = ? ORDER BY id", (t["id"],)
        ).fetchall()
    finally:
        conn.close()
    log.info("判定依据: 事件链=%s",
             [(r[0], r[1], r[2]) for r in rows])
    assert [r[1] for r in rows] == ["running", "failed"]
    assert rows[1][2] == "boom"
