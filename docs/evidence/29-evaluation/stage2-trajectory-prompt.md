# 阶段2 合成用户固定轨迹（32 条）测试生成 Prompt

> 用法：把本文件整段复制给外部 GPT。要求它只产出一个测试文件与一个冻结剧本数据文件，不要修改 `app/` 下任何生产代码，不要新增依赖。业务/交易数据均为仿真，但认证、隔离、幂等、状态机、角色守卫全部是真实实现。

## 一、你的任务

为 CaseDesk（FastAPI + LangGraph 的企业级智能客服/导购平台）新增**阶段2：合成用户固定轨迹测试**，共 **32 条**：导购 16 条 + 售后 16 条。每条轨迹由**冻结的 JSONL 剧本**驱动，通过**真实 `POST /api/v1/chat`** 跑完整 graph，最终对真实数据库终态与 HTTP 状态码做断言。

### 硬约束（违反任意一条即视为不合格）

1. **替身只能替代 LLM**。数据库、会话、工具执行器、售后状态机、写锁、角色守卫、HTTP 路由全部走真实实现。
2. **禁止**在测试里直接改数据库来“推进”状态、**禁止**替换 Planner / 执行器 / 工具仓库 / 状态机、**禁止** monkeypatch 任何 `app/repositories`、`app/tools`、`app/agents/nodes` 下的生产逻辑。唯一允许 monkeypatch 的是 LLM 提供者（见第四节注入方式）。
3. 测试在**独立临时库**运行（沿用 `auth_headers` 夹具机制），绝不能触碰 `data/local/app_merchant.db`。
4. 剧本必须**冻结**：节点响应是确定性的，不允许随机、不允许依赖当前时间或网络。
5. 写操作（申请/审批/取消）都要走真实的“待确认动作 → 带 `confirm_action_id` 与**同一 `session_id`** 确认”两段式，不能跳过确认。
6. 全部测试打 `pytest.mark.trajectory` 标记；其中验证越权的用例额外打 `regression`，验证重复记账的用例也打 `regression`。

## 二、真实事实包（直接引用，不要臆测）

### 1. LLM 节点识别表（按 system 消息首行关键字分派）

一次 `/chat` 可能触发以下节点；替身必须全部能识别，未命中也要返回安全默认值，避免触发规则 fallback。

| 节点 kind | system 首行特征 | 输出（`LLMResponse.content`，JSON 字符串） |
| --- | --- | --- |
| `intent` | `你是电商客服意图分类器` | `IntentResult`：`{"label","confidence","rationale","prompt_version"}`，`prompt_version` 固定 `"intent_v3"` |
| `complexity` | `你是检索复杂度分类器` | `ComplexityResult`：`{"complexity","strategy","confidence","rationale"}` |
| `rewrite` | `你改写电商检索查询` | `{"queries": ["..."]}` |
| `grade` | `你评估检索结果` | Self-RAG 评分：`{"score":"yes/no","rationale":"..."}` |
| `planner` | `你是电商客服规划器` | `AgentPlan`：`{"steps":[PlanStep...],"rationale":"..."}` |
| `answer` | `你是电商平台客服助手` | `AnswerPayload`：`{"answer","citation_chunk_ids":[],"refused":false}` |
| `faithfulness` | `你检测客服回答是否忠实` | `{"supported":true,"hallucinated_spans":[],"rationale":"..."}` |
| `self_correct` | `你是电商客服改写器` | 同 `AnswerPayload` 结构 |

### 2. 意图枚举 `IntentLabel`（label 取值）

`greeting, chitchat, faq_policy, order_inquiry, logistics, refund, return_exchange, cancel_order, product_consult, recommendation, coupon, complaint, human_handoff, out_of_scope`

### 3. 复杂度策略 `RetrievalStrategy`（complexity 节点的 strategy 取值，决定 graph 路由）

- `direct` → `generate_answer`（参数化/问候，不检索）
- `refuse` → `refuse_answer`（边界/政策拒绝）
- `single` / `multi` → `retrieve_docs`
- `agent` → `plan_tasks`（**只有 agent 才进 Planner**）

`complexity` 字段取值：`simple, single_hop, multi_hop, tool_required, out_of_scope`。

### 4. Skill 清单（`app/skills/definitions/`，决定 Planner 可用工具白名单）

| Skill | triggers | 允许工具 |
| --- | --- | --- |
| `product_recommend` | 推荐、有没有类似 | `search_products, recommend_products, check_inventory` |
| `order_tracking` | 物流、快递、到哪了 | `get_order, track_shipment` |
| `order_cancellation` | 取消订单、不想要了 | `get_order, cancel_order` |
| `return_handling` | 退货、换货、7天无理由 | `get_order, get_order_items, search_knowledge, create_return_request` |
| `aftersale_approval`（**新增**） | 审批、批准、驳回、拒绝、签收、打款、完成处理 | `process_refund, process_return, get_refund_status` |

