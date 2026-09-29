# Task Record Service（任务记录持久化服务）

一个完全运行在本地的后台任务记录服务：SQLite（WAL）持久化 + FastAPI 接口。
**不依赖任何外部数据库或外部服务**，多个执行进程并发操作同一份任务数据时，
状态、版本、分页都不会错乱。

## 1. 数据模型与状态流转

任务表 `tasks`：`id / name / status / version / created_at / updated_at`。
另有 `task_events` 记录每次状态变更（与状态更新在同一事务内写入）。

固定状态机（只允许相邻流转，不允许跳级）：

```
 pending ──claim──▶ running ──complete──▶ completed     终态
    │                  │
    │                  ├──fail────────▶ failed          终态
    │                  │
    └────cancel────────┴──cancel──────▶ cancelled       终态
```

| 当前状态  | 允许的下一状态 |
|-----------|----------------------------------------------|
| pending   | running、cancelled |
| running   | completed、failed、cancelled |
| completed | （无，终态） |
| failed    | （无，终态） |
| cancelled | （无，终态） |

非法操作都会被**明确拒绝**，并通过错误码区分拒绝原因：

| 错误码 | 含义 | HTTP |
|--------|------|------|
| `task_not_found` | 任务不存在 | 404 |
| `stale_version` | 乐观锁版本冲突：携带的 `expected_version` 已过期（别人先写入），或读与写之间被并发改动 | 409 |
| `claim_conflict` | 并发认领失败：任务已离开 pending，被其他执行者抢先 | 409 |
| `repeated_completion` | 重复推进：目标状态与当前状态相同（如对 completed 再次 complete） | 409 |
| `already_terminal` | 已在终态（completed/failed/cancelled）还要变更到**别的**状态 | 409 |
| `illegal_transition` | 跳级/逆向流转，或目标状态未知（如 pending→completed） | 422 |

`transition` 的判定顺序（决定归类）：

```
存在性 → 版本前提（expected_version 过期一律先报 stale_version）
       → 同态重复(repeated_completion)
       → 终态变更(already_terminal)
       → 跳级/非法(illegal_transition)
       → 单条原子 UPDATE 落库
```

> 版本号是调用方显式携带的前提：只要前提过期，无论目标状态为何都先报
> `stale_version`，因此两个执行者读到同一版本后先后写入，后写者**绝不会**
> 覆盖先写者。

## 2. 并发认领

认领由**单条带条件的 UPDATE** 完成“判定 + 写入”，没有先 SELECT 再 UPDATE 的竞态窗口：

```sql
UPDATE tasks SET status='running', version=version+1, updated_at=?
WHERE id=? AND status='pending';
```

- 影响行数 = 1：当前执行者认领成功；
- 影响行数 = 0：已被别人抢先，归类为 `claim_conflict`。

N 个进程同时认领同一个任务时，SQLite 的写锁把这些 UPDATE 串行化，
结果**恰好 1 个成功、N-1 个失败**，且 version 只从 1 增加到 2（不会重复 +1）。

## 3. 乐观版本控制

- 每次成功写入（认领、流转）`version = version + 1`。
- 流转接口必须携带读取时拿到的 `expected_version`。
- 落库语句同时要求 `status=当前状态 AND version=期望版本`：

```sql
UPDATE tasks SET status=?, version=version+1, updated_at=?
WHERE id=? AND status=? AND version=?;
```

即使在读与写之间被其他进程改动，该语句也会影响 0 行并转换为
`stale_version`，保证“后写不覆盖先写”。

## 4. 事务：多次写入要么全成要么全回滚

所有写操作走显式 `BEGIN IMMEDIATE` 事务（拿到 RESERVED 写锁），
正常 `COMMIT`、异常 `ROLLBACK`。以 `POST /tasks/bulk-cancel` 为例，
一批取消中只要有一个任务不满足条件（终态 / 版本过期 / 不存在），
整批回滚，前面已经 UPDATE 的行也不会留下半更新状态；`task_events`
与状态更新在同一事务内，同生共死。

