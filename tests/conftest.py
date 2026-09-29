"""pytest 公共夹具与日志。

每个测试使用独立的临时 SQLite 文件，互不干扰。
所有测试通过 logging 打印：输入参数、关键中间状态、断言的判定依据。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.db import init_db
from app.service import TaskService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("tasks-test")


@pytest.fixture
def service(tmp_path: Path) -> TaskService:
    db = tmp_path / "tasks.db"
    log.info("初始化临时数据库: %s", db)
    init_db(db)
    svc = TaskService(db)
    return svc