另有始终可用工具：`search_knowledge`、`escalate_to_human`。

### 5. 全部 17 个工具名

`search_knowledge, get_coupon, get_warranty, escalate_to_human, search_products, get_product_detail, recommend_products, check_inventory, get_refund_status, create_refund_request, create_return_request, cancel_order, process_refund, process_return, get_order, get_order_items, track_shipment`

### 6. 关键工具参数（PlanStep.arguments 必须匹配）

- `create_refund_request` / `create_return_request`：`{"order_id": "ORD10001", "reason": "不想要了"}`
- `cancel_order`：`{"order_id": "ORD10003"}`
- `process_refund`：`{"refund_id": "rf_xxx", "action": "approve|reject|complete"}`
- `process_return`：`{"return_id": "rt_xxx", "action": "approve|reject|complete"}`
- `PlanStep` 结构：`{"step_id": 1, "goal": "...", "tool_name": "...", "arguments": {...}, "needs_retrieval": false}`

### 7. 审批角色口径（已锁定，不要改）

- **可以** approve/reject/complete：`operator`、`service_agent`（二者等价，都要测）。
- **买家（buyer）调用任何 process 写工具必须 403**；买家走 `/chat` 发起审批时 `pending_action=None`，单据状态不变。
- 测试身份通过 `auth_headers(...)` 选择：买家 `"u_demo"`、运营 `"op"`、客服 `"agent"`。

### 8. 售后状态机（断言终态用）

`AftersaleStatus`：`requested → approved/rejected`，`approved → completed`，终态不可再转。
退货 `completed` 时在同一工作单元自动插入一笔**全额退款**（金额=订单总额 `total_cents`），订单变 `refunded`。
部分唯一索引保证同一订单只能有一笔活跃（requested/approved）售后。
订单状态：`ORD10001` shipped、`ORD10002` delivered、`ORD10003` paid。

## 三、32 条轨迹清单（逐条实现，预期即验收标准）

### 导购 16 条（SHOP-01 ~ SHOP-16）

| ID | 角色 | 场景 | complexity 路由 | 关键预期 |
| --- | --- | --- | --- | --- |
| SHOP-01 | 买家 | 问候 | direct | 200，无 pending，answer 非空 |
| SHOP-02 | 买家 | 闲聊 | direct | 200，无写操作 |
| SHOP-03 | 买家 | 商品咨询 | single | 真实检索，answer 含 `citation_chunk_ids` |
| SHOP-04 | 买家 | 求推荐 | agent | 出推荐工具 pending，确认后无越权、只含可售商品 |
| SHOP-05 | 买家 | 紫色+44 码（不存在的硬条件组合） | refuse/agent | **零硬条件违例**：不得编造商品，明确无可用项 |
| SHOP-06 | 买家 | 零库存商品 | single/agent | `check_inventory` 显示不可售，不引导下单 |
| SHOP-07 | 买家 | 多轮：推荐→选中→看详情 | agent | navigation=select，会话内状态连续 |
| SHOP-08 | 买家 | 优惠券 | direct/single | 调用 `get_coupon`，信息真实 |
| SHOP-09 | 买家 | 政策 FAQ | single | `search_knowledge`，有引用 |
| SHOP-10 | 买家 | 保修 | single | `get_warranty` 真实返回 |
| SHOP-11 | 买家 | 物流查询 | agent | `track_shipment`，pending 确认 |
| SHOP-12 | 买家 | 投诉 | agent | `escalate_to_human`，转人工 |
| SHOP-13 | 买家 | 越界请求 | refuse | `refused=true`，进入 refuse_answer |
| SHOP-14 | 买家 | 商品对比 | agent/multi | navigation=compare，不泄露其他租户数据 |
| SHOP-15 | 买家 | 导购回答忠实性 | 任意 | faithfulness 返回 `supported=true`、无 hallucinated spans |
| SHOP-16 | 买家 | 同一查询重复两次 | 同前 | 归一化耗时字段后，终态/回答完全一致（确定性） |

### 售后 16 条（AFTER-01 ~ AFTER-16）

