# BUG-002：售后审批工具缺角色守卫，且审批计划被多层过滤，主链路无法触达审批

| 字段 | 内容 |
| --- | --- |
| bug_id | BUG-002 |
| 标题 | process_refund/process_return 无角色守卫（买家可越权处理售后），同时 planner 与 skill 白名单把审批计划静默过滤，审批链路从聊天入口不可达 |
| 严重级别 | P1（工具层缺角色校验违反身份隔离；但当前主链路不可直接利用，且无真实资金移动，故定 P1 而非 P0） |
| 归因层 | 工具授权层 + 规划节点 + Skill 白名单 + 复杂度路由，四处共同作用 |
| 数据版本 | Alembic 0010，SQLite 内存/文件库 |
| 回归用例 | `tests/integration/test_aftersale_transition.py::test_buyer_cannot_process_aftersale`（参数化 refund/return）；审批主链路由同文件 `test_process_refund_tool_runs_approved` 覆盖 |
| 发现方式 | 阶段2 合成轨迹设计时，用脚本替身驱动真实 HTTP 入口，发现审批计划被过滤后逐层定位 |

## 问题拆解（三个相互耦合的缺口）

1. **工具层无角色守卫**：`ProcessRefundTool` / `ProcessReturnTool` 的 `authorize()` 只调用
   `_get_scoped_refund` / `_get_scoped_return` 做资源归属校验，不检查当前角色。若计划被接通，
   买家可审批自己的售后单，属越权。
2. **planner_node 过滤审批步骤**：写步骤统一经 `_explicit_write_request()` 判定，而它只识别
   “申请退款/申请退货/取消订单”，approve/reject/complete 一律返回 False，处理步骤被静默过滤。
3. **Skill 白名单与复杂度路由**：intent=refund 命中 `return_handling` skill，其工具白名单不含
   process_*；且复杂度分类未把请求路由为 `AGENT` 时，graph 根本不会进入 `plan_tasks` 节点
   （只有 `RetrievalStrategy.AGENT` 才走 planner）。

## 修复（最小改动，纵深防御）

- 工具层：两个 process 工具的 `authorize()` 增加角色断言，
  `require_data_scope().role` 必须属于 `{operator, service_agent}`，否则抛 `ForbiddenError(403)`。
- 新增独立 Skill `app/skills/definitions/aftersale_approval.json`，triggers 为
  审批/驳回/签收/打款等措辞，工具白名单含 `process_refund`、`process_return`、`get_refund_status`。
- `Planner.create_plan()`：命中审批 Skill 但当前角色不是 approver 时，把 skill 置空，
  买家无法进入审批流程。
- `planner_node`：新增 `_explicit_process_request` / `_write_step_is_requested`，结合角色
  保留处理步骤；`_has_required_handle` 区分 process 工具需要的 refund_id/return_id 与
  申请/取消工具需要的 order_id，避免误判“缺少订单号”。

## 修复后证据（通过）

真实 HTTP 链路（TestClient，替身只替代 LLM，DB/执行器/状态机均真实）：

- operator 发送“请审批通过 rf_xxx 这笔退款”，节点序列为
  `intent → complexity(strategy=agent) → planner`，返回 `pending_action`（tool_name=process_refund）。
- 带 `confirm_action_id` 与同一 `session_id` 确认后，退款单状态由 requested 变为 approved。
- 买家发送同样请求：`pending_action=None`，退款单始终保持 requested，无任何越权写入。
- 买家直接调用 process 工具：`authorize` 抛 `ForbiddenError`。

## 副作用预期（回归断言）

- 买家任何处理请求都被拒绝，售后单状态与订单状态保持不变。
- 审批/驳回/完成仍受售后四状态状态机约束，非法流转抛 `InvalidRequestError`。
- 退货 complete 自动插入全额退款的闭环不变，金额守恒（累计已完成退款不超过订单总额）。
