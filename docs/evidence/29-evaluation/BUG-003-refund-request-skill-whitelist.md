# BUG-003：refund 意图的退款申请计划被 Skill 白名单过滤，买家只退款链路不可达

| 字段 | 内容 |
| --- | --- |
| bug_id | BUG-003 |
| 标题 | `refund` 意图经 INTENT_TO_SKILL 映射到 return_handling，其工具白名单不含 `create_refund_request`，导致买家“只退款、不退货”的写计划被 `_sanitize_plan` 静默过滤 |
| 严重级别 | P1（核心售后写链路被阻断，11 条轨迹无法继续；但无越权、无资金重复记账风险，故定 P1 而非 P0） |
| 归因层 | Skill 白名单 + 意图映射 |
| 数据版本 | Alembic 0010，SQLite 临时库 |
| 回归用例 | `tests/e2e/test_synthetic_trajectories.py::test_fixed_trajectory[AFTER-01]`（买家申请只退款，须返回 pending） |
| 发现方式 | 阶段2 合成 32 轨迹首次运行，11 条售后轨迹在“申请退款”步失败，逐步定位到 Skill 白名单 |

## 根因

`app/skills/loader.py` 的 `INTENT_TO_SKILL` 把 `refund` 与 `return_exchange` 都映射到
`return_handling`。但该 Skill 的工具白名单只有
`get_order, get_order_items, search_knowledge, create_return_request`，
**不含 `create_refund_request`**。

于是当买家说“申请退款 ORD…”（intent=refund，且 query 不含“退货”等 trigger）：

1. `match_skill` 走意图映射拿到 `return_handling`；
2. `_allowed_tools` 生成的白名单不含 `create_refund_request`；
3. `_sanitize_plan` 把替身返回的 `create_refund_request` 步骤过滤；
4. 结果 `pending_action=None`，依赖该退款单的后续审批/完成步骤全部无法验证。

## 修复（职责正交，与 BUG-002 的 aftersale_approval 模式一致）

- 新增独立 Skill `app/skills/definitions/refund_request.json`，triggers 为
  `申请退款 / 我要退款 / 退钱`，工具白名单含
  `get_order, get_order_items, search_knowledge, create_refund_request`。
- `INTENT_TO_SKILL` 拆分：`refund → refund_request`，`return_exchange → return_handling` 保持不变。

trigger 刻意使用“申请退款”等具体措辞而非单个“退款”，避免污染：
审批句（含“审批”）由排序更靠前的 `aftersale_approval` 命中；“查询…退款进度”
（intent=faq_policy）不命中 trigger，走通用工具集，`get_refund_status` 正常。

## 修复后证据（通过）

- 32 条固定轨迹 **32/32 全部通过**，全部经真实 HTTP 与真实数据库终态核对。
- 全量后端 **548 passed**，Ruff、mypy（145 文件）均通过。

## 相关的两条“预期 vs 机制”修正（非业务缺陷）

修复白名单后，AFTER-14 与 AFTER-16 仍失败，经核实是测试预期与真实安全机制不符，已按确认口径修正：

- **AFTER-14**：写操作两段式确认，首次只出 pending（200，零业务写入），确认时
  fail-closed 返回 **403**（写路径找不到资源统一 403，不暴露资源存在性）。
- **AFTER-16**：重复提交同一 `confirm_action_id` 命中幂等表，返回 **200 幂等重放**，
  实测终态 `refund_count=1`、`total=7900`（金额未翻倍）、零业务变更，不重复记账。

## 副作用预期（回归断言）

- 买家只退款与退货两条链路分离，互不串用工具。
- 退款申请仍受资源归属、可发起状态与部分唯一索引约束。
- 幂等重放与 fail-closed 不改变金额守恒：累计已完成退款不超过订单总额。
