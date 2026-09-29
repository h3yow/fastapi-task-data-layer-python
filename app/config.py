"""配置：数据库文件路径。

默认使用项目根目录下 data/tasks.db（SQLite 本地文件，无需外部数据库）。
可用环境变量 TASK_DB_PATH 覆盖，测试中也可直接传入路径。
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_PATH = Path("./data/tasks.db")


def db_path() -> Path:
    """返回当前生效的数据库文件路径（TASK_DB_PATH 环境变量优先）。"""
    raw = os.environ.get("TASK_DB_PATH")
    return Path(raw) if raw else DEFAULT_DB_PATH
