"""Bounded, deterministic observe/decide controller for read-only shopping."""

from __future__ import annotations

import time
from dataclasses import asdict

from app.agents.navigation import NavigationCommand
from app.agents.product_requirements import matches_requirements, protect_arguments
from app.agents.state import AgentState
from app.config.settings import Settings
from app.core.constants import (
    SHOPPING_RECALL_PAGE_SIZES,
    SHOPPING_TIME_BUDGET_SECONDS,
    SHOPPING_VERIFIED_TARGET,
)
from app.ranking.product import ProductRanker
from app.schemas.agent import AgentPlan, PlanStep
from app.schemas.order import Product


def _show_current(loop: dict) -> None:
    sku = loop["verified"][loop["display_index"]]["sku"]
    loop["display_skus"] = list(dict.fromkeys([*loop["display_skus"], sku]))


def _navigate(loop: dict, command: NavigationCommand) -> bool:
    """Apply a view change. Return True only if next needs a new candidate."""
    verified = loop.get("verified", [])
    shown = loop["display_skus"]
    index = loop["display_index"]
    loop["view"] = "list"
    loop.pop("navigation_notice", None)
    loop.pop("navigation_found", None)
    if command.command == "next":
        if index + 1 < len(verified):
            loop["display_index"] += 1
        elif len(verified) >= SHOPPING_VERIFIED_TARGET:
            loop["display_index"] = 0
        elif loop.get("stop_reason") == "budget_exhausted":
            loop["navigation_notice"] = "已达到本轮查询上限，暂无更多已核验商品。"
            return False
        elif loop.get("stop_reason") == "needs_clarification":
            loop["navigation_notice"] = "请先确认本轮选购条件。"
            return False
        elif loop.get("recall_exhausted") and all(
            item["sku"] in loop["checked_skus"] for item in loop.get("candidates", [])
        ):
            loop["navigation_notice"] = "当前范围内没有更多符合条件的商品。"
            return False
        else:
            return True
        _show_current(loop)
    elif command.command == "previous":
        if not verified or index == 0:
            loop["navigation_notice"] = "已经在首位，没有更早的候选。" if verified else "目前没有已核验商品。"
        else:
            loop["display_index"] -= 1
            _show_current(loop)
    else:
        by_sku = {item["sku"]: item for item in verified}
        indexes = command.indexes
        if indexes:
            skus = [shown[i - 1] for i in indexes if 1 <= i <= len(shown)]
        elif command.command == "compare":
            skus = [p["sku"] for p in verified[index : index + 2]]
        else:
            skus = [verified[index]["sku"]] if verified and verified[index]["sku"] in shown else []
        needed = 2 if command.command == "compare" else 1
        if len(set(skus)) != needed or any(sku not in by_sku for sku in skus):
            loop["navigation_notice"] = (
                "请指定两个不同的已展示商品序号，或先查看更多候选。"
                if needed == 2 else "请指定一个已展示的商品序号。"
            )
        elif command.command == "compare":
            loop["view"] = "compare"
            loop["compare_skus"] = skus
            loop["display_skus"] = list(dict.fromkeys([*shown, *skus]))
        else:
            loop["view"] = "select"
            loop["selected_sku"] = skus[0]
            loop["display_index"] = next(i for i, p in enumerate(verified) if p["sku"] == skus[0])
    return False


