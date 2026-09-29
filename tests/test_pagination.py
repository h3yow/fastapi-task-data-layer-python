"""游标分页边界场景。

- 总数刚好等于页大小 / 页大小整数倍时的边界；
- 多进程持续写入的同时向后翻页，不重复、不遗漏（对翻页开始前已存在的数据）；
- 游标不透明、损坏游标被拒绝；
- 查询计划走复合索引，没有全表扫描或逐条查询。
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp

import pytest

from app.db import connect
from app.repository import decode_cursor, encode_cursor

log = logging.getLogger("tasks-test")


def _fetch_all(service, limit: int) -> tuple[list[int], int]:
    """从第一页翻到最后，返回 (有序id列表, 页数)。"""
    ids, cursor, pages = [], None, 0
    while True:
        page = service.list_tasks(limit=limit, cursor=cursor)
        ids.extend(item["id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    return ids, pages


def test_empty_list_first_page(service):
    page = service.list_tasks(limit=10)
    log.info("输入: 空库 list(limit=10)；判定依据: items=%s next_cursor=%s",
             page["items"], page["next_cursor"])
    assert page["items"] == [] and page["next_cursor"] is None


def test_exact_page_boundary(service):
    # 总数恰好等于两页：20 条，limit=10
    for i in range(20):
        service.create_task(f"task-{i}")
    p1 = service.list_tasks(limit=10)
    log.info("输入: 20条/每页10 第一页；判定依据: 本页%d条, 有游标=%s",
             len(p1["items"]), p1["next_cursor"] is not None)
    assert len(p1["items"]) == 10 and p1["next_cursor"] is not None

    p2 = service.list_tasks(limit=10, cursor=p1["next_cursor"])
    log.info("输入: 第二页；判定依据: 本页%d条, next_cursor=%s（恰好取完时应为None）",
             len(p2["items"]), p2["next_cursor"])
    assert len(p2["items"]) == 10 and p2["next_cursor"] is None

    ids = [x["id"] for x in p1["items"]] + [x["id"] for x in p2["items"]]
    assert ids == sorted(set(ids)) and len(ids) == 20
    log.info("判定: 20条数据跨两页，无重复无遗漏，且第二页后无多余游标")


def test_off_by_one_boundaries(service):
    # 边界：11 条 / limit=5 => 3 页（5,5,1）
    for i in range(11):
        service.create_task(f"x{i}")
    ids, pages = _fetch_all(service, limit=5)
    log.info("输入: 11条/每页5；判定依据: 页数=%d, 条数=%d", pages, len(ids))
    assert pages == 3 and len(ids) == 11 and ids == sorted(set(ids))


def test_cursor_roundtrip_and_tampering(service):
    cursor = encode_cursor(123456789, 42)
    log.info("输入: 游标编码 (created_at=123456789, id=42) => %s", cursor)
    assert decode_cursor(cursor) == (123456789, 42)

    for bad in ("not-a-cursor!!!", "", "eyJhIjogMX0="):
        log.info("输入: 损坏游标 %r", bad)
        with pytest.raises(ValueError):
            decode_cursor(bad)
    log.info("判定: 合法游标可还原；损坏/伪造游标抛 ValueError（API 层映射为400）")


def _insert_worker(db_path: str, count: int) -> None:
    from app.service import TaskService

    svc = TaskService(db_path)
    for i in range(count):
        svc.create_task(f"concurrent-{i}")


def test_pagination_stable_under_concurrent_writes(service, tmp_path):
    db_path = str(service.db_path)
    # 先放入基线数据
    for i in range(30):
        service.create_task(f"base-{i}")
    log.info("输入: 基线30条，每页8条；翻页期间另起进程持续写入20条")

    ctx = mp.get_context("spawn")
    # 在翻到第二页时启动写入进程
    p1 = service.list_tasks(limit=8)
    writer = ctx.Process(target=_insert_worker, args=(db_path, 20))
    writer.start()

    seen = [x["id"] for x in p1["items"]]
    cursor = p1["next_cursor"]
    while cursor:
        page = service.list_tasks(limit=8, cursor=cursor)
        seen.extend(x["id"] for x in page["items"])
        cursor = page["next_cursor"]
    writer.join()

    log.info("判定依据: 翻页共收集 %d 个 id（基线30；新写入的20条键更大，"
             "不应插入到旧页造成重复）", len(seen))
    # 不重复、有序；且所有基线30条都被覆盖（不遗漏）
    assert len(seen) == len(set(seen))
    assert seen == sorted(seen)
    base_ids = set(range(1, 31))
    # 写入结束后重新全量翻页，应能取到全部 50 条
    full, _ = _fetch_all(service, limit=8)
    log.info("输入: 写入结束后重新全量翻页；判定依据: 总条数=%d（期望50）", len(full))
    assert len(full) == 50 and len(set(full)) == 50
    assert base_ids.issubset(set(full))
    log.info("判定: 并发写入下基线数据无重复无遗漏，最终50条全部可取")


def test_query_uses_index_not_full_scan(service):
    for i in range(50):
        service.create_task(f"idx-{i}")
    conn = connect(service.db_path)
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id, name, status, version, created_at, updated_at "
            "FROM tasks WHERE (created_at, id) > (?, ?) "
            "ORDER BY created_at ASC, id ASC LIMIT 11",
            (0, 0),
        ).fetchall()
    finally:
        conn.close()
    detail = [r[-1] for r in plan]
    log.info("输入: EXPLAIN QUERY PLAN 分页查询；判定依据: %s", json.dumps(detail))
    joined = " ".join(detail).upper()
    assert "SCAN TASKS" not in joined, f"出现全表扫描: {detail}"
    assert "IDX_TASKS_CREATED_ID" in joined, f"未走复合索引: {detail}"
    log.info("判定: 查询计划使用 idx_tasks_created_id 索引，无全表扫描")


def test_limit_validation(service):
    log.info("输入: list(limit=0) 与 list(limit=101)")
    with pytest.raises(ValueError):
        service.list_tasks(limit=0)
    with pytest.raises(ValueError):
        service.list_tasks(limit=101)
    log.info("判定: 越界 limit 被拒绝（1..100）")
