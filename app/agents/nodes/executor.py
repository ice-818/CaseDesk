"""Tool execution node with argument fallback and retries."""

from __future__ import annotations

import hashlib
import json
import time

from app.agents.product_requirements import extract_requirements, protect_arguments
from app.agents.state import AgentState
from app.config.settings import Settings
from app.core.auth_context import check_execution_identity
from app.core.exceptions import (
    AppError,
    AuthenticationUnavailableError,
    ForbiddenError,
    InvalidRequestError,
    ResourceNotFoundError,
    ToolExecutionError,
    ToolPermissionError,
    UnauthorizedError,
)
from app.core.request_scope import get_db_session
from app.observability.metrics import TOOL_CALLS
from app.repositories.idempotency import IdempotencyRepository
from app.schemas.agent import PlanStep, ToolResult
from app.schemas.common import LatencyBreakdown
from app.tools.base import Tool, ToolContext
from app.tools.permissions import WRITE_RISKS, assert_permitted
from app.tools.registry import ToolRegistry

READ_FALLBACKS: dict[str, list[str]] = {
    "get_order": ["search_knowledge"],
    "get_order_items": ["get_order", "search_knowledge"],
    "track_shipment": ["get_order", "search_knowledge"],
    "get_product_detail": ["search_products", "search_knowledge"],
    "search_products": ["search_knowledge"],
    "recommend_products": ["search_products", "search_knowledge"],
    "get_coupon": ["search_knowledge"],
    "get_warranty": ["search_knowledge"],
    "get_refund_status": ["search_knowledge"],
    "check_inventory": ["search_products", "search_knowledge"],
    "mcp_store_hours": ["search_knowledge"],
    "mcp_warehouse_cutoff": ["search_knowledge"],
}


