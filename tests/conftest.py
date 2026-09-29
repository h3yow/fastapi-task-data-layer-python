"""pytest 公共夹具与判定日志。

单测日志中会打印每条用例的输入内容与判定依据：
- ``check`` 辅助函数记录"期望/实际/结论"；
- 通过 logging 输出到 stderr，可用 ``pytest -s`` 或
  ``--log-cli-level=INFO`` 查看完整输入与判定过程。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import logging
from pathlib import Path
from typing import Any

import pytest

from task_service.db import connect
from task_service.repository import TaskRepository
from task_service.service import TaskService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("task-tests")


@pytest.fixture
def service() -> Iterator[TaskService]:
    """内存库 + 已建表的服务（快速、单进程场景）。"""

    conn = connect(":memory:")
    repo = TaskRepository(conn)
    repo.init_schema()
    yield TaskService(repo)
    conn.close()


@pytest.fixture
def file_db_service(tmp_path: Path) -> Iterator[tuple[TaskService, Path]]:
    """文件库（WAL）服务，供多进程并发场景共享。"""

    path = tmp_path / "tasks.db"
    conn = connect(path)
    repo = TaskRepository(conn)
    repo.init_schema()
    conn.close()
    fresh = connect(path)
    try:
        yield TaskService(TaskRepository(fresh)), path
    finally:
        fresh.close()


def check(case: str, expected: object, actual: object) -> None:
    """记录输入、期望值、实际值与判定结论。"""

    ok = expected == actual
    logger.info(
        "用例=%s | 输入/期望=%r | 实际=%r | 判定=%s",
        case,
        expected,
        actual,
        "PASS" if ok else "FAIL",
    )
    assert ok, f"{case}: expected {expected!r}, got {actual!r}"


def check_raises(
    case: str,
    expected_exc: type[BaseException],
    fn: Callable[..., Any],
    *args: object,
    **kwargs: object,
) -> None:
    """执行调用，断言抛出指定错误类型并记录判定依据。"""

    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 需要比对任意异常类型
        matched = isinstance(exc, expected_exc)
        logger.info(
            "用例=%s | 输入=args=%r kwargs=%r | 抛出=%s: %s | 期望=%s | 判定=%s",
            case,
            args,
            kwargs,
            type(exc).__name__,
            exc,
            expected_exc.__name__,
            "PASS" if matched else "FAIL",
        )
        assert matched, (
            f"{case}: expected {expected_exc.__name__}, "
            f"got {type(exc).__name__}: {exc}"
        )
    else:
        logger.info(
            "用例=%s | 输入=args=%r kwargs=%r | 未抛异常 | 期望=%s | 判定=FAIL",
            case,
            args,
            kwargs,
            expected_exc.__name__,
        )
        pytest.fail(f"{case}: expected {expected_exc.__name__}, nothing raised")