def shopping_check_node(state: AgentState, *, settings: Settings, ranker: ProductRanker) -> dict:
    requirements = state.get("product_requirements")
    navigation = state.get("navigation")
    if navigation and not state.get("shopping_loop"):
        return {}
    if not requirements or not requirements.active or state.get("confirmed_action"):
        return {}
    loop = dict(state.get("shopping_loop") or {})
    loop.setdefault("checked_skus", [])
    loop.setdefault("verified", [])
    loop.setdefault("history", [])
    loop.setdefault("recall_offset", 0)
    loop.setdefault("recall_exhausted", False)
    loop.setdefault("display_index", 0)
    loop.setdefault("display_skus", [])
    loop.setdefault("selected_sku", None)
    loop.setdefault("compare_skus", [])
    loop.setdefault("view", "list")
    loop["next"] = "finish"
    first_navigation = bool(navigation and not state.get("navigation_applied"))
    if first_navigation and navigation and not _navigate(loop, navigation):
        return {"shopping_loop": loop}
    if first_navigation:
        loop.pop("stop_reason", None)
    loop.setdefault("started_at", time.monotonic() - loop.get("elapsed_seconds", 0))
    navigation_update = {"navigation_applied": True} if navigation else {}

    def stop(reason: str) -> dict:
        loop["stop_reason"] = reason
        return {**navigation_update, "shopping_loop": loop}

    if (state.get("extra") or {}).get("product_clarification"):
        return stop("needs_clarification")
    results = [] if first_navigation else state.get("tool_results") or []
    if not first_navigation:
        loop["history"] = [
            *loop["history"], {"tools": [r.name for r in results], "step_count": state.get("step_count", 0)}
        ]
        if not results or not all(result.success for result in results):
            return stop("tool_failure")
    candidates = {p["sku"]: p for p in loop.get("candidates", [])}
    recalled_page = False
    added_sku = None
    for result in results:
        if result.name in SHOPPING_RECALL_PAGE_SIZES:
            recalled_page = True
            loop.setdefault("recall_tool", result.name)
            batch = result.output.get("products", [])
            # Advance by rows returned, not the deduplicated candidate count.
            loop["recall_offset"] += len(batch)
            loop["recall_exhausted"] = len(batch) < SHOPPING_RECALL_PAGE_SIZES[result.name]
            candidates.update(
                (p["sku"], p) for p in batch if isinstance(p, dict) and "sku" in p
            )
    loop["candidates"] = list(candidates.values())
    if recalled_page or "candidate_ranking" not in loop:
        ranking = ranker.rank([Product.model_validate(p) for p in candidates.values()], requirements)
        loop["candidate_ranking"] = [asdict(item) for item in ranking]
    for result in results:
        if result.name == "get_product_detail":
            product = result.output
            if (
                product.get("sku") == loop.get("pending_sku")
                and matches_requirements(product, requirements.constraints)
                and type(product.get("inventory")) is int
                and product["inventory"] > 0
                and all(
                    f"feature:{tag}" in (product.get("tags") or [])
                    for tag in requirements.evidence_tags
                )
            ):
                verified = {p["sku"]: p for p in loop["verified"]}
                verified[product["sku"]] = product
                ranking = ranker.rank([Product.model_validate(p) for p in verified.values()], requirements)
                loop["verified"] = [verified[item.sku] for item in ranking]
                loop["verified_ranking"] = [asdict(item) for item in ranking]
                added_sku = product["sku"]
    if navigation and added_sku:
        loop["navigation_found"] = True
        loop["display_index"] = next(i for i, p in enumerate(loop["verified"]) if p["sku"] == added_sku)
        _show_current(loop)
    # Once the display target is reached, further page and detail calls add no value.
    if len(loop["verified"]) >= SHOPPING_VERIFIED_TARGET:
        return stop("completed")
    remaining = [
        item["sku"] for item in loop["candidate_ranking"] if item["sku"] not in loop["checked_skus"]
    ]
    if (
        state.get("step_count", 0) >= min(settings.max_tool_calls, settings.max_agent_steps)
        or time.monotonic() - loop["started_at"] >= SHOPPING_TIME_BUDGET_SECONDS
    ):
        return stop("budget_exhausted")
    # Below the target, finishing a page only ends the search if the filtered
    # catalog is exhausted. The budget check above takes precedence over no_match.
    if not remaining and loop["recall_exhausted"]:
        return stop("completed" if loop["verified"] else "no_match")
    if navigation and added_sku:
        return {"shopping_loop": loop}
    if not remaining:
        if not loop.get("recall_tool"):
            return stop("tool_failure")
        loop["next"] = "execute"
        return {
            **navigation_update,
            "shopping_loop": loop,
            "plan": AgentPlan(
                steps=[
                    PlanStep(
                        step_id=1,
                        goal="recall next candidate batch",
                        tool_name=loop["recall_tool"],
                        arguments=protect_arguments(
                            {"query": requirements.task_query or "", "offset": loop["recall_offset"]},
                            requirements,
                        ),
                    )
                ]
            ),
        }
    sku = remaining[0]
    loop["checked_skus"] = [*loop["checked_skus"], sku]
    loop["pending_sku"] = sku
    loop["next"] = "execute"
    return {
        **navigation_update,
        "shopping_loop": loop,
        "plan": AgentPlan(
            steps=[
                PlanStep(
                    step_id=1,
                    goal="verify candidate evidence",
                    tool_name="get_product_detail",
                    arguments={"sku": sku},
                )
            ]
        ),
    }


def after_shopping_check(state: AgentState) -> str:
    return (
        "execute_tools"
        if (state.get("shopping_loop") or {}).get("next") == "execute"
        else "generate_answer"
    )


async def checkpointed_shopping_check(
    state: AgentState, *, settings: Settings, ranker: ProductRanker
) -> dict:
    update = shopping_check_node(state, settings=settings, ranker=ranker)
    if state.get("shopping_checkpoint_id"):
        from app.core.request_scope import get_db_session
        from app.repositories.shopping_checkpoint import ShoppingCheckpointRepository

        db = get_db_session()
        await ShoppingCheckpointRepository(db).save({**state, **update})
        # This path is restricted to shopping reads; commit snapshots before
        # requesting the next tool so later failures cannot roll them back.
        await db.commit()
    return update
