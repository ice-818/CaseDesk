# PostgreSQL 可移植性实测证据

- 日期：2026-11-25
- 范围：在独立 PostgreSQL 容器上验证 Alembic 全量迁移、数据库级部分唯一索引、写锁路径、黑盒 HTTP 关键链路与幂等，不修改任何已提交代码与现有 SQLite 数据。

## 1. 目标

项目默认使用 SQLite 开发，但代码与迁移脚本声明了对 PostgreSQL 的方言兼容（部分索引 `postgresql_where`、写锁 `FOR UPDATE`）。本次实测用于证明这些机制在真实 PostgreSQL 上确实成立，而非仅在 SQLite 上验证。

## 2. 环境

| 组件 | 版本 / 配置 |
| --- | --- |
| Docker Engine | 29.4.0（Docker Desktop 4.70.0） |
| 镜像 | `postgres:16-alpine` |
| 容器名 | `casedesk-pg-verify` |
| 端口映射 | 本地 `55432` → 容器 `5432` |
| 账号 / 库 | 用户 `cs`，数据库 `casedesk`，临时本地密码 |
| 存储 | 不挂载命名卷，容器删除即数据消失 |
| 应用 | 本地 `.venv` 启动 uvicorn，端口 `8012`，`DEEPSEEK_API_KEY=""`（离线模式） |

连接串：`postgresql+asyncpg://cs:***@127.0.0.1:55432/casedesk`

### 网络问题与处理（如实记录）

直连 Docker Hub 首次出现 `TLS handshake timeout`；多个国内镜像源在下载层文件（Cloudflare R2 等后端）时连接中断（`EOF`）。本机 Clash 代理（端口 7890）当前节点失效，经 Clash 控制接口将节点从失效的香港节点切换到可用节点后，`registry-1.docker.io` 返回 401（未带认证的正常响应，证明连通），随后成功拉取镜像。未新增任何依赖（asyncpg 本就在依赖清单）。

## 3. 迁移与结构验证

`alembic upgrade head` 成功执行 `0001 → 0010` 全部 10 个迁移，`alembic_version.version_num = 0010`。

- 共建立 19 张表（含 `alembic_version`），包括 `users`、`auth_session`、`orders`、`order_items`、`refunds`、`returns`、`idempotency_keys` 等。
- 部分唯一索引在 PostgreSQL 上的真实定义：

```sql
CREATE UNIQUE INDEX uq_refunds_active_order
ON public.refunds USING btree (order_id)
WHERE ((status)::text = ANY (ARRAY['requested'::character varying,
                                   'approved'::character varying]::text[]));

CREATE UNIQUE INDEX uq_returns_active_order
ON public.returns USING btree (order_id)
WHERE ((status)::text = ANY (ARRAY['requested'::character varying,
                                   'approved'::character varying]::text[]));
```

- `refunds` 表 CHECK 约束：`status` 仅允许 `requested / approved / rejected / completed`。

## 4. 黑盒 HTTP 验证（17/17 通过）

通过 httpx 对运行中的应用发起真实 HTTP 请求，再用 asyncpg 直连核对终态。

| # | 验证点 | 结果 |
| --- | --- | --- |
| 1 | 未认证访问 `/chat` 返回 401 | PASS |
| 2 | 买家 `u_demo` 登录返回 200 与令牌 | PASS |
| 3 | `/auth/me` 买家身份正确 | PASS |
| 4 | 导购问答（推荐耳机）返回 200 | PASS |
| 5 | 导购返回回答或商品视图 | PASS |
| 6 | 订单查询（ORD10001）返回 200 | PASS |
| 7 | 申请退款 ORD10003 返回待确认动作 | PASS |
| 8 | 待确认动作为 `create_refund_request` | PASS |
| 9 | 带 `confirm_action_id` 确认后返回 200 | PASS |
| 10 | 确认后无待确认动作 | PASS |
| 11 | 相同确认载荷幂等重放返回 200 | PASS |
| 12 | 买家发起"批准退款"无审批写动作（角色守卫） | PASS |
| 13 | `refunds` 终态仅 1 条（幂等未重复创建） | PASS |
| 14 | 退款单状态为 `requested` | PASS |
| 15 | 退款金额 = 订单总额 7900 分 | PASS |
| 16 | `idempotency_keys` 已写入记录 | PASS |
| 17 | 同订单第二条 active 退款被部分索引拒绝（UniqueViolation） | PASS |

第 17 项数据库实际报错：

```
duplicate key value violates unique constraint "uq_refunds_active_order"
```

## 5. 结论

- Alembic 全量迁移、数据库级部分唯一索引、CHECK 约束、写操作两段式确认、幂等表与角色守卫在真实 PostgreSQL 16 上全部按预期工作。
- 写操作经 `OrderRepository._lock_order_for_write`，在非 SQLite 方言下使用 `SELECT ... FOR UPDATE` 行级锁路径，黑盒写库与终态核对通过。
- 本次为可移植性验证，不做性能压测，不报告 QPS / TPS。

## 6. 已知边界

- 售后审批（`process_refund` / `process_return`）需要在消息中提供 `rf_` / `rt_` 单号；离线确定性 planner 不解析该参数，该参数在正常运行时由 LLM 从上下文提取。因此审批端到端 HTTP 流程未纳入本次离线黑盒，改为通过部分索引与写锁的数据库级验证覆盖其正确性。
- 验证用临时脚本 `tmp_pg_verify.py` 已在结束后删除，不进入版本库。

## 7. 回滚

- 停止并删除容器：`docker rm -f casedesk-pg-verify`（数据随容器删除）。
- 删除临时验证脚本。
- 现有 SQLite 数据库与已提交代码在整个过程中未被修改。
