import pytest

from app.agents.nodes.shopping import after_shopping_check, shopping_check_node as check_shopping
from app.agents.product_requirements import update_requirements
from app.config.settings import Settings
from app.ranking.product import RuleBasedProductRanker
from app.schemas.agent import ToolResult


def shopping_check_node(state, *, settings):
    return check_shopping(state, settings=settings, ranker=RuleBasedProductRanker())


def product(sku="shoe", features=True):
    return {
        "sku": sku,
        "name": "鞋",
        "category": "鞋",
        "price_cents": 29900,
        "inventory": 1,
        "tags": ["color:黑色", "size:42"] + (["feature:防滑"] if features else []),
    }


def initial():
    return {
        "product_requirements": update_requirements("推荐300元以内黑色42码防滑鞋"),
        "step_count": 1,
        "tool_results": [
            ToolResult(name="recommend_products", success=True, output={"products": [product()]})
        ],
    }


def test_observe_plan_detail_then_stop_when_verified():
    state = initial()
    state.update(shopping_check_node(state, settings=Settings()))
    assert after_shopping_check(state) == "execute_tools"
    step = state["plan"].steps[0]
    assert step.tool_name == "get_product_detail"
    assert step.arguments == {"sku": "shoe"}
    state.update(
        tool_results=[ToolResult(name="get_product_detail", success=True, output=product())],
        step_count=2,
    )
    state.update(shopping_check_node(state, settings=Settings()))
    assert after_shopping_check(state) == "generate_answer"
    assert state["shopping_loop"]["stop_reason"] == "completed"
    assert len(state["shopping_loop"]["verified"]) == 1


@pytest.mark.parametrize(
    "detail",
    [
        product(features=False),
        {**product(), "price_cents": 35000},
        {**product(), "inventory": 0},
        product(sku="other"),
    ],
)
def test_missing_evidence_or_changed_details_are_not_approved(detail):
    state = initial()
    state.update(shopping_check_node(state, settings=Settings()))
    state.update(
        tool_results=[ToolResult(name="get_product_detail", success=True, output=detail)],
        step_count=2,
    )
    result = shopping_check_node(state, settings=Settings())["shopping_loop"]
    assert result["stop_reason"] == "no_match"
    assert result["verified"] == []


def test_empty_results_do_not_retry_or_relax_requirements():
    state = initial()
    state["tool_results"][0].output["products"] = []
    result = shopping_check_node(state, settings=Settings())["shopping_loop"]
    assert result["stop_reason"] == "no_match"
    assert result["next"] == "finish"
    assert state["product_requirements"].constraints["max_price_cents"] == 30000


def test_call_budget_stops_before_another_tool_call():
    state = initial()
    state["step_count"] = Settings().max_tool_calls
    result = shopping_check_node(state, settings=Settings())["shopping_loop"]
    assert result["stop_reason"] == "budget_exhausted"


def test_tool_failure_stops_without_infinite_retries():
    state = initial()
    state["tool_results"] = [
        ToolResult(name="recommend_products", success=False, error="unavailable")
    ]
    assert (
        shopping_check_node(state, settings=Settings())["shopping_loop"]["stop_reason"]
        == "tool_failure"
    )


def test_features_survive_multiturn_and_reset_for_new_task():
    first = initial()["product_requirements"]
    assert update_requirements("白色也可以", first).evidence_tags == ["防滑"]
    assert update_requirements("不要防滑", first).evidence_tags == []
    assert update_requirements("再推荐耳机", first).evidence_tags == []
