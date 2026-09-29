"""游标分页边界场景：不重、不漏、稳定翻页、写入期间翻页一致。"""

from __future__ import annotations

from task_service.models import TaskStatus as S
from task_service.repository import decode_cursor
from task_service.service import TaskService

from .conftest import check, check_raises


def _seed(service: TaskService, n: int) -> list[int]:
    return [service.create_task(f"t{i}").id for i in range(n)]


def _collect(service: TaskService, limit: int, status: S | None = None):
    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        page = service.list_tasks(limit=limit, cursor=cursor, status=status)
        seen.extend(t.id for t in page.items)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        check("游标严格基于末行 id 解码", page.items[-1].id,
              decode_cursor(page.next_cursor))
    return seen, pages


def test_pagination_no_duplicates_no_gaps(service: TaskService) -> None:
    ids = _seed(service, 25)
    seen, pages = _collect(service, limit=10)
    logger_input = f"总数=25 limit=10"
    check(f"({logger_input}) 页数", 3, pages)
    check("无重复", len(seen), len(set(seen)))
    check("无遗漏且有序", ids, seen)
    check("末页只有 5 条", 5, len(seen) - 20)


def test_pagination_exact_page_boundaries(service: TaskService) -> None:
    ids = _seed(service, 20)
    # 整除边界：两页各 10 条，最后 next_cursor 为 None
    seen, pages = _collect(service, limit=10)
    check("整除边界页数", 2, pages)
    check("整除边界数据完整", ids, seen)

    # limit=1 逐条翻页
    seen1, pages1 = _collect(service, limit=1)
    check("limit=1 页数等于总数", 20, pages1)
    check("limit=1 数据完整", ids, seen1)


def test_pagination_stable_under_concurrent_writes(
    service: TaskService,
) -> None:
    """翻页期间持续写入新任务：已翻页区域 id 更小，新数据不回插，
    因此不会出现重复；旧快照范围内的数据也不会漏。"""

    ids = _seed(service, 15)
    first = service.list_tasks(limit=5)
    first_ids = [t.id for t in first.items]
    check("首页 5 条", ids[:5], first_ids)

    # 模拟其它执行进程在翻页期间新增数据
    new_ids = _seed(service, 10)
    check("新任务 id 都大于旧数据", True, all(n > ids[-1] for n in new_ids))

    # 用旧游标继续翻：旧 15 条仍可完整收集，且不含重复
    seen = list(first_ids)
    cursor = first.next_cursor
    while True:
        page = service.list_tasks(limit=5, cursor=cursor)
        seen.extend(t.id for t in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    check("翻页期间写入不重复", len(seen), len(set(seen)))
    check("原始 15 条全部被覆盖", ids, [i for i in seen if i <= ids[-1]])


def test_pagination_filtered_by_status(service: TaskService) -> None:
    ids = _seed(service, 6)
    # 认领前 2 个，完成第 1 个
    t = service.claim_task(ids[0], "w")
    service.transition(t.id, S.RUNNING, t.version, owner="w")
    t = service.get_task(ids[0])
    service.transition(t.id, S.COMPLETED, t.version, owner="w")
    service.claim_task(ids[1], "w")

    completed, _ = _collect(service, 10, status=S.COMPLETED)
    check("只返回 completed", [ids[0]], completed)

    claimed, _ = _collect(service, 10, status=S.CLAIMED)
    check("只返回 claimed", [ids[1]], claimed)

    pending, _ = _collect(service, 10, status=S.PENDING)
    check("只返回 pending", ids[2:], pending)


def test_invalid_cursor_rejected(service: TaskService) -> None:
    check_raises(
        "非法游标",
        ValueError,
        service.list_tasks, 10, "not-a-cursor", None,
    )
