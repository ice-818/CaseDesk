import pytest

from app.agents.navigation import parse_navigation
from app.agents.nodes.generate import generate_node
from app.agents.nodes.shopping import shopping_check_node
from app.agents.product_requirements import update_requirements
from app.config.settings import Settings
from app.providers.fake import OfflineLLMProvider
from app.ranking.product import RuleBasedProductRanker
from app.schemas.agent import ToolResult


@pytest.mark.parametrize(
    "query,command,indexes",
    [
        (word, "next", []) for word in ("换一个", "下一个", "再来一个", "换一款", "还有吗", "其他的呢", "别的", "再换一个")
    ] + [
        (word, "previous", []) for word in ("上一个", "前一个", "刚才那个", "回到上一个")
    ] + [
        (" 对比这两个？ ", "compare", []),
        ("比较第2和第四个", "compare", [2, 4]),
        ("比对第一个与第十二款", "compare", [1, 12]),
        ("就这个", "select", []),
        ("就选它", "select", []),
        ("第2个", "select", [2]),
        ("第3个吧", "select", [3]),
        ("选第两个", "select", [2]),
    ] + [
        (word, "unknown", []) for word in ("下不了单", "不要黑色", "上次的订单", "换一个白色的", "对比订单", "下一个订单", "不要", "比较第1")
    ],
)
def test_parse_navigation(query, command, indexes):
    parsed = parse_navigation(query)
    assert (parsed.command, parsed.indexes, parsed.raw) == (command, indexes, query)
    assert parse_navigation(query) == parsed


def test_navigation_preserves_requirements_and_known_synonyms():
    initial = update_requirements("推荐300元以内黑色42码防滑跑步鞋")
    assert not update_requirements("换一个").active
    assert not update_requirements("换一个", update_requirements("你好")).active
    continued = update_requirements("换一个", initial)
    assert continued.model_dump(exclude={"source_query"}) == initial.model_dump(exclude={"source_query"})
    synonym = update_requirements("跑鞋", initial)
    assert synonym.constraints == initial.constraints
    assert synonym.evidence_tags == initial.evidence_tags
    assert synonym.task_query == initial.task_query


def state_with_verified():
    products = [
        {"sku": sku, "name": "通勤鞋", "category": "服饰", "inventory": 3, "price_cents": 21000, "tags": []}
        for sku in ("A", "B", "C")
    ]
    return {
        "product_requirements": update_requirements("推荐300元以内通勤鞋"),
        "step_count": 6,
        "shopping_loop": {
            "verified": products, "display_skus": ["A", "B", "C"],
            "candidates": products, "checked_skus": ["A", "B", "C"],
            "recall_offset": 3, "recall_tool": "recommend_products", "stop_reason": "completed",
            "verified_ranking": [
                {"sku": p["sku"], "total_score": 0.6, "feature_scores": {
                    "relevance": 1.0, "price_fit": 0.5, "inventory": 0.5, "tag_match": 0.5,
                }} for p in products
            ],
        },
    }


def run_command(state, text, *, settings=None):
    state = {**state, "navigation": parse_navigation(text), "product_requirements": update_requirements(
        text, state["product_requirements"]
    )}
    state.pop("navigation_applied", None)
    return {**state, **shopping_check_node(
        state, settings=settings or Settings(), ranker=RuleBasedProductRanker(),
    )}


async def test_pointer_navigation_compare_and_select_are_tool_free():
    state = state_with_verified()
    for text, index in [("上一个", 0), ("换一个", 1), ("再换一个", 2), ("换一个", 0), ("下一个", 1), ("上一个", 0)]:
        state = run_command(state, text)
        assert state["shopping_loop"]["display_index"] == index
        assert state["shopping_loop"]["checked_skus"] == ["A", "B", "C"]
        assert state["shopping_loop"]["recall_offset"] == 3
        assert state["step_count"] == 6
        assert state["shopping_loop"]["next"] == "finish"
    for text, skus in [("对比", ["A", "B"]), ("比较第一和第三", ["A", "C"])]:
        state = run_command(state, text)
        assert state["shopping_loop"]["compare_skus"] == skus
        answer = (await generate_node(state, OfflineLLMProvider()))["final_answer"]
        for text in ("价格", "库存", "标签", "relevance", "price_fit", "inventory", "tag_match", "total_score", "不代表独立检测"):
            assert text in answer
    state = run_command(state, "选第2个")
    assert state["shopping_loop"]["selected_sku"] == "B"
    for command in ("选第4个", "对比第0和第2", "对比第1和第1"):
        state = run_command(state, command)
        assert state["shopping_loop"]["navigation_notice"]
        assert state["shopping_loop"]["selected_sku"] == "B"


@pytest.mark.parametrize("reason,step,exhausted,expected", [
    ("budget_exhausted", 6, False, "查询上限"),
    ("completed", 2, True, "没有更多"),
    (None, 6, False, "查询上限"),
])
async def test_next_does_not_search_when_limited(reason, step, exhausted, expected):
    state = state_with_verified()
    state["step_count"] = step
    loop = state["shopping_loop"]
    loop.update(verified=loop["verified"][:1], display_skus=["A"], stop_reason=reason, recall_exhausted=exhausted)
    result = run_command(state, "下一个")
    assert result["shopping_loop"]["next"] == "finish"
    assert expected in (await generate_node(result, OfflineLLMProvider()))["final_answer"]


async def test_next_fetches_another_page_with_original_conditions_when_needed():
    state = state_with_verified()
    state["step_count"] = 2
    loop = state["shopping_loop"]
    loop.update(verified=loop["verified"][:1], display_skus=["A"])
    result = run_command(state, "下一个")
    assert result["plan"].steps[0].arguments == {"query": "通勤鞋", "offset": 3, "max_price_cents": 30000}
    product = {**loop["verified"][0], "sku": "D"}
    result["tool_results"] = [ToolResult(name="recommend_products", success=True, output={"products": [product]})]
    result["step_count"] = 3
    result.update(shopping_check_node(result, settings=Settings(), ranker=RuleBasedProductRanker()))
    assert result["plan"].steps[0].arguments == {"sku": "D"}
    result["tool_results"] = [ToolResult(name="get_product_detail", success=True, output=product)]
    result["step_count"] = 4
    result.update(shopping_check_node(result, settings=Settings(), ranker=RuleBasedProductRanker()))
    assert result["shopping_loop"]["display_skus"] == ["A", "D"]
    assert "第2个" in (await generate_node(result, OfflineLLMProvider()))["final_answer"]
