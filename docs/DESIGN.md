# 设计说明：状态机、并发与冲突处理

本文档说明任务状态流转规则、多进程并发正确性、乐观锁冲突策略、
游标分页原理，以及本地验证方法。

## 1. 状态流转规则

任务有 6 个固定状态：

```
pending ──► claimed ──► running ──► completed
   │            │           │
   ├─► cancelled ◄──────────┘   (pending/claimed/running 可取消)
   └────────────────────────► running ──► failed
```

合法的相邻流转（仅这些被允许）：

| 当前状态 | 可推进到 |
| --- | --- |
| `pending` | `claimed`、`cancelled` |
| `claimed` | `running`、`cancelled` |
| `running` | `completed`、`failed`、`cancelled` |
| `completed` / `failed` / `cancelled` | （终止态，无） |

拒绝原因被拆成不同异常，调用方可以明确区分：

- **跳级 / 倒流** → `IllegalTransitionError`
  （如 `pending→running`、`claimed→completed`、`running→claimed`）。
- **终止态再变更** → `TerminalStateError`
  （如 `failed→running`）。
- **重复完成** → `TaskAlreadyCompletedError`
  （`completed→completed`），与一般终止态错误区分开。
- **越权推进** → `TaskNotClaimedError`
  （claimed/running 之后只有认领者本人，即 `owner`，才能推进）。

## 2. 并发认领：为什么恰好一个成功

认领不是"先 SELECT 再 UPDATE"两步，而是单条条件 UPDATE：

```sql
UPDATE tasks
   SET status='claimed', owner=?, version=version+1, updated_at=?
 WHERE id=? AND status='pending' AND version=?;
```

- SQLite 写操作在表级写锁/文件锁下串行执行，多个执行进程的两条
  UPDATE 不可能同时匹配到同一行；第一个提交把行改成 claimed 后，
  后者的 `status='pending'` 条件不再命中，`rowcount=0`。
- 文件库开启 **WAL + `busy_timeout=30s`**：读者不阻塞写者，
  锁竞争时等待重试而不是立刻报 `database is locked`。
- 服务层在 UPDATE 未命中时**重读一行**，把失败归类为
  `TaskAlreadyClaimedError`（已被他人认领）或
  `VersionConflictError`（快照过期）。

`tests/test_concurrency.py` 用 `multiprocessing`（spawn）拉起 8 个
真实进程抢同一个任务，断言恰好 1 个 `ok`、7 个 `already_claimed`，
且库里 owner 唯一、version 只递增一次。

## 3. 乐观锁与版本冲突

每行带 `version`，创建时为 1，每次成功写入 +1。读取任务时拿到
`version`，之后所有修改都必须带 `expected_version`：

```sql
UPDATE tasks SET ..., version=version+1
 WHERE id=? AND status=? AND version=?;
```

判定顺序（先写优先）：

1. 任务不存在 → `TaskNotFoundError`；
2. `expected_version != 当前 version` → `VersionConflictError`。
   只要读取后的快照已被别人先写入，就**显式报错、绝不后写覆盖
   先写**——即使最新状态同时已终止，也优先归为版本冲突；
3. 当前执行者不是 owner → `TaskNotClaimedError`；
4. 状态机不允许 → 第 1 节中的对应错误。

### 3.1 一次操作里多次写入的原子性

所有写操作通过 `BEGIN IMMEDIATE` 事务提交（见 `task_service/db.py`）。
批量推进 `bulk_transition` 把多条 UPDATE 放进**同一个写事务**，
逐条做存在性 / 版本 / 归属 / 状态机校验：任何一条失败，整个事务
`ROLLBACK`，已写入的行一并撤销，不会留下"半更新"状态。

## 4. 游标分页

使用**键集分页（keyset pagination）**，不用 OFFSET：

```sql
SELECT ... FROM tasks WHERE id > ? ORDER BY id LIMIT ?;
-- 按状态过滤时走 (status, id) 复合索引：
SELECT ... FROM tasks WHERE status=? AND id > ? ORDER BY id LIMIT ?;
```

- 分页键是 `INTEGER PRIMARY KEY AUTOINCREMENT`，单调递增、绝不复用。
- 每页多取 1 行判断是否有下一页，游标为上一页最后一条的 id
  （base64 不透明编码）。
- 每条查询恒定走主键 / `(status, id)` 索引，复杂度只与页大小有关，
  不会随数据量增大变成逐条查询或全表扫描。
- 多进程持续写入时，新任务 id 都更大，不会插入到已翻过的页之前：
  已访问页不会重复出现，旧快照范围内的记录也不会漏掉。
  （语义与"快照点之前存在的行"一致；新数据在后续页尾部出现。）

## 5. 本地验证方法

```bash
uv sync

# 单元 + 并发 + 回滚 + 分页测试（含真实多进程）
uv run pytest --log-cli-level=INFO

# 启动服务后手工验证并发认领
uv run python main.py &
curl -s -X POST localhost:8000/tasks -d '{"title":"demo"}' \
  -H 'Content-Type: application/json'
for i in 1 2; do
  curl -s -X POST localhost:8000/tasks/1/claim -H "X-Owner: w$i" &
done; wait
# 两个响应中只有一个 200，另一个 409 TaskAlreadyClaimedError
```

### 关键文件

- `task_service/models.py`：状态枚举、合法流转表、状态机校验；
- `task_service/errors.py`：可区分的错误类型；
- `task_service/db.py`：SQLite 连接（WAL/busy_timeout）与事务；
- `task_service/repository.py`：建表索引、条件 UPDATE、键集分页；
- `task_service/service.py`：认领归类、乐观锁、批量事务编排；
- `task_service/api.py`：FastAPI 路由与错误→HTTP 映射。
