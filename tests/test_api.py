"""HTTP API 端到端：状态码、错误码字段、游标分页接口。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from main import create_app

log = logging.getLogger("tasks-test")


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "api.db"
    app = create_app(db)  # startup 事件初始化这个临时库
    with TestClient(app) as c:
        yield c


def test_lifecycle_and_rejection_status_codes(client):
    r = client.post("/tasks", json={"name": "api-task"})
    log.info("输入: POST /tasks name=api-task => %s %s", r.status_code, r.json())
    assert r.status_code == 201
    tid = r.json()["id"]

    r = client.post(f"/tasks/{tid}/claim", json={"executor": "w1"})
    assert r.status_code == 200 and r.json()["status"] == "running"
    log.info("判定: 认领成功 200 running/v%s", r.json()["version"])

    # 第二个执行者并发视角下重复认领 -> 409 claim_conflict
    r = client.post(f"/tasks/{tid}/claim", json={"executor": "w2"})
    log.info("输入: 重复认领 => %s body=%s", r.status_code, r.json()["detail"]["error"])
    assert r.status_code == 409 and r.json()["detail"]["error"] == "claim_conflict"

    # 跳级/逆向流转（带正确版本）-> 422 illegal_transition
    r = client.post(f"/tasks/{tid}/transition",
                    json={"new_status": "pending", "expected_version": 2})
    log.info("输入: running->pending 逆向流转 => %s %s",
             r.status_code, r.json()["detail"]["error"])
    assert r.status_code == 422 and r.json()["detail"]["error"] == "illegal_transition"

    # 过期版本 -> 409 stale_version
    r = client.post(f"/tasks/{tid}/transition",
                    json={"new_status": "completed", "expected_version": 1})
    log.info("输入: 过期版本 v1 => %s %s", r.status_code, r.json()["detail"]["error"])
    assert r.status_code == 409 and r.json()["detail"]["error"] == "stale_version"

    # 正确版本完成 -> 200
    r = client.post(f"/tasks/{tid}/transition",
                    json={"new_status": "completed", "expected_version": 2})
    assert r.status_code == 200
    log.info("判定: 正确版本完成 => 200 completed/v%s", r.json()["version"])

    # 重复完成（目标==当前，版本正确）-> 409 repeated_completion
    r = client.post(f"/tasks/{tid}/transition",
                    json={"new_status": "completed", "expected_version": 3})
    log.info("输入: 重复完成 => %s %s", r.status_code, r.json()["detail"]["error"])
    assert r.status_code == 409 and r.json()["detail"]["error"] == "repeated_completion"

    # 终态再改成别的状态 -> 409 already_terminal
    r = client.post(f"/tasks/{tid}/transition",
                    json={"new_status": "failed", "expected_version": 3})
    log.info("输入: 终态->failed => %s %s", r.status_code, r.json()["detail"]["error"])
    assert r.status_code == 409 and r.json()["detail"]["error"] == "already_terminal"

    # 不存在 -> 404
    r = client.get("/tasks/9999")
    log.info("输入: GET /tasks/9999 => %s", r.status_code)
    assert r.status_code == 404


def test_list_endpoint_cursor_pagination(client):
    for i in range(25):
        client.post("/tasks", json={"name": f"p{i}"})
    collected, cursor, pages = [], None, 0
    while True:
        params = {"limit": 10}
        if cursor:
            params["cursor"] = cursor
        r = client.get("/tasks", params=params)
        assert r.status_code == 200
        body = r.json()
        log.info("输入: GET /tasks limit=10 page=%d => %d 条, next=%s",
                 pages + 1, len(body["items"]), bool(body["next_cursor"]))
        collected.extend(x["id"] for x in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    log.info("判定依据: 共%d页 %d条，唯一=%s", pages, len(collected),
             len(collected) == len(set(collected)))
    assert pages == 3 and len(collected) == 25
    assert collected == sorted(set(collected))


def test_bulk_cancel_rollback_endpoint(client):
    a = client.post("/tasks", json={"name": "a"}).json()
    b = client.post("/tasks", json={"name": "b"}).json()
    client.post(f"/tasks/{b['id']}/claim", json={"executor": "w"})
    client.post(f"/tasks/{b['id']}/transition",
                json={"new_status": "completed", "expected_version": 2})

    r = client.post("/tasks/bulk-cancel",
                    json={"tasks": {str(a["id"]): 1, str(b["id"]): 3}})
    log.info("输入: bulk-cancel 含已完成任务 => %s %s",
             r.status_code, r.json()["detail"]["error"])
    assert r.status_code == 409 and r.json()["detail"]["error"] == "already_terminal"

    a_after = client.get(f"/tasks/{a['id']}").json()
    log.info("判定依据: 回滚后任务a status=%s（期望 pending）", a_after["status"])
    assert a_after["status"] == "pending"


def test_global_exception_handler_shape(client):
    from app.errors import StaleVersion

    @client.app.get("/_boom")
    def _boom():
        raise StaleVersion("x", task_id=7)

    r = client.get("/_boom")
    log.info("输入: 触发全局 TaskError 处理器 => %s %s", r.status_code, r.json())
    assert r.status_code == 409
    assert r.json()["error"] == "stale_version" and r.json()["task_id"] == 7