def _fingerprint(tool_name: str, arguments: dict) -> str:
    payload = json.dumps(
        {"tool": tool_name, "arguments": arguments}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def fallback_arguments(tool_name: str, step: PlanStep, query: str) -> dict:
    if tool_name == "search_knowledge":
        return {"query": query[:200]}
    if tool_name in {"search_products", "recommend_products"}:
        arguments = dict(step.arguments)
        arguments.pop("category", None)
        arguments["query"] = (arguments.get("query") or query)[:80]
        return arguments
    return dict(step.arguments)


async def execute_with_fallback(
    tool: Tool,
    step: PlanStep,
    context: ToolContext,
    registry: ToolRegistry,
    query: str,
) -> ToolResult:
    """Execution-layer fallback: a failed read tool tries the next mapped tool."""
    await check_execution_identity()
    try:
        result = await tool.run(step.arguments, context)
        if result.success:
            return result
        raise ToolExecutionError(result.error or f"{tool.name} returned success=false")
    except (ToolPermissionError, UnauthorizedError, ForbiddenError, AuthenticationUnavailableError, ResourceNotFoundError):
        raise
    except (AppError, InvalidRequestError) as exc:
        last_error = str(exc)
        for fallback_name in READ_FALLBACKS.get(tool.name, []):
            if fallback_name not in registry.names():
                continue
            fallback = registry.get(fallback_name)
            await check_execution_identity()
            try:
                fb_result = await fallback.run(
                    fallback_arguments(fallback_name, step, query), context
                )
                if fb_result.success:
                    merged = {
                        **fb_result.output,
                        "fallback_from": tool.name,
                        "fallback_error": last_error,
                    }
                    TOOL_CALLS.labels(tool=fallback_name, status="fallback").inc()
                    return fb_result.model_copy(update={"output": merged})
            except (UnauthorizedError, ForbiddenError, AuthenticationUnavailableError, ResourceNotFoundError):
                raise
            except (AppError, InvalidRequestError):
                continue
        raise


async def executor_node(
    state: AgentState,
    *,
    registry: ToolRegistry,
    settings: Settings,
) -> dict:
    plan = state.get("plan")
    if plan is None:
        return {"tool_results": []}
    if state.get("pending_action") and not state.get("confirm_action_id"):
        return {"tool_results": []}

    started = time.perf_counter()
    results: list[ToolResult] = []
    context = ToolContext(
        user_id=state["user_id"],
        session_id=state["session_id"],
        trace_id=state["trace_id"],
        confirmed_action_id=state.get("confirm_action_id"),
        idempotency_key=state.get("idempotency_key"),
    )
    idempotency = IdempotencyRepository(get_db_session())
    calls = 0
    query = state["user_query"]
    requirements = state.get("product_requirements") or extract_requirements(query)
    if requirements.source_query != query:
        requirements = extract_requirements(query)
    for step in plan.steps:
        if (
            calls >= settings.max_tool_calls
            or (state.get("step_count") or 0) >= settings.max_agent_steps
        ):
            break
        if not step.tool_name:
            continue
        tool_name = step.tool_name
        if step.tool_name in {
            "search_products",
            "recommend_products",
            "get_product_detail",
            "check_inventory",
        }:
            if requirements.clarification:
                return {
                    "tool_results": [],
                    "product_requirements": requirements,
                    "extra": {
                        **(state.get("extra") or {}),
                        "product_clarification": requirements.clarification,
                    },
                }
            if step.tool_name in {"search_products", "recommend_products"}:
                arguments = protect_arguments(step.arguments, requirements)
                if requirements.active:
                    # Pagination is owned by the controller, never the model.
                    arguments["offset"] = (state.get("shopping_loop") or {}).get("recall_offset", 0)
                step = step.model_copy(
                    update={"arguments": arguments}
                )
        tool = registry.get(tool_name)
        await check_execution_identity()
        confirmed = bool(state.get("confirm_action_id"))
        try:
            if tool.risk in WRITE_RISKS:
                assert_permitted(tool, confirmed=confirmed)
                parsed = tool.parse(step.arguments)
                await tool.authorize(parsed, context)
                step = step.model_copy(update={"arguments": parsed.model_dump()})
                idem_key = context.confirmed_action_id or context.idempotency_key
                if idem_key:
                    fp = _fingerprint(tool.name, step.arguments)
                    existing = await idempotency.get_if_matches(idem_key, fp)
                    if existing is not None:
                        results.append(
                            ToolResult(
                                name=tool.name, success=True, output=existing, risk=tool.risk
                            )
                        )
                        TOOL_CALLS.labels(tool=tool.name, status="idempotent").inc()
                        calls += 1
                        continue
                    result = await tool.run(step.arguments, context)
                    await idempotency.put(idem_key, fp, result.output)
                    TOOL_CALLS.labels(tool=tool.name, status="ok").inc()
                    results.append(result)
                    calls += 1
                    continue
            if requirements.active and tool.name in {
                "recommend_products",
                "search_products",
                "get_product_detail",
                "check_inventory",
            }:
                # The shopping controller owns continuation. Do not silently
                # fall back to unrelated knowledge or exceed its call budget.
                result = await tool.run(step.arguments, context)
            else:
                result = await execute_with_fallback(tool, step, context, registry, query)
            TOOL_CALLS.labels(tool=tool.name, status="ok").inc()
            results.append(result)
        except (UnauthorizedError, ForbiddenError, AuthenticationUnavailableError, ResourceNotFoundError):
            raise
        except ToolPermissionError as exc:
            TOOL_CALLS.labels(tool=tool.name, status="denied").inc()
            results.append(
                ToolResult(name=tool.name, success=False, error=str(exc), risk=tool.risk)
            )
        except AppError as exc:
            if exc.error_code.value == "IDEMPOTENCY_CONFLICT":
                TOOL_CALLS.labels(tool=tool.name, status="conflict").inc()
            else:
                TOOL_CALLS.labels(tool=tool.name, status="error").inc()
            results.append(
                ToolResult(name=tool.name, success=False, error=str(exc), risk=tool.risk)
            )
        calls += 1
    latency = state.get("latency") or LatencyBreakdown()
    latency.tool_ms += (time.perf_counter() - started) * 1000
    return {
        "tool_results": results,
        "product_requirements": requirements,
        "step_count": (state.get("step_count") or 0) + calls,
        "latency": latency,
    }