| ID | 角色 | 场景 | 关键预期（含真实 DB 终态） |
| --- | --- | --- | --- |
| AFTER-01 | 买家 | 申请退款 | 两段确认后退款单 `requested`，返回 `rf_` 单号 |
| AFTER-02 | 买家 | 申请退货 | 退货单 `requested`，返回 `rt_` 单号，**未**自动退款 |
| AFTER-03 | operator | 审批通过退款 | `process_refund approve` → `approved` |
| AFTER-04 | service_agent | 审批通过退款 | 角色等价，同样 `approved` |
| AFTER-05 | operator | 驳回退款 | `process_refund reject` → `rejected` |
| AFTER-06 | operator | 已批准退款打款 | `complete` → `completed`，订单 `refunded` |
| AFTER-07 | 买家 | 尝试审批自己的退款 | `/chat` 无 pending；直接 process 被 **403**（regression） |
| AFTER-08 | operator | 审批通过退货 | `process_return approve` → `approved` |
| AFTER-09 | operator | 签收退货 | `complete` → 自动插入**1 笔**全额退款，金额=订单总额，订单 `refunded`（regression） |
| AFTER-10 | service_agent | 驳回退货 | `process_return reject` → `rejected` |
| AFTER-11 | operator | requested 直接 complete | 非法流转被拒，状态保持 `requested` |
| AFTER-12 | operator | 已 completed 再 approve | 终态不可转，被拒，无新增记录 |
| AFTER-13 | 买家 | 已有活跃售后再申请 | 唯一索引/状态机拒绝，活跃单仍只有 1 笔 |
| AFTER-14 | 买家 | 对不可见/不存在订单申请 | 返回 **404**，无任何写入 |
| AFTER-15 | 买家 | 读自己的退款状态 | `get_refund_status` 成功（读），写操作一律 403 |
| AFTER-16 | operator | 同一确认重复提交 | **零重复记账**：不产生第二笔退款，金额不翻倍（regression） |

## 四、替身注入方式（唯一允许的 monkeypatch，提供完整骨架）

测试环境 `DEEPSEEK_API_KEY` 为空，`build_container` 会使用 `OfflineLLMProvider`。在 TestClient 进入 startup **之前** monkeypatch `app.services.container.OfflineLLMProvider`，让它返回你的脚本化 provider：

```python
import app.services.container as container_mod
from app.providers.base import LLMResponse
from app.schemas.common import TokenUsage


class ScriptedLLMProvider:
    def __init__(self, script):
        # script: dict[kind, callable(user_text, messages) -> str]
        self.script = script
        self.calls = []

    async def ping(self):
        return True

    @staticmethod
    def _identify(system_text):
        if system_text.startswith("你是电商客服意图分类器"):
            return "intent"
        if system_text.startswith("你是检索复杂度分类器"):
            return "complexity"
        if system_text.startswith("你改写电商检索查询"):
            return "rewrite"
        if system_text.startswith("你评估检索结果"):
            return "grade"
        if system_text.startswith("你是电商客服规划器"):
            return "planner"
        if system_text.startswith("你是电商平台客服助手"):
            return "answer"
        if system_text.startswith("你检测客服回答是否忠实"):
            return "faithfulness"
        if system_text.startswith("你是电商客服改写器"):
            return "self_correct"
        return "unknown"

    async def generate(self, messages, *, temperature=0.0, max_tokens=1024, response_format=None):
        system_text = next((m["content"] for m in messages if m["role"] == "system"), "")
        kind = self._identify(system_text)
        user_text = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        content = self.script[kind](user_text, messages)
        self.calls.append({"kind": kind, "user": user_text})
        return LLMResponse(content=content, usage=TokenUsage(total_tokens=8), model="scripted")


def make_client(monkeypatch, script):
    provider = ScriptedLLMProvider(script)
    monkeypatch.setattr(container_mod, "OfflineLLMProvider", lambda: provider)
    # 仅在 e2e 目录下使用，配合 tests/conftest.py 的 auth_headers 与临时库
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app())
```

## 五、断言要求

1. 不仅断言 HTTP 状态码，还要通过真实只读查询（`app.state.container.session_factory` 或真实 GET 端点）核对售后单/订单的**真实终态、笔数、金额**。
2. 越权类（AFTER-07、AFTER-14、AFTER-15）必须断言数据库零变更、无令牌/密钥泄露。
3. 重复记账类（AFTER-09、AFTER-16）必须断言退款笔数恰为 1、累计金额恰为订单总额。
4. 硬条件类（SHOP-05、SHOP-06）必须断言回答不包含任何不满足条件的商品、不伪造 SKU。

## 六、交付物（仅两个文件，不要动其他东西）

1. `tests/e2e/test_synthetic_trajectories.py`：32 个测试，文件级 `pytest.mark.trajectory`，越权/重复记账用例加 `regression`。
2. `tests/e2e/fixtures/trajectory_scripts.json`（或 `.jsonl`）：冻结的 32 条剧本节点响应数据，测试从该文件加载，不允许在测试函数内硬编码随机内容。

完成后请附上：每个文件的完整内容、32 条测试的 node id 列表，以及你对“哪些断言依赖真实数据库终态而非仅 HTTP 200”的逐条说明。
