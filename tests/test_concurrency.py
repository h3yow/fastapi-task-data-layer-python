"""并发认领场景：多个执行进程同时认领，只能有一个成功。

- 线程级：同进程多线程对同一文件库并发认领一批任务；
- 进程级：multiprocessing 拉起多个真实执行进程抢同一任务，
  断言恰好一个成功，其余被归类为 TaskAlreadyClaimedError。
"""

from __future__ import annotations

import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from task_service.errors import TaskAlreadyClaimedError
from task_service.models import TaskStatus as S
from task_service.repository import TaskRepository
from task_service.service import TaskService

from .conftest import check, logger
from task_service.db import connect


def _claim_worker(db_path: str, task_id: int, owner: str, out: dict) -> None:
    """子进程入口：独立连接认领任务并记录结果分类。"""

    conn = connect(db_path)
    try:
        svc = TaskService(TaskRepository(conn))
        task = svc.claim_task(task_id, owner)
        out[owner] = ("ok", task.owner, task.version)
    except TaskAlreadyClaimedError as exc:
        out[owner] = ("already_claimed", str(exc))
    except Exception as exc:  # noqa: BLE001
        out[owner] = ("error", f"{type(exc).__name__}: {exc}")


def test_concurrent_claim_multiple_processes(
    file_db_service: tuple[TaskService, Path],
) -> None:
    svc, db_path = file_db_service
    task = svc.create_task("race")
    workers = [f"worker-{i}" for i in range(8)]

    ctx = mp.get_context("spawn")
    manager = ctx.Manager()
    out: dict = manager.dict()
    procs = [
        ctx.Process(
            target=_claim_worker,
            args=(str(db_path), task.id, w, out),
        )
        for w in workers
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        check(f"子进程 {p.name} 退出码", 0, p.exitcode)

    results = dict(out)
    logger.info("输入: task_id=%s workers=%s", task.id, workers)
    logger.info("各进程认领判定: %s", results)

    oks = [w for w, r in results.items() if r[0] == "ok"]
    losers = [w for w, r in results.items() if r[0] == "already_claimed"]
    check("成功认领者数量恰好为 1", 1, len(oks))
    check("其余全部被归类为已认领", 7, len(losers))

    winner = oks[0]
    final = svc.get_task(task.id)
    check("落库状态 claimed", S.CLAIMED, final.status)
    check("落库 owner 为唯一赢家", winner, final.owner)
    check("只发生一次版本递增", 2, final.version)


def test_thread_pool_claim_distinct_tasks(
    file_db_service: tuple[TaskService, Path],
) -> None:
    """一批任务被多于任务数的线程抢：每个任务恰好一个赢家。"""

    svc, db_path = file_db_service
    n_tasks, n_workers = 20, 40
    ids = [svc.create_task(f"t{i}").id for i in range(n_tasks)]

    def claim(idx: int) -> tuple[int, str]:
        task_id = ids[idx % n_tasks]
        conn = connect(str(db_path))
        try:
            local_svc = TaskService(TaskRepository(conn))
            local_svc.claim_task(task_id, f"t-{idx}")
            return task_id, "ok"
        except TaskAlreadyClaimedError:
            return task_id, "lost"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(n_workers)))

    winners: dict[int, int] = {}
    losers = 0
    for task_id, outcome in results:
        if outcome == "ok":
            winners[task_id] = winners.get(task_id, 0) + 1
        else:
            losers += 1

    logger.info(
        "输入: tasks=%s workers=%s | 每任务赢家数=%s | 失败者=%s",
        n_tasks, n_workers, winners, losers,
    )
    check("每个任务恰好一个赢家", [1] * n_tasks,
          [winners.get(i, 0) for i in ids])
    check("失败者总数", n_workers - n_tasks, losers)
