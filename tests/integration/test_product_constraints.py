import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.exceptions import InvalidRequestError
from app.repositories.models import ProductRow
from app.repositories.order_repository import OrderRepository
from app.schemas.auth import AuthIdentity
from app.tools.base import ToolContext
from app.tools.product_tools import RecommendProductsTool, SearchProductsTool
from tests.database_support import migrate_test_database


@pytest.fixture
async def repository():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await migrate_test_database(engine)
    try:
        async with async_sessionmaker(engine)() as session:
            rows = [
                ("A-expensive", 35000, "color:黑色,size:42", 1, "鞋"),
                ("B-wrong-color", 25000, "color:白色,size:42", 1, "鞋"),
                ("C-wrong-size", 25000, "color:黑色,size:43", 1, "鞋"),
                ("D-no-size", 25000, "color:黑色", 1, "鞋"),
                ("E-no-stock", 25000, "color:黑色,size:42", 0, "鞋"),
                ("F-partial-size", 25000, "color:黑色,size:420", 1, "鞋"),
                ("G-other-category", 25000, "color:黑色,size:42", 1, "帽子"),
                ("Z-match", 30000, "color:黑色,size:42", 1, "鞋"),
            ]
            for sku, price, tags, inventory, category in rows:
                session.add(
                    ProductRow(
                        merchant_id="m_demo",
                        sku=sku,
                        name="通勤鞋" if category == "鞋" else "帽子",
                        category=category,
                        price_cents=price,
                        inventory=inventory,
                        tags=tags,
                        description="黑色42码（文案不能替代结构化规格）",
                    )
                )
            await session.flush()
            yield OrderRepository(session)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", [RecommendProductsTool, SearchProductsTool])
async def test_filters_all_constraints_before_limit(repository, tool_type):
    tool = tool_type(repository)
    result = await tool.run(
        {"query": "通勤鞋", "max_price_cents": 30000, "color": "黑色", "size": "42"},
        ToolContext(user_id="u", session_id="s", trace_id="t"),
    )
    assert [p["sku"] for p in result.output["products"]] == ["Z-match"]


@pytest.mark.asyncio
async def test_no_match_does_not_relax_budget(repository):
    tool = RecommendProductsTool(repository)
    result = await tool.run(
        {"category": "鞋", "max_price_cents": 29999, "color": "黑色", "size": "42"},
        ToolContext(user_id="u", session_id="s", trace_id="t"),
    )
    assert result.success
    assert result.output["products"] == []
    assert result.output["status"] == "no_match"
    assert result.output["applied_constraints"]["max_price_cents"] == 29999


@pytest.mark.asyncio
async def test_category_and_query_are_both_applied(repository):
    assert await repository.find_matching_products(query="帽子", category="鞋") == []
    assert await repository.find_matching_products(query="%") == []
    assert await repository.find_matching_products(size="42%") == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"min_price_cents": 30100, "max_price_cents": 30000},
        {"max_price_cents": -1},
        {"max_price_cents": 30000.5},
        {"max_price_cents": True},
        {"max_price_cents": "30000"},
        {"color": ""},
        {"size": " "},
        {"size": "42,43"},
        {"budget": 300},
    ],
)
def test_invalid_constraints_rejected(arguments):
    with pytest.raises(InvalidRequestError):
        RecommendProductsTool().parse(arguments)


@pytest.fixture(autouse=True)
def trusted_buyer_scope():
    with bind_data_scope(scope_from_identity(AuthIdentity("u", "buyer"))):
        yield
