"""Real migrated storage + real executor/tools + bounded shopping controller."""

from copy import deepcopy

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.nodes.executor import executor_node
from app.agents.nodes.generate import generate_node
from app.agents.nodes.shopping import shopping_check_node as check_shopping
from app.agents.product_requirements import update_requirements
from app.config.settings import Settings
from app.core.auth_context import bind_auth_checker
from app.core.constants import SHOPPING_RECALL_PAGE_SIZES, SHOPPING_VERIFIED_TARGET
from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.exceptions import InvalidRequestError
from app.core.request_scope import request_session
from app.providers.fake import OfflineLLMProvider
from app.ranking.product import RuleBasedProductRanker
from app.repositories.models import ProductRow
from app.repositories.order_repository import OrderRepository
from app.retrieval.policy_scope import policy_request
from app.schemas.agent import AgentPlan, PlanStep, ToolResult
from app.schemas.auth import AuthIdentity
from app.tools.base import ToolContext
from app.tools.product_tools import GetProductDetailTool, RecommendProductsTool, SearchProductsTool
from app.tools.registry import ToolRegistry
from tests.database_support import migrate_test_database


def shopping_check_node(state, *, settings):
    return check_shopping(state, settings=settings, ranker=RuleBasedProductRanker())


@pytest.fixture
async def catalog():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await migrate_test_database(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def populate(factory, *, count=8, valid=(), merchant="m_demo", prefix="A"):
    async with factory.begin() as db:
        for i in range(count):
            db.add(ProductRow(
                sku=f"{prefix}-{i:02}", merchant_id=merchant, name="通勤鞋", category="服饰",
                price_cents=29900, inventory=5,
                tags="color:黑色,size:42" + (",feature:防滑" if i in valid else ""),
            ))


async def run_shopping(factory, *, tool="recommend_products", query="推荐300元以内黑色42码防滑通勤鞋",
                       budget=40, identity=None):
    identity = identity or AuthIdentity("u_demo", "buyer")

    async def check():
        return identity

    state = {
        "user_id": identity.user_id, "session_id": "recall-session", "trace_id": "recall-test",
        "user_query": query, "product_requirements": update_requirements(query), "step_count": 0,
        "plan": AgentPlan(steps=[PlanStep(
            step_id=1, goal="initial recall", tool_name=tool,
            # A model-supplied offset must not skip the initial page.
            arguments={"query": "通勤鞋", "offset": 999},
        )]),
    }
    settings = Settings(max_tool_calls=budget, max_agent_steps=budget)
    offsets = []
    with bind_data_scope(scope_from_identity(identity)), bind_auth_checker(check), policy_request():
        async with request_session(factory) as db:
            registry = ToolRegistry()
            repo = OrderRepository(db)
            for cls in (RecommendProductsTool, SearchProductsTool, GetProductDetailTool):
                registry.register(cls(repo))
            for _ in range(budget + 2):
                state.update(await executor_node(state, registry=registry, settings=settings))
                for result in state["tool_results"]:
                    assert result.success
                    if result.name in SHOPPING_RECALL_PAGE_SIZES:
                        offsets.append(state.get("shopping_loop", {}).get("recall_offset", 0))
                before = deepcopy(state)
                update = shopping_check_node(state, settings=settings)
                # Node must not mutate the supplied state's lists or dictionaries.
                assert state == before
                state.update(update)
                if state["shopping_loop"]["next"] != "execute":
                    return state, offsets
    pytest.fail("shopping controller exceeded its finite call budget")


@pytest.mark.parametrize("tool", ["recommend_products", "search_products"])
async def test_valid_candidate_after_first_page_is_recalled(catalog, tool):
    page = SHOPPING_RECALL_PAGE_SIZES[tool]
    # SQL correctly enforces price/color/size first; feature evidence is checked in detail.
    await populate(catalog, count=page + 1, valid={page})
    state, offsets = await run_shopping(catalog, tool=tool)
    loop = state["shopping_loop"]
    assert offsets == [0, page]
    assert [p["sku"] for p in loop["verified"]] == [f"A-{page:02}"]
    assert loop["stop_reason"] == "completed"
    assert loop["recall_exhausted"]
    answer = await generate_node(state, OfflineLLMProvider())
    assert f"A-{page:02}" in answer["final_answer"]


@pytest.mark.parametrize("tool", ["recommend_products", "search_products"])
async def test_entire_filtered_catalog_exhausted_without_evidence(catalog, tool):
    page = SHOPPING_RECALL_PAGE_SIZES[tool]
    await populate(catalog, count=page * 2)
    state, offsets = await run_shopping(catalog, tool=tool)
    loop = state["shopping_loop"]
    assert offsets == [0, page, page * 2]  # Full final page needs an empty probe.
    assert loop["recall_offset"] == page * 2
    assert len(loop["candidates"]) == len(loop["checked_skus"]) == page * 2
    assert loop["verified"] == []
    assert loop["recall_exhausted"] and loop["stop_reason"] == "no_match"
    answer = await generate_node(state, OfflineLLMProvider())
    assert "未找到" in answer["final_answer"]
    assert "没有擅自放宽" in answer["final_answer"]
    assert "A-" not in answer["final_answer"]


async def test_impossible_sql_constraints_exhaust_on_first_empty_page(catalog):
    await populate(catalog)
    state, offsets = await run_shopping(catalog, query="推荐300元以内紫色44码通勤鞋")
    assert offsets == [0]
    assert state["shopping_loop"]["stop_reason"] == "no_match"
    assert state["shopping_loop"]["candidates"] == []
    assert state["product_requirements"].constraints == {
        "max_price_cents": 30000, "color": "紫色", "size": "44",
    }


async def test_all_candidates_and_all_verified_when_below_target(catalog):
    await populate(catalog, count=8, valid={4, 7})
    state, offsets = await run_shopping(catalog)
    loop = state["shopping_loop"]
    skus = [p["sku"] for p in loop["candidates"]]
    assert offsets == [0, 3, 6]
    assert skus == [f"A-{i:02}" for i in range(8)]
    assert len(skus) == len(set(skus)) == len(set(loop["checked_skus"]))
    assert [p["sku"] for p in loop["verified"]] == ["A-04", "A-07"]
    assert loop["recall_exhausted"] and loop["stop_reason"] == "completed"


async def test_target_three_still_stops_without_scanning_unneeded_pages(catalog):
    await populate(catalog, count=8, valid=set(range(3, 8)))
    state, offsets = await run_shopping(catalog)
    assert offsets == [0, 3]
    assert len(state["shopping_loop"]["verified"]) == SHOPPING_VERIFIED_TARGET
    assert [p["sku"] for p in state["shopping_loop"]["verified"]] == ["A-03", "A-04", "A-05"]
    assert not state["shopping_loop"]["recall_exhausted"]
    assert state["shopping_loop"]["stop_reason"] == "completed"


@pytest.mark.parametrize("budget,offsets,rows", [(1, [0], 3), (5, [0, 3], 6)])
async def test_paging_stops_at_call_budget(catalog, budget, offsets, rows):
    await populate(catalog, count=15)
    state, actual_offsets = await run_shopping(catalog, budget=budget)
    loop = state["shopping_loop"]
    assert actual_offsets == offsets
    assert state["step_count"] == budget
    assert loop["recall_offset"] == rows
    assert loop["stop_reason"] == "budget_exhausted"
    assert not loop["recall_exhausted"]


async def test_shop_paging_never_returns_other_merchants(catalog):
    await populate(catalog, count=7, valid={6})
    await populate(catalog, count=9, valid=set(range(9)), merchant="m_second", prefix="B")
    state, offsets = await run_shopping(
        catalog, identity=AuthIdentity("agent", "service_agent", merchant_id="m_demo"),
    )
    loop = state["shopping_loop"]
    assert offsets == [0, 3, 6]
    assert len(loop["candidates"]) == 7
    assert {p["merchant_id"] for p in loop["candidates"]} == {"m_demo"}
    assert [p["sku"] for p in loop["verified"]] == ["A-06"]
    assert loop["stop_reason"] == "completed"


@pytest.mark.parametrize("shop", [False, True])
async def test_ranked_candidates_drive_verification_and_display(catalog, shop):
    await populate(catalog, count=5, valid=set(range(5)))
    identity = None
    if shop:
        identity = AuthIdentity("agent", "service_agent", merchant_id="m_demo")
        await populate(catalog, count=1, valid={0}, merchant="m_second", prefix="0-other")
    async with catalog.begin() as db:
        for i, price in enumerate([30000, 15000, 27000, 22500, 21000]):
            (await db.get(ProductRow, f"A-{i:02}")).price_cents = price
        if shop:
            (await db.get(ProductRow, "0-other-00")).price_cents = 22500
        db.add(ProductRow(sku="0-sold-out", merchant_id="m_demo", name="通勤鞋", category="服饰",
                          inventory=0, price_cents=22500, tags="color:黑色,size:42,feature:防滑"))
    state, offsets = await run_shopping(catalog, tool="search_products", identity=identity)
    loop = state["shopping_loop"]
    expected = ["A-03", "A-04", "A-01"]
    assert offsets == [0]
    assert loop["checked_skus"] == [p["sku"] for p in loop["verified"]] == expected
    assert [p["sku"] for p in loop["verified_ranking"]] == expected
    assert [p["total_score"] for p in loop["verified_ranking"]] == sorted(
        (p["total_score"] for p in loop["verified_ranking"]), reverse=True
    )
    assert {p["sku"] for p in loop["candidates"]} == {f"A-{i:02}" for i in range(5)}
    assert loop["stop_reason"] == "completed"
    rendered = (await generate_node(state, OfflineLLMProvider()))["final_answer"]
    assert [sku for sku in expected if sku in rendered] == expected
    assert [rendered.index(sku) for sku in expected] == sorted(rendered.index(sku) for sku in expected)


async def test_next_page_rescores_pool_and_reorders_verified_results(catalog):
    await populate(catalog, count=5, valid={0, 3, 4})
    async with catalog.begin() as db:
        (await db.get(ProductRow, "A-03")).price_cents = 21000
        (await db.get(ProductRow, "A-04")).price_cents = 22500
    state, offsets = await run_shopping(catalog)
    loop = state["shopping_loop"]
    assert offsets == [0, 3]
    assert [p["sku"] for p in loop["candidate_ranking"]] == ["A-04", "A-03", "A-00", "A-01", "A-02"]
    assert loop["checked_skus"] == ["A-00", "A-01", "A-02", "A-04", "A-03"]
    assert [p["sku"] for p in loop["verified"]] == ["A-04", "A-03", "A-00"]


async def test_empty_recommendation_reads_catalog_and_offset(catalog):
    await populate(catalog, count=5)
    with bind_data_scope(scope_from_identity(AuthIdentity("u_demo", "buyer"))):
        async with catalog() as db:
            tool = RecommendProductsTool(OrderRepository(db))
            context = ToolContext(user_id="u_demo", session_id="s", trace_id="t")
            first = await tool.run({}, context)
            second = await tool.run({"offset": 3}, context)
            assert [p["sku"] for p in first.output["products"]] == ["A-00", "A-01", "A-02"]
            assert [p["sku"] for p in second.output["products"]] == ["A-03", "A-04"]
            assert "offset" not in second.output["applied_constraints"]


async def test_sql_filters_are_applied_before_offset(catalog):
    await populate(catalog, count=7)
    await populate(catalog, count=2, merchant="m_second", prefix="0-other")
    async with catalog.begin() as db:
        (await db.get(ProductRow, "A-00")).price_cents = 35000
        (await db.get(ProductRow, "A-01")).inventory = 0
        (await db.get(ProductRow, "A-02")).tags = "color:白色,size:42"
        (await db.get(ProductRow, "A-03")).tags = "color:黑色,size:43"
    with bind_data_scope(scope_from_identity(AuthIdentity("agent", "service_agent", merchant_id="m_demo"))):
        async with catalog() as db:
            products = await OrderRepository(db).find_matching_products(
                query="通勤鞋", category="服饰", min_price_cents=100, max_price_cents=30000,
                colors=["黑色"], size="42", offset=1, limit=1,
            )
            assert [p.sku for p in products] == ["A-05"]


@pytest.mark.parametrize("tool_type", [SearchProductsTool, RecommendProductsTool])
async def test_negative_offset_is_rejected(catalog, tool_type):
    with bind_data_scope(scope_from_identity(AuthIdentity("u_demo", "buyer"))):
        async with catalog() as db:
            tool = tool_type(OrderRepository(db))
            with pytest.raises(InvalidRequestError):
                await tool.run({"query": "通勤鞋", "offset": -1},
                               ToolContext(user_id="u_demo", session_id="s", trace_id="t"))


def test_duplicate_skus_do_not_duplicate_candidates_or_verified():
    product = {"sku": "A", "name": "鞋", "category": "服饰", "price_cents": 100, "inventory": 1, "tags": []}
    state = {
        "product_requirements": update_requirements("推荐鞋"),
        "step_count": 1,
        "tool_results": [ToolResult(name="recommend_products", success=True,
                                    output={"products": [product, product, product]})],
    }
    result = shopping_check_node(state, settings=Settings())
    assert result["shopping_loop"]["recall_offset"] == 3
    assert result["shopping_loop"]["candidates"] == [product]
    state.update(result)
    state["tool_results"] = [ToolResult(name="get_product_detail", success=True, output=product)]
    state["step_count"] = 2
    state.update(shopping_check_node(state, settings=Settings()))
    assert state["shopping_loop"]["verified"] == [product]


def test_time_budget_prevents_next_page(monkeypatch):
    state = {
        "product_requirements": update_requirements("推荐鞋"), "step_count": 2,
        "shopping_loop": {"started_at": 0, "candidates": [], "recall_tool": "recommend_products"},
        "tool_results": [ToolResult(name="recommend_products", success=True,
                                    output={"products": []})],
    }
    monkeypatch.setattr("app.agents.nodes.shopping.time.monotonic", lambda: 31)
    assert shopping_check_node(state, settings=Settings())["shopping_loop"]["stop_reason"] == "budget_exhausted"
