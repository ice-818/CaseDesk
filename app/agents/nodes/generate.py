"""Answer generation and refusal nodes."""

from __future__ import annotations

import json
import time

from pydantic import BaseModel, Field

from app.agents.product_requirements import matches_requirements
from app.agents.state import AgentState
from app.core.enums import RetrievalStrategy
from app.core.exceptions import LLMOutputError, LLMProviderError
from app.prompts.answer import ANSWER_SYSTEM, ANSWER_USER
from app.providers.base import LLMProvider
from app.providers.fake import OfflineLLMProvider
from app.providers.json_parser import parse_model
from app.schemas.common import LatencyBreakdown, TokenUsage
from app.schemas.retrieval import Citation


class AnswerPayload(BaseModel):
    answer: str
    citation_chunk_ids: list[str] = Field(default_factory=list)
    refused: bool = False


def _navigation_answer(loop: dict, *, continued: bool) -> str:
    if notice := loop.get("navigation_notice"):
        return notice
    verified = loop["verified"]
    if continued and not loop.get("navigation_found"):
        return {
            "budget_exhausted": "已达到本轮查询上限，暂时无法确认更多符合条件的商品。",
            "tool_failure": "商品工具暂时不可用，无法核实更多商品。",
            "needs_clarification": "请先确认本轮选购条件。",
        }.get(loop.get("stop_reason", ""), "当前范围内没有更多符合条件的商品。")
    if not verified:
        return "目前没有已核验商品。"
    if loop["view"] == "select":
        return f"已记录你选中的商品（SKU：{loop['selected_sku']}）。"
    by_sku = {p["sku"]: p for p in verified}
    ranking = {item["sku"]: item for item in loop.get("verified_ranking", [])}
    shown = loop["display_skus"]

    def describe(sku: str) -> str:
        product = by_sku[sku]
        tags = "、".join(product.get("tags") or []) or "无规格标签"
        item = ranking.get(sku)
        scores = "暂无评分"
        if item:
            scores = "、".join(f"{key} {value:.3f}" for key, value in item["feature_scores"].items())
            scores += f"、total_score {item['total_score']:.3f}"
        return (
            f"第{shown.index(sku) + 1}个 {product['name']}（{sku}）｜"
            f"价格 {product['price_cents'] / 100:.2f} 元｜库存 {product['inventory']}｜标签 {tags}｜{scores}"
        )

    if loop["view"] == "compare":
        rows = [describe(sku) for sku in loop["compare_skus"]]
        return "\n".join(["商品对比", *rows, "数据来自已核验详情，不代表独立检测。"])
    return describe(verified[loop["display_index"]]["sku"])


