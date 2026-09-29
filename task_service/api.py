"""FastAPI 路由：把任务服务暴露为本地 HTTP 接口。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from task_service.db import connect
from task_service.errors import (
    IllegalTransitionError,
    InvalidStateTransitionError,
    TaskAlreadyClaimedError,
    TaskAlreadyCompletedError,
    TaskNotFoundError,
    TaskNotClaimedError,
    TaskServiceError,
    TerminalStateError,
    VersionConflictError,
)
from task_service.models import Task, TaskStatus
from task_service.repository import TaskRepository
from task_service.service import TaskService

#: 错误类型 -> HTTP 状态码（不同拒绝原因可被调用方区分）。
_ERROR_STATUS: dict[type[Exception], int] = {
    TaskNotFoundError: 404,
    VersionConflictError: 409,
    TaskAlreadyClaimedError: 409,
    TaskAlreadyCompletedError: 409,
    TerminalStateError: 422,
    IllegalTransitionError: 422,
    InvalidStateTransitionError: 422,
    TaskNotClaimedError: 403,
}


class CreateTaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)


class TransitionIn(BaseModel):
    target: TaskStatus
    expected_version: int = Field(ge=1)


class TaskOut(BaseModel):
    id: int
    title: str
    status: TaskStatus
    owner: str | None
    version: int
    created_at: str
    updated_at: str

    @classmethod
    def from_model(cls, task: Task) -> "TaskOut":
        return cls(
            id=task.id,
            title=task.title,
            status=task.status,
            owner=task.owner,
            version=task.version,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


class PageOut(BaseModel):
    items: list[TaskOut]
    next_cursor: str | None


@contextmanager
def _service_lifecycle(db_path: str | Path) -> Iterator[TaskService]:
    conn = connect(db_path)
    try:
        repo = TaskRepository(conn)
        repo.init_schema()
        yield TaskService(repo)
    finally:
        conn.close()


def create_app(db_path: str | Path = "tasks.db") -> FastAPI:
    """构造应用；默认使用本地文件 tasks.db（多进程共享）。"""

    app = FastAPI(title="task-service", version="0.1.0")

    def get_service() -> Iterator[TaskService]:
        # 每个请求一个连接，WAL 下多进程/多请求可并发。
        with _service_lifecycle(db_path) as svc:
            yield svc

    def raise_http(exc: Exception) -> None:
        for err_type, status in _ERROR_STATUS.items():
            if isinstance(exc, err_type):
                raise HTTPException(
                    status_code=status,
                    detail={"error": type(exc).__name__, "message": str(exc)},
                ) from exc
        raise exc

    @app.post("/tasks", response_model=TaskOut, status_code=201)
    def create_task(
        body: CreateTaskIn,
        svc: TaskService = Depends(get_service),
    ) -> TaskOut:
        try:
            return TaskOut.from_model(svc.create_task(body.title))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/tasks/{task_id}", response_model=TaskOut)
    def get_task(
        task_id: int, svc: TaskService = Depends(get_service)
    ) -> TaskOut:
        try:
            return TaskOut.from_model(svc.get_task(task_id))
        except TaskServiceError as exc:
            raise_http(exc)

    @app.post("/tasks/{task_id}/claim", response_model=TaskOut)
    def claim_task(
        task_id: int,
        svc: TaskService = Depends(get_service),
        x_owner: str = Header(..., alias="X-Owner"),
    ) -> TaskOut:
        try:
            return TaskOut.from_model(svc.claim_task(task_id, x_owner))
        except TaskServiceError as exc:
            raise_http(exc)

    @app.post("/tasks/{task_id}/transition", response_model=TaskOut)
    def transition_task(
        task_id: int,
        body: TransitionIn,
        svc: TaskService = Depends(get_service),
        x_owner: str | None = Header(default=None, alias="X-Owner"),
    ) -> TaskOut:
        try:
            return TaskOut.from_model(
                svc.transition(
                    task_id, body.target, body.expected_version, x_owner
                )
            )
        except TaskServiceError as exc:
            raise_http(exc)

    @app.get("/tasks", response_model=PageOut)
    def list_tasks(
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = None,
        status: TaskStatus | None = None,
        svc: TaskService = Depends(get_service),
    ) -> PageOut:
        try:
            page = svc.list_tasks(limit, cursor, status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return PageOut(
            items=[TaskOut.from_model(t) for t in page.items],
            next_cursor=page.next_cursor,
        )

    return app