## 5. 游标分页（多进程持续写入下不重不漏）

- 排序键 `(created_at, id)` 全局唯一、严格递增。
  `created_at` 由库内单行序列表 `task_seq` 在写事务中分配，
  跨进程也严格单调——新写入的数据其键一定大于此前所有页的键，
  因此向后翻页不会把新行插进旧页造成重复或遗漏。
- keyset 分页（无 OFFSET）：

```sql
WHERE (created_at, id) > (?, ?)
ORDER BY created_at ASC, id ASC
LIMIT ?
```

- 复合索引 `idx_tasks_created_id(created_at, id)` 覆盖排序与区间条件，
  数据量变大后是索引区间扫描，不会全表扫描，也没有逐条查询
  （测试中用 `EXPLAIN QUERY PLAN` 断言这一点）。
- 游标为 `(created_at, id)` 的 base64 不透明字符串；损坏/伪造游标返回 400。

### SQLite 并发参数

`journal_mode=WAL`（读写不互斥）、`busy_timeout=10000`（写锁冲突时等待）、
`isolation_level=None`（事务完全显式控制）、`foreign_keys=ON`。

## 6. HTTP 接口

| 方法 & 路径 | 说明 |
|---|---|
| `POST /tasks` | 创建任务，body `{"name": "..."}` |
| `GET /tasks/{id}` | 查询单个任务 |
| `POST /tasks/{id}/claim` | 认领，body `{"executor": "worker-1"}` |
| `POST /tasks/{id}/transition` | 状态推进，body `{"new_status":"completed","expected_version":2,"note":"..."}` |
| `POST /tasks/bulk-cancel` | 批量取消（事务），body `{"tasks": {"1": 1, "2": 1}}`（id→期望版本） |
| `GET /tasks?limit=20&cursor=...` | 游标分页，返回 `{items, next_cursor}` |

错误响应形如：`{"detail": {"error": "stale_version", "message": "..."}}`。

## 7. 本地验证

需要 Python 3.14（或修改 `requires-python`）。依赖由 uv 管理，无外部服务。

```bash
# 安装依赖
uv sync

# 跑全部测试（含多进程并发、版本冲突、非法流转、分页边界/EXPLAIN 索引断言）
uv run pytest -v

# 只看并发认领与跨进程版本冲突
uv run pytest tests/test_concurrency.py -v -s

# 启动服务（默认数据库 ./data/tasks.db，可用 TASK_DB_PATH 覆盖）
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

手动验证示例：

```bash
curl -s -X POST localhost:8000/tasks -H 'Content-Type: application/json' \
  -d '{"name":"demo"}'
# -> {"id":1,...,"status":"pending","version":1,...}

curl -s -X POST localhost:8000/tasks/1/claim \
  -H 'Content-Type: application/json' -d '{"executor":"w1"}'
# -> status=running, version=2

curl -s -X POST localhost:8000/tasks/1/transition \
  -H 'Content-Type: application/json' \
  -d '{"new_status":"completed","expected_version":2}'
# -> status=completed, version=3

curl -s "localhost:8000/tasks?limit=20"
```

## 8. 代码结构

```
app/
  config.py       数据库路径配置（TASK_DB_PATH 可覆盖）
  enums.py        状态枚举与合法流转表
  errors.py       领域错误与错误码（6 类可区分的拒绝原因）
  models.py       Task / Page 数据模型
  db.py           SQLite 连接(WAL)、建表、显式事务、单调序列
  repository.py   原子 UPDATE、keyset 分页、游标编解码
  service.py      状态机校验 + 乐观版本控制 + 事务编排
  schemas.py      请求模型
  api.py          FastAPI 路由与错误码→HTTP 映射
main.py           应用入口（lifespan 建库、全局异常处理）
tests/            并发认领 / 版本冲突 / 非法流转 / 分页边界 / 事务 / API
```
