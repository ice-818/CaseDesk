# BUG-001：跨表售后并发可在同一订单同时活跃

| 字段 | 内容 |
| --- | --- |
| bug_id | BUG-001 |
| 标题 | 退款与退货并发时，可在同一订单上同时成为活跃单据，跨表互斥失效 |
| 严重级别 | P0（直接破坏发布底线「售后零重复记账」） |
| 归因层 | 事务/隔离层（快照隔离窗口）；数据库约束只覆盖单表，跨表无共同约束 |
| 数据版本 | Alembic 0010，SQLite 文件库 |
| 回归用例 | `tests/integration/test_aftersale_faults.py::test_concurrent_refund_and_return_cannot_both_be_active` |
| 发现方式 | 阶段 4 受控并发交错（`asyncio.Event` 固定时序，非随机线程调度） |

## 触发时序

两个独立连接针对同一订单 `ORD10003`：

1. 事务 A（create_refund）开始写流程并持有写锁，尚未提交。
2. 事务 B（create_return）在 A 持锁期间通过第一次读取建立了自己的事务快照。
3. A 提交并释放锁。
4. B 继续执行其活跃单据检查，但 B 的快照在 A 提交前已固化，读不到 A 刚提交的退款。
5. 部分唯一索引 `uq_refunds_active_order` / `uq_returns_active_order` 只在各自单表内对
   `order_id` 唯一，无法约束“退款表一行 + 退货表一行”的跨表组合。
6. B 成功插入退货并提交，最终出现一个活跃退款和一个活跃退货并存。

## 修复前证据（失败）

受控交错终态：`active (refund, return) = (1, 1)`，与跨表互斥设计相悖。

## 修复

在 `create_refund` / `create_return` 的**任何读取之前**调用 `_lock_order_for_write`：

- SQLite：`BEGIN IMMEDIATE` 提前取得库级写锁（SQLite 无行锁）。
- 其它后端：对父订单行 `SELECT ... FOR UPDATE`。

第二个写者在加锁阶段即被阻塞，直到第一个事务提交；它拿到锁后才开始读取，
因此看到的是已提交的最新状态，被活跃单据检查拒绝，而不是基于过期快照继续。

## 修复后证据（通过）

- 同一受控交错终态：`active (refund, return) = (1, 0)`。
- B 收到 `InvalidRequestError`，且其事务回滚，全表快照在请求前后零变更。
- 全量后端测试 463 通过，Ruff / mypy 无问题。

## 副作用预期（回归断言）

- 被拒绝的请求不得新增任何售后单据，订单状态保持不变。
- 任意时刻同一订单至多一个活跃售后单据（无论类型）。
- 该测试用固定事件时序，不依赖真实线程调度，重复运行结果确定，不产生 flaky。
