from copy import deepcopy

import pytest

from app.agents.product_requirements import ProductRequirements
from app.agents.shopping_view import build_shopping_view


def requirements(**changes):
    return ProductRequirements(source_query="推荐鞋", active=True).model_copy(update=changes)


def product(sku):
    return {
        "sku": sku, "name": "通勤鞋", "category": "服饰", "price_cents": 22500,
        "inventory": 5, "tags": ["color:黑色", "size:42"], "merchant_id": "m_demo",
    }


@pytest.mark.parametrize("view", ["list", "compare", "select"])
def test_view_projects_only_verified_products_in_ranked_order(view):
    loop = {
        "view": view, "verified": [product("B"), product("A")], "display_skus": ["A", "B"],
        "display_index": 1, "stop_reason": "completed", "navigation_notice": "提示",
        "compare_skus": ["A", "B", "hidden"], "selected_sku": "A",
        "candidates": [product("hidden")], "checked_skus": ["A", "B", "hidden"],
        "verified_ranking": [{"sku": "B", "feature_scores": {"relevance": 0.5}, "total_score": 0.7}],
    }
    req = requirements(constraints={"max_price_cents": 30000, "color": "黑色", "size": "42"}, evidence_tags=["防滑"])
    before = deepcopy(loop)
    result = build_shopping_view(loop, req)
    assert result == {
        "view": view, "display_index": 1, "stop_reason": "completed", "notice": "提示",
        "selected_sku": "A", "compare_skus": ["A", "B"],
        "conditions": {"max_price_cents": 30000, "color": "黑色", "size": "42", "evidence_tags": ["防滑"]},
        "products": [
            {key: value for key, value in product("B").items() if key != "merchant_id"}
            | {"position": 2, "feature_scores": {"relevance": 0.5}, "total_score": 0.7},
            {key: value for key, value in product("A").items() if key != "merchant_id"}
            | {"position": 1, "feature_scores": {}, "total_score": None},
        ],
    }
    result["products"][0]["tags"].clear()
    result["products"][0]["feature_scores"].clear()
    result["conditions"]["evidence_tags"].clear()
    assert loop == before
    assert req.evidence_tags == ["防滑"]


def test_missing_verified_or_inactive_returns_none():
    assert build_shopping_view(None, requirements()) is None
    assert build_shopping_view({"candidates": [product("hidden")]}, requirements()) is None
    assert build_shopping_view({"verified": [product("A")]}, requirements(active=False)) is None
    assert build_shopping_view({"verified": [product("A")]}, None) is None


def test_defaults_and_multiple_colors_without_inventing_unassigned_position():
    loop = {"verified": [product("A")], "selected_sku": "hidden"}
    result = build_shopping_view(loop, requirements())
    assert result["conditions"] == {"max_price_cents": None, "color": None, "size": None, "evidence_tags": []}
    assert result["products"][0]["position"] is None
    assert result["selected_sku"] is None
    result = build_shopping_view(loop, requirements(constraints={"colors": ["黑色", "白色"]}))
    assert result["conditions"]["color"] == "黑色 / 白色"
