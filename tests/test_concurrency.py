"""多进程并发场景：多个执行进程操作同一个 SQLite 文件。

覆盖：
- N 个执行者同时认领同一个任务，恰好 1 个成功，其余 claim_conflict。
- 两个进程都读到同一版本后先后写入，后写者得到 stale_version，
  不会后写覆盖先写。
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
from pathlib import Path

import pytest

from app.errors import ClaimConflict, StaleVersion
from app.service import TaskService

log = logging.getLogger("tasks-test")


def _claim_worker(db_path: str, task_id: int, executor: str, queue) -> None:
    """子进程入口：认领任务，把结果（ok / 错误码）放入队列。"""
    svc = TaskService(db_path)
    try:
        result = svc.claim_task(task_id, executor)
        queue.put({"executor": executor, "ok": True, "result": result})
    except ClaimConflict as exc:
        queue.put({"executor": executor, "ok": False, "code": exc.code})
    except Exception as exc:  # noqa: BLE001 - 测试需要看到任何意外错误
        queue.put({"executor": executor, "ok": False, "code": type(exc).__name__})


def _transition_worker(db_path: str, task_id: int, expected_version: int, queue) -> None:
    """子进程入口：用给定版本号把 running 推进到 completed。"""
    svc = TaskService(db_path)
    try:
        result = svc.transition(task_id, "completed", expected_version)
        queue.put({"ok": True, "result": result})
    except StaleVersion as exc:
        queue.put({"ok": False, "code": exc.code,
                   "expected": exc.expected_version, "actual": exc.actual_version})
    except Exception as exc:  # noqa: BLE001
        queue.put({"ok": False, "code": type(exc).__name__})


@pytest.fixture
def mp_ctx():
    ctx = mp.get_context("spawn")
    yield ctx


def test_concurrent_claim_exactly_one_winner(service, mp_ctx, tmp_path: Path):
    db_path = str(service.db_path)
    task = service.create_task("hot-task")
    n_workers = 8
    log.info("输入: %d 个执行进程并发认领 task_id=%s", n_workers, task["id"])

    queue = mp_ctx.Queue()
    procs = [
        mp_ctx.Process(
            target=_claim_worker,
            args=(db_path, task["id"], f"worker-{i}", queue),
        )
        for i in range(n_workers)
    ]
    for p in procs:
        p.start()
    results = [queue.get() for _ in procs]
    for p in procs:
        p.join()

    winners = [r for r in results if r["ok"]]
    losers = [r for r in results if not r["ok"]]
    log.info("判定依据: 成功认领者 %s；失败者 %s",
             [w["executor"] for w in winners],
             [(r["executor"], r["code"]) for r in losers])

    assert len(winners) == 1, f"期望恰好1个认领成功，实际 {len(winners)}"
    assert len(losers) == n_workers - 1
    assert all(r["code"] == "claim_conflict" for r in losers)

    final = service.get_task(task["id"])
    log.info("最终记录: %s", json.dumps(final, ensure_ascii=False))
    assert final["status"] == "running"
    # 判定依据：认领是单条 UPDATE，version 只可能从1加到2，不会被重复+1
    assert final["version"] == 2


def test_concurrent_claim_distinct_tasks_all_succeed(service, mp_ctx):
    db_path = str(service.db_path)
    tasks = [service.create_task(f"t{i}") for i in range(4)]
    queue = mp_ctx.Queue()
    procs = []
    for i, t in enumerate(tasks):
        procs.append(mp_ctx.Process(
            target=_claim_worker,
            args=(db_path, t["id"], f"worker-{i}", queue),
        ))
    for p in procs:
        p.start()
    results = [queue.get() for _ in procs]
    for p in procs:
        p.join()
    log.info("输入: 4 个进程各认领不同任务；判定依据: 全部成功 = %s",
             [r["ok"] for r in results])
    assert all(r["ok"] for r in results)


def test_cross_process_version_conflict(service, mp_ctx):
    db_path = str(service.db_path)
    task = service.create_task("v-hot")
    service.claim_task(task["id"], "claimer")  # running, version=2

    # 两个进程都持有 expected_version=2（读之后别人可能已写入）
    log.info("输入: 2 个进程都以 expected_version=2 推进 task_id=%s 到 completed",
             task["id"])
    queue = mp_ctx.Queue()
    procs = [
        mp_ctx.Process(target=_transition_worker,
                       args=(db_path, task["id"], 2, queue))
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    results = [queue.get() for _ in procs]
    for p in procs:
        p.join()

    winners = [r for r in results if r["ok"]]
    losers = [r for r in results if not r["ok"]]
    log.info("判定依据: 先写者结果=%s；后写者结果=%s", winners, losers)

    assert len(winners) == 1
    assert len(losers) == 1 and losers[0]["code"] == "stale_version"
    assert losers[0]["expected"] == 2 and losers[0]["actual"] == 3

    final = service.get_task(task["id"])
    # 判定依据：后写没有覆盖先写，version 恰好为3，状态 completed
    assert final["status"] == "completed" and final["version"] == 3
    log.info("最终记录: status=%s version=%s（无后写覆盖）",
             final["status"], final["version"])


def _claim_pool_worker(db_path: str, task_ids: list[int], executor: str, queue) -> None:
    """子进程入口：依次尝试认领任务池中的任务，记录自己抢到的 id。"""
    import sqlite3  # noqa: F401

    svc = TaskService(db_path)
    mine = []
    for tid in task_ids:
        try:
            svc.claim_task(tid, executor)
            mine.append(tid)
        except ClaimConflict:
            pass
        except Exception as exc:  # noqa: BLE001
            queue.put({"error": f"{executor}: {type(exc).__name__}"})
            return
    queue.put({"executor": executor, "claimed": mine})


def test_pool_claim_no_double_and_no_missing(service, mp_ctx):
    db_path = str(service.db_path)
    n_tasks, n_workers = 20, 5
    task_ids = [service.create_task(f"pool-{i}")["id"] for i in range(n_tasks)]
    log.info("输入: %d 个进程争抢 %d 个任务（每个进程都尝试全部任务）",
             n_workers, n_tasks)

    queue = mp_ctx.Queue()
    procs = [
        mp_ctx.Process(
            target=_claim_pool_worker,
            args=(db_path, task_ids, f"worker-{i}", queue),
        )
        for i in range(n_workers)
    ]
    for p in procs:
        p.start()
    results = [queue.get() for _ in procs]
    for p in procs:
        p.join()

    assert all("error" not in r for r in results), results
    claimed_flat = [tid for r in results for tid in r["claimed"]]
    log.info("判定依据: 各进程认领数=%s；总认领事件=%d；去重后=%d",
             {r["executor"]: len(r["claimed"]) for r in results},
             len(claimed_flat), len(set(claimed_flat)))

    # 不变量1：没有任何任务被两个进程重复认领
    assert len(claimed_flat) == len(set(claimed_flat))
    # 不变量2：任务池中每个任务都恰好被认领一次，无遗漏
    assert sorted(claimed_flat) == sorted(task_ids)
    # 不变量3：库中状态与版本一致
    for tid in task_ids:
        row = service.get_task(tid)
        assert row["status"] == "running" and row["version"] == 2, row
    log.info("判定: 20 个任务各被认领恰好一次，全部 running/v2，无重复无遗漏")
