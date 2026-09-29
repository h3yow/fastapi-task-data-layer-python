# 本地持久化后台任务服务

一个完全跑在本地的后台任务服务：任务记录落 **SQLite**（Python 标准库，
无需任何外部数据库或外部服务），支持固定状态机、多执行进程并发认领、
乐观锁版本判断、单事务批量推进，以及稳定的游标分页。

## 快速开始

```bash
# 安装依赖（含 pytest 开发依赖）
uv sync

# 本地启动（默认数据文件 ./tasks.db，自动建表）
uv run python main.py
# 打开 http://127.0.0.1:8000/docs 可直接调试

# 换一个数据文件路径
TASK_DB_PATH=/tmp/my-tasks.db uv run python main.py
```

## 运行测试

```bash
uv run pytest                      # 全部用例
uv run pytest --log-cli-level=INFO # 打印每条用例的输入内容与判定依据
```

测试覆盖四类场景：

| 文件 | 场景 |
| --- | --- |
| `tests/test_concurrency.py` | 8 个真实子进程同时认领同一任务，恰好 1 个成功；线程池抢任务 |
| `tests/test_versioning.py` | 旧版本写入被拒绝且不覆盖先写；批量推进失败整体回滚 |
| `tests/test_state_machine.py` | 重复完成、跳级流转、终止态再变更、越权推进的精确归类 |
| `tests/test_pagination.py` | 分页不重不漏、整除边界、翻页期间持续写入、按状态过滤 |

设计与验证细节见 [`docs/DESIGN.md`](docs/DESIGN.md)。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/tasks` | 创建任务，body `{"title": "..."}` |
| `GET` | `/tasks/{id}` | 读取任务（含 `version`） |
| `POST` | `/tasks/{id}/claim` | 认领，请求头 `X-Owner: <执行者>` |
| `POST` | `/tasks/{id}/transition` | 推进状态，body `{"target": "running", "expected_version": 3}`，可带 `X-Owner` |
| `GET` | `/tasks?limit=&cursor=&status=` | 游标分页，返回 `items` 与不透明 `next_cursor` |

### 一次完整调用示例

```bash
curl -s -X POST localhost:8000/tasks -H 'Content-Type: application/json' \
  -d '{"title":"build"}'
# -> {"id":1,"status":"pending","version":1,...}

curl -s -X POST localhost:8000/tasks/1/claim -H 'X-Owner: worker-1'
# -> {"id":1,"status":"claimed","owner":"worker-1","version":2,...}

curl -s -X POST localhost:8000/tasks/1/transition \
  -H 'Content-Type: application/json' -H 'X-Owner: worker-1' \
  -d '{"target":"running","expected_version":2}'
# -> {"id":1,"status":"running","version":3,...}
```

## 错误归类

失败响应体为 `{"detail": {"error": "<错误类型>", "message": "..."}}`，
不同拒绝原因使用不同错误类型与 HTTP 状态码：

| 错误类型 | HTTP | 含义 |
| --- | --- | --- |
| `TaskNotFoundError` | 404 | 任务不存在 |
| `TaskAlreadyClaimedError` | 409 | 并发认领失败：已被他人认领 |
| `VersionConflictError` | 409 | 乐观锁版本冲突，后写被拒绝 |
| `TaskAlreadyCompletedError` | 409 | 重复完成 |
| `IllegalTransitionError` | 422 | 跳级 / 非法相邻流转 |
| `TerminalStateError` | 422 | 已终止任务再次变更 |
| `TaskNotClaimedError` | 403 | 非认领者无权推进 |