async def generate_node(state: AgentState, llm: LLMProvider) -> dict:
    started = time.perf_counter()
    chunks = state.get("retrieved_chunks") or []
    tools = state.get("tool_results") or []
    clarification = (state.get("extra") or {}).get("product_clarification")
    if clarification:
        return {
            "final_answer": f"推荐前需要确认：{clarification}。",
            "citations": [],
            "extra": {**(state.get("extra") or {}), "product_answer_checked": True},
        }
    requirements = state.get("product_requirements")
    shopping_loop = state.get("shopping_loop")
    if state.get("navigation"):
        answer = (
            _navigation_answer(shopping_loop, continued=bool(state.get("navigation_applied")))
            if shopping_loop else "当前没有可继续浏览的选购结果，请先说明想找什么商品。"
        )
        return {
            "final_answer": answer,
            "citations": [],
            "extra": {
                **(state.get("extra") or {}),
                "product_answer_checked": True,
                "shopping_stop_reason": (shopping_loop or {}).get("stop_reason"),
            },
        }
    if requirements and requirements.active and shopping_loop:
        products = shopping_loop.get("verified") or []
        reason = str(shopping_loop.get("stop_reason") or "insufficient_evidence")
        if products:
            lines = ["以下候选已核对本次硬条件及库存："]
            for index, p in enumerate(products, start=1):
                lines.append(
                    f"第{index}个 {p['name']}（{p['sku']}），{p['price_cents'] / 100:.2f}元；目录依据：{'、'.join(p.get('tags') or [])}。"
                )
            if requirements.evidence_tags:
                lines.append("属性依据来自商品目录标注，不代表独立检测或性能保证。")
            if reason in {"budget_exhausted", "tool_failure"}:
                lines.append("其余候选尚未核实：已达到本轮查询上限或工具暂时不可用。")
            answer = "\n".join(lines)
        else:
            explanations = {
                "no_match": "当前未找到符合全部条件的商品。",
                "tool_failure": "商品工具暂时不可用，无法完成本次核实。",
                "budget_exhausted": "已达到本轮查询上限，暂时无法确认有符合条件的商品。",
            }
            answer = explanations.get(reason, "候选商品的资料不足，无法确认满足全部要求。")
            answer += "没有擅自放宽条件；请补充信息或明确告知是否调整要求。"
        return {
            "final_answer": answer,
            "shopping_loop": {**shopping_loop, "display_skus": [p["sku"] for p in products]},
            "citations": [],
            "extra": {
                **(state.get("extra") or {}),
                "product_answer_checked": True,
                "shopping_stop_reason": reason,
            },
        }
    product_calls = [
        tool for tool in tools if tool.name in {"search_products", "recommend_products"}
    ]
    plan = state.get("plan")
    planned_product = plan and any(
        step.tool_name in {"search_products", "recommend_products"} for step in plan.steps
    )
    if requirements and requirements.constraints and (product_calls or planned_product):
        products = {
            p["sku"]: p
            for tool in product_calls
            if tool.success
            for p in tool.output.get("products", [])
            if isinstance(p, dict)
            and "sku" in p
            and matches_requirements(p, requirements.constraints)
        }
        if not products:
            answer = "当前未查到可核实符合全部条件的商品；我没有放宽预算、颜色或尺码。请补充信息或明确告知是否调整条件。"
        else:
            lines = ["以下商品通过了本次明确预算、颜色和尺码条件的核对："]
            for p in list(products.values())[:3]:
                lines.append(
                    f"{p['name']}（{p['sku']}），价格{p['price_cents'] / 100:.2f}元；规格标签：{'、'.join(p.get('tags') or [])}。"
                )
            lines.append("其他用途和偏好尚需核实，以上不代表已满足未结构化的要求。")
            answer = "\n".join(lines)
        return {
            "final_answer": answer,
            "citations": [],
            "extra": {**(state.get("extra") or {}), "product_answer_checked": True},
        }
    if tools and all(not tool.success for tool in tools):
        return {
            "final_answer": "暂时无法查到相关业务数据，请核对订单号或稍后重试。",
            "citations": [],
        }
    pending = state.get("pending_action")
    if (state.get("extra") or {}).get("missing_order_id"):
        return {"final_answer": "请提供要处理的订单号，确认后我才能继续。", "citations": []}
    if pending:
        answer = pending.summary
        latency = state.get("latency") or LatencyBreakdown()
        latency.llm_ms = (time.perf_counter() - started) * 1000
        return {
            "final_answer": answer,
            "citations": [],
            "refused": False,
            "latency": latency,
        }
    evidence = (
        "\n\n".join(f"[{c.chunk_id}] {c.title}\n{c.content}" for c in chunks[:6]) or "(无检索结果)"
    )
    tool_text = json.dumps([t.model_dump() for t in tools], ensure_ascii=False) if tools else "(无)"
    intent = state.get("intent")
    strategy = state.get("strategy") or RetrievalStrategy.DIRECT
    if isinstance(llm, OfflineLLMProvider):
        answer, citation_ids = _offline_answer(state)
        citations = [
            Citation(
                document_id=chunk.document_id,
                title=chunk.title,
                chunk_id=chunk.chunk_id,
                section=chunk.section,
                policy_layer=chunk.policy_layer,
                merchant_id=chunk.merchant_id,
                applicable_category=chunk.applicable_category,
                policy_version=chunk.policy_version,
            )
            for chunk in chunks
            if chunk.chunk_id in citation_ids
        ]
        return {"final_answer": answer, "citations": citations, "refused": False}
    messages = [
        {"role": "system", "content": ANSWER_SYSTEM},
        {
            "role": "user",
            "content": ANSWER_USER.format(
                query=state["user_query"],
                intent=intent.label.value if intent else "unknown",
                strategy=strategy.value,
                evidence=evidence,
                tool_results=tool_text,
            ),
        },
    ]
    try:
        response = await llm.generate(
            messages,
            temperature=0.2,
            max_tokens=700,
            response_format={"type": "json_object"},
        )
        payload = parse_model(response.content, AnswerPayload)
        usage = response.usage
    except (LLMProviderError, LLMOutputError):
        payload = AnswerPayload(
            answer=_extractive_fallback(state["user_query"], evidence, tool_text),
            citation_chunk_ids=[chunks[0].chunk_id] if chunks else [],
        )
        usage = TokenUsage()
    citations = [
        Citation(
            document_id=chunk.document_id,
            title=chunk.title,
            chunk_id=chunk.chunk_id,
            section=chunk.section,
            policy_layer=chunk.policy_layer,
            merchant_id=chunk.merchant_id,
            applicable_category=chunk.applicable_category,
            policy_version=chunk.policy_version,
        )
        for chunk in chunks
        if chunk.chunk_id in set(payload.citation_chunk_ids)
    ]
    latency = state.get("latency") or LatencyBreakdown()
    latency.llm_ms = (time.perf_counter() - started) * 1000
    prev_usage = state.get("token_usage") or TokenUsage()
    merged = TokenUsage(
        prompt_tokens=prev_usage.prompt_tokens + usage.prompt_tokens,
        completion_tokens=prev_usage.completion_tokens + usage.completion_tokens,
        total_tokens=prev_usage.total_tokens + usage.total_tokens,
    )
    return {
        "final_answer": payload.answer,
        "citations": citations,
        "refused": payload.refused,
        "latency": latency,
        "token_usage": merged,
    }


