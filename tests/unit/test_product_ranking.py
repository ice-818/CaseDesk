from dataclasses import asdict

import pytest

from app.agents.product_requirements import ProductRequirements
from app.ranking.product import RankingWeights, RuleBasedProductRanker
from app.schemas.order import Product


def product(sku, *, price=7500, stock=10, tags=(), name="shoe", category="apparel"):
    return Product(
        sku=sku, name=name, category=category, price_cents=price, inventory=stock, tags=list(tags)
    )


def requirements(*, query="shoe", budget=10000, tags=()):
    return ProductRequirements(
        source_query=query or "", task_query=query,
        constraints={} if budget is None else {"max_price_cents": budget},
        evidence_tags=list(tags),
    )


def test_weighted_order_and_explanation():
    candidates = [
        product("A", price=4500),
        product("B", price=9800, tags=["feature:grip"]),
        product("C", price=8000, tags=["feature:grip"]),
    ]
    req = requirements(tags=["grip"])
    ranker = RuleBasedProductRanker()
    ranking = ranker.rank(candidates, req)
    assert [item.sku for item in ranking] == ["C", "B", "A"]
    weights = asdict(RankingWeights())
    for item in ranking:
        assert item.feature_scores.keys() == weights.keys()
        assert all(0 <= score <= 1 for score in item.feature_scores.values())
        assert item.total_score == sum(weight * item.feature_scores[name] for name, weight in weights.items())
    assert ranking[0].feature_scores == {
        "relevance": 1.0, "price_fit": pytest.approx(0.8), "inventory": 1.0, "tag_match": 1.0,
    }
    price_only = RuleBasedProductRanker(RankingWeights(0, 1, 0, 0))
    assert [item.sku for item in price_only.rank(candidates, req)] == ["C", "A", "B"]


def test_price_fit_peaks_inside_budget():
    options = [product(sku, price=price) for sku, price in
               [("low", 2000), ("comfort", 7500), ("high", 9500), ("over", 10001)]]
    ranker = RuleBasedProductRanker()
    ranking = ranker.rank(options, requirements())
    assert [item.sku for item in ranking] == ["comfort", "low", "high", "over"]
    assert ranking[-1].feature_scores["price_fit"] == 0.0
    assert ranker.rank([product("free", price=0)], requirements(budget=0))[0].feature_scores["price_fit"] == 1.0


def test_stock_score_saturates_with_diminishing_returns():
    candidates = [product(str(stock), stock=stock) for stock in [0, 1, 4, 10, 100]]
    ranking = RuleBasedProductRanker().rank(candidates, requirements())
    scores = {item.sku: item.feature_scores["inventory"] for item in ranking}
    assert [item.sku for item in ranking] == ["10", "100", "4", "1", "0"]
    assert scores["10"] == scores["100"] == 1
    assert scores["1"] - scores["0"] > (scores["4"] - scores["1"]) / 3


def test_relevance_prefers_name_over_category_and_tags():
    candidates = [
        product("tag", name="sneaker", tags=["feature:shoe"]),
        product("category", name="sneaker", category="shoe"),
        product("name", name="shoe", tags=["feature:shoe"]),
        product("specification", name="sneaker", tags=["size:shoe", "color:shoe"]),
    ]
    ranking = RuleBasedProductRanker().rank(candidates, requirements(query="shoe shoe"))
    assert [item.sku for item in ranking] == ["name", "category", "tag", "specification"]
    assert [item.feature_scores["relevance"] for item in ranking] == [1, 0.65, 0.5, 0]


def test_missing_query_and_budget_are_neutral_and_ties_deterministic():
    candidates = [product("Z", price=1), product("A", price=20000)]
    req = requirements(query=None, budget=None)
    ranker = RuleBasedProductRanker()
    first = ranker.rank(candidates, req)
    assert first == ranker.rank(candidates, req) == ranker.rank(list(reversed(candidates)), req)
    assert [item.sku for item in first] == ["A", "Z"]
    assert first[0].feature_scores == {
        "relevance": 0.5, "price_fit": 0.5, "inventory": 1.0, "tag_match": 0.5,
    }
    assert [item.sku for item in candidates] == ["Z", "A"]
    assert ranker.rank([], req) == []


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1"):
        RankingWeights(relevance=0.6)
