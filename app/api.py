"""FastAPI 路由与领域错误 -> HTTP 响应映射。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from app.errors import (
    AlreadyTerminal,
    ClaimConflict,
    IllegalTransition,
    RepeatedCompletion,
    StaleVersion,
    TaskError,
    TaskNotFound,
)
from app.schemas import BulkCancelIn, ClaimIn, CreateTaskIn, TransitionIn
from app.service import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])

#: 错误码 -> HTTP 状态码。409 表示与当前资源状态/版本冲突。
_STATUS_BY_CODE = {
    "task_not_found": 404,
    "stale_version": 409,
    "claim_conflict": 409,
    "already_terminal": 409,
    "repeated_completion": 409,
    "illegal_transition": 422,
}


def _service(request: Request) -> TaskService:
    return request.app.state.service


@router.post("", status_code=201)
def create_task(body: CreateTaskIn, request: Request) -> dict:
    return _service(request).create_task(body.name)


@router.get("/{task_id}")
def get_task(task_id: int, request: Request) -> dict:
    try:
        return _service(request).get_task(task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail={"error": exc.code}) from exc


@router.post("/{task_id}/claim")
def claim_task(task_id: int, body: ClaimIn, request: Request) -> dict:
    try:
        return _service(request).claim_task(task_id, body.executor)
    except (TaskNotFound, ClaimConflict, AlreadyTerminal) as exc:
        raise HTTPException(
            status_code=_STATUS_BY_CODE[exc.code],
            detail={"error": exc.code, "message": str(exc)},
        ) from exc


@router.post("/{task_id}/transition")
def transition_task(task_id: int, body: TransitionIn, request: Request) -> dict:
    try:
        return _service(request).transition(
            task_id, body.new_status, body.expected_version, body.note
        )
    except (
        TaskNotFound,
        StaleVersion,
        IllegalTransition,
        RepeatedCompletion,
        AlreadyTerminal,
    ) as exc:
        raise HTTPException(
            status_code=_STATUS_BY_CODE[exc.code],
            detail={"error": exc.code, "message": str(exc)},
        ) from exc


@router.post("/bulk-cancel")
def bulk_cancel(body: BulkCancelIn, request: Request) -> dict:
    try:
        count = _service(request).bulk_cancel(body.tasks)
    except TaskError as exc:
        raise HTTPException(
            status_code=_STATUS_BY_CODE.get(exc.code, 400),
            detail={"error": exc.code, "message": str(exc)},
        ) from exc
    return {"cancelled": count}


@router.get("")
def list_tasks(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> dict:
    try:
        return _service(request).list_tasks(limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_cursor",
                                                     "message": str(exc)}) from exc
