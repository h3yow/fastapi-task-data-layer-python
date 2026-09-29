"""FastAPI 入口：启动时建库并装配 TaskService。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import _STATUS_BY_CODE, router as task_router
from app.config import db_path as default_db_path
from app.db import init_db
from app.errors import TaskError
from app.service import TaskService


def create_app(db_path: Path | str | None = None) -> FastAPI:
    """构造应用。db_path 为 None 时使用配置中的默认本地数据库。"""
    resolved = Path(db_path) if db_path is not None else default_db_path()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(resolved)
        app.state.db_path = resolved
        app.state.service = TaskService(resolved)
        yield

    app = FastAPI(title="Task Record Service", lifespan=lifespan)

    @app.exception_handler(TaskError)
    async def _task_error_handler(_: Request, exc: TaskError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS_BY_CODE.get(exc.code, 400),
            content={
                "error": exc.code,
                "message": str(exc.args[0]) if exc.args else "",
                "task_id": exc.task_id,
            },
        )

    app.include_router(task_router)

    @app.get("/")
    def read_root() -> dict:
        return {"service": "task-record", "status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
