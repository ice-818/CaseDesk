# 售后幂等与状态机

售后写操作（退款、退货、取消）必须满足两条：**重复提交只生效一次**，**单据只能沿合法状态流转**。
本系统不靠单一开关，而是用三层防御纵深，任何一层失守，下一层仍能兜底。

## 状态机

退款单与退货单共用同一套四状态，定义在 `app/core/enums.py:AftersaleStatus`。

```text
            approve                complete
requested ───────────► approved ───────────► completed   （终态）
    │
    └──────────────► rejected                            （终态）
```

| 状态 | 含义 |
| --- | --- |
| `requested` | 用户已提交，等待审批，占用订单的活跃名额 |
| `approved` | 审批通过，仍占用名额；退款等待打款，退货等待收货 |
| `rejected` | 审批拒绝，终态，释放名额 |
| `completed` | 退款打款完成 / 退货签收完成，终态，释放名额 |

合法转换集中在 `app/repositories/order_repository.py` 的 `_AFTERSALE_TRANSITIONS`：
`requested` 只能转 `approved`/`rejected`，`approved` 只能转 `completed`，终态不能再转。
任何不在表内的转换抛 `InvalidRequestError`，因此「未 approve 直接 complete」「completed 再 approve」都会被拒绝。

## 数据库层：硬约束

迁移 `0010`（`alembic/versions/0010_aftersale_guard.py`）建立两类约束，不依赖应用是否被调用：

- **CHECK 约束** `ck_refunds_status` / `ck_returns_status`：状态只能是四个合法值。
- **部分唯一索引** `uq_refunds_active_order` / `uq_returns_active_order`：
  仅对 `status IN ('requested','approved')` 的行在 `order_id` 上唯一。

部分索引的关键在于终态行不满足 WHERE 条件、不占名额，所以一笔单据终结后，同一订单可以重新发起申请，
而活跃期间任何重复插入都会被数据库拒绝。索引同时声明 `sqlite_where` 与 `postgresql_where`，
在两种后端语义一致。

## 业务规则

`create_refund` / `create_return` 在插入前按顺序校验（见 `order_repository.py`）：

1. 同表是否已有活跃单据 → 有则拒绝，杜绝同类型重复申请。
2. 跨表是否已有活跃单据（退款与退货互斥路径）→ 有则拒绝。
3. 订单状态是否属于 `paid/shipped/delivered`；`created/cancelled/refunded` 不允许再开售后。
4. 金额安全：`累计已完成退款 + 本次金额 ≤ 订单总额`，否则拒绝，防止超额与重复退款。

在任何读取之前，创建入口先对订单加写锁（`_lock_order_for_write`）：
SQLite 用 `BEGIN IMMEDIATE` 提前取得库级写锁，其它后端对订单行 `SELECT ... FOR UPDATE`。
这让同一订单的两个售后写者**串行化**——第二个写者阻塞到第一个提交后才读取，
因此一定能看到已提交的活跃单据并被业务规则拒绝。这一步是为了堵住快照隔离窗口：
若第二个事务在第一个提交前就建立了只读快照，它会读不到对方刚提交的单据，
而部分唯一索引只约束单表、挡不住退款与退货的跨表组合，于是可能出现两者同时活跃。

并发请求即使同时通过应用层检查，最终在部分唯一索引上也会有一个失败；
该 `IntegrityError` 被捕获并转成确定性的 `409 IDEMPOTENCY_CONFLICT`，而不是裸 500。

## 退货到退款闭环

退货单 `approved → completed` 表示仓库已签收退货。`transition_return` 在同一工作单元内：

1. 退货单标记 `completed`。
2. 自动插入一笔金额为订单总额、状态 `completed` 的退款单。
3. 订单状态置为 `refunded`。

三步要么一起成功，要么外层回滚全部撤销，不会出现「货已收但没退款」的中间态。
纯退款路径则由 `transition_refund` 在 `completed` 时把订单置为 `refunded`；
退款被 `rejected` 时，订单按是否有签收/发货时间回退到 `delivered/shipped/paid`。

## 三层防御纵深

| 层 | 机制 | 防住什么 |
| --- | --- | --- |
| 请求级 | `IdempotencyRepository` + 确认 ID/幂等 key，fingerprint 不符返回 409 | 客户端带 key 的重试、网络重发 |
| 业务级 | 订单写锁串行化 + 活跃单据、跨表、状态、累计金额校验 | 不带 key 的重复提交、跨表竞态、非法操作 |
| 数据库级 | CHECK + 部分唯一索引 | 并发竞态、绕过应用层的写入 |

审批动作通过 `process_refund` / `process_return` 两个工具触发，`action` 限定为
`approve/reject/complete`，与其它写工具一样需要用户确认后才执行。

## 边界

- 退款金额当前按订单全额，未开放用户指定的部分退款；累计金额校验已具备，扩展部分退款只需补输入校验。
- 不做真实支付网关与资金到账，`completed` 是仿真状态流转，不代表真实资金移动。
- PostgreSQL 尚未实测；部分索引已按方言写好，恢复 Docker 后需补全量验证。

迁移与约束的可执行验证见
[`docs/evidence/28-aftersale/verify_0010.py`](../evidence/28-aftersale/verify_0010.py)，
对抗用例见 `tests/integration/test_aftersale_guard.py` 与 `test_aftersale_transition.py`。