async def refuse_node(state: AgentState) -> dict:
    return {
        "final_answer": "这个问题超出电商客服范围，我无法协助。如需购物、订单、物流或售后帮助，请换个问题。",
        "refused": True,
        "citations": [],
    }


def _extractive_fallback(query: str, evidence: str, tool_text: str) -> str:
    if evidence and evidence != "(无检索结果)":
        snippet = evidence.split("\n", 1)[-1][:240].strip()
        return f"暂时无法生成完整答复。相关资料节选：\n\n> {snippet}\n\n请以原文为准。"
    if tool_text and tool_text != "(无)":
        return "已查询到系统数据，但暂时无法可靠地整理答复。请稍后重试或转人工。"
    return "暂时无法基于现有信息给出可靠答复，请补充相关信息或转人工。"


def _offline_answer(state: AgentState) -> tuple[str, set[str]]:
    """Use only retrieved text or whitelisted tool fields when no LLM is configured."""
    strategy = state.get("strategy")
    if strategy == RetrievalStrategy.DIRECT:
        return "你好，我可以帮你查看商品政策、订单、物流和售后问题。", set()

    for result in reversed(state.get("tool_results") or []):
        if not result.success:
            continue
        data = result.output
        if result.name == "cancel_order":
            return f"订单 {data.get('order_id', '')} 已取消。", set()
        if result.name == "create_refund_request":
            return f"订单 {data.get('order_id', '')} 的退款申请已提交，尚不代表款项到账。", set()
        if result.name == "create_return_request":
            return f"订单 {data.get('order_id', '')} 的退货申请已提交，尚不代表退款完成。", set()
        if result.name == "get_order":
            status = {
                "created": "待支付",
                "paid": "已支付",
                "shipped": "已发货",
                "delivered": "已签收",
                "cancelled": "已取消",
                "refunding": "退款中",
                "refunded": "已退款",
            }.get(str(data.get("status")), "未知")
            return f"订单 {data.get('order_id', '')} 当前状态：{status}。", set()
        if result.name == "track_shipment":
            events = data.get("events") or []
            latest = events[-1] if events else None
            label = (
                {"warehouse": "仓库处理中", "in_transit": "运输中", "delivered": "已签收"}.get(
                    str(latest.get("status")), "状态未知"
                )
                if isinstance(latest, dict)
                else "暂无物流轨迹"
            )
            return f"订单 {data.get('order_id', '')} 的物流状态：{label}。", set()
        if result.name == "get_refund_status":
            refunds = data.get("refunds") or []
            if not refunds:
                return "这笔订单暂未查询到退款申请记录。", set()
            first = refunds[0]
            if isinstance(first, dict):
                return (
                    f"查询到退款单 {first.get('refund_id', '')}，当前状态：{first.get('status', '未知')}。",
                    set(),
                )
        if result.name == "search_knowledge":
            found = data.get("chunks") or []
            if found and isinstance(found[0], dict):
                item = found[0]
                title = str(item.get("title") or "相关政策")
                excerpt = str(item.get("content") or "").strip()[:260]
                return (
                    f"当前未配置生成模型。找到《{title}》中的资料节选，请核对原文：\n\n> {excerpt}",
                    set(),
                )

    chunks = state.get("retrieved_chunks") or []
    if chunks:
        chunk = chunks[0]
        excerpt = chunk.content.strip()[:260]
        return (
            f"当前未配置生成模型，以下是《{chunk.title}》的资料节选，请核对原文：\n\n> {excerpt}",
            {chunk.chunk_id},
        )
    return (
        "当前无法可靠回答这个问题。请补充相关信息，或联系人工客服。",
        set(),
    )
