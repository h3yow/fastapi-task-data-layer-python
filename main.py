"""本地任务服务入口。

默认数据文件 tasks.db（SQLite WAL，多执行进程共享，无外部依赖）。
可用环境变量 TASK_DB_PATH 覆盖。
"""

from __future__ import annotations

import os

import uvicorn

from task_service.api import create_app

app = create_app(os.environ.get("TASK_DB_PATH", "tasks.db"))


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
