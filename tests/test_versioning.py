"""乐观锁版本冲突与批量事务回滚场景。"""

from __future__ import annotations

from task_service.errors import VersionConflictError
from task_service.models import TaskStatus as S
from task_service.service import TaskService

from .conftest import check, check_raises


def _to_running(service: TaskService, owner: str = "w1"):
    t = service.create_task("v")
    t = service.claim_task(t.id, owner)
    return service.transition(t.id, S.RUNNING, t.version, owner=owner)


def test_stale_version_rejected_not_overwritten(service: TaskService) -> None:
    t = _to_running(service)
    stale_version = t.version  # 读取后保存的版本

    # 别人先写入（owner w1 先完成；模拟"先写"）
    first = service.transition(t.id, S.COMPLETED, stale_version, owner="w1")
    check("先写成功，版本递增", stale_version + 1, first.version)

    # 后写拿着旧版本，必须显式冲突，而不是覆盖
    check_raises(
        "旧版本完成 -> 版本冲突",
        VersionConflictError,
        service.transition, t.id, S.FAILED, stale_version, "w1",
    )
    final = service.get_task(t.id)
    check("后写未覆盖先写", S.COMPLETED, final.status)
    check("版本仍为先写后的版本", first.version, final.version)


def test_bulk_transition_all_or_nothing(service: TaskService) -> None:
    tasks = [_to_running(service) for _ in range(4)]
    versions_before = [t.version for t in tasks]

    # 第 3 条使用过期版本 -> 整批必须回滚
    changes = [(t.id, S.COMPLETED, t.version) for t in tasks]
    bad_id, _, bad_ver = changes[2]
    changes[2] = (bad_id, S.COMPLETED, bad_ver - 1)

    check_raises(
        "批量中含过期版本",
        VersionConflictError,
        service.bulk_transition, changes, "w1",
    )

    for t, ver in zip(tasks, versions_before, strict=True):
        cur = service.get_task(t.id)
        check(f"任务{t.id}回滚-状态", S.RUNNING, cur.status)
        check(f"任务{t.id}回滚-版本", ver, cur.version)

    # 修正版本后整批成功
    good = [(t.id, S.COMPLETED, t.version) for t in tasks]
    service.bulk_transition(good, owner="w1")
    statuses = [service.get_task(t.id).status for t in tasks]
    check("整批全部完成", [S.COMPLETED] * 4, statuses)
