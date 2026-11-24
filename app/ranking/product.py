"""Explainable ranking for products already admitted by catalog recall."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from math import isclose

from app.agents.product_requirements import ProductRequirements
from app.retrieval.tokenizer import tokenize
from app.schemas.order import Product

NEUTRAL_SCORE = 0.5
PRICE_COMFORT_RATIO = 0.75
SUFFICIENT_STOCK = 10
NAME_HIT_SCORE = 1.0
CATEGORY_HIT_SCORE = 0.65
TAG_HIT_SCORE = 0.5


@dataclass(frozen=True)
class RankingWeights:
    # All four features are normalized to [0, 1]. Weights sum to 1 so the
    # unrounded weighted sum is on the same scale.
    relevance: float = 0.40
    price_fit: float = 0.25
    inventory: float = 0.10
    tag_match: float = 0.25

    def __post_init__(self) -> None:
        values = asdict(self).values()
        if not all(0 <= value <= 1 for value in values) or not isclose(sum(values), 1.0):
            raise ValueError("ranking weights must be in [0, 1] and sum to 1")


@dataclass(frozen=True)
class RankedProduct:
    sku: str
    total_score: float
    feature_scores: dict[str, float]


class ProductRanker(ABC):
    @abstractmethod
    def rank(
        self, candidates: Sequence[Product], requirements: ProductRequirements
    ) -> list[RankedProduct]:
        """Return one score per input SKU in descending priority order."""


class RuleBasedProductRanker(ProductRanker):
    def __init__(self, weights: RankingWeights = RankingWeights()) -> None:
        self._weights = weights

    def rank(
        self, candidates: Sequence[Product], requirements: ProductRequirements
    ) -> list[RankedProduct]:
        query_tokens = sorted(set(tokenize(requirements.task_query or "")))
        budget = requirements.constraints.get("max_price_cents")
        requested_tags = {f"feature:{tag}" for tag in requirements.evidence_tags}
        weights = asdict(self._weights)
        ranked = []
        for product in candidates:
            tags = set(product.tags)
            features = {
                "relevance": _relevance(product, query_tokens),
                "price_fit": _price_fit(product.price_cents, budget),
                "inventory": (min(max(product.inventory, 0), SUFFICIENT_STOCK) / SUFFICIENT_STOCK) ** 0.5,
                "tag_match": len(tags & requested_tags) / len(requested_tags)
                if requested_tags else NEUTRAL_SCORE,
            }
            total = sum(weight * features[name] for name, weight in weights.items())
            ranked.append(RankedProduct(product.sku, total, features))
        return sorted(ranked, key=lambda item: (-item.total_score, item.sku))


def _relevance(product: Product, query_tokens: list[str]) -> float:
    if not query_tokens:
        return NEUTRAL_SCORE
    name = set(tokenize(product.name))
    category = set(tokenize(product.category))
    tags = {
        token
        for tag in product.tags
        if not tag.startswith(("color:", "size:"))
        for token in tokenize(tag.removeprefix("feature:"))
    }
    # Each distinct query token gets credit only for its strongest matching field.
    return sum(
        NAME_HIT_SCORE if token in name else
        CATEGORY_HIT_SCORE if token in category else
        TAG_HIT_SCORE if token in tags else 0.0
        for token in query_tokens
    ) / len(query_tokens)


def _price_fit(price: int, budget: int | None) -> float:
    if budget is None:
        return NEUTRAL_SCORE
    if not 0 <= price <= budget:
        return 0.0
    if budget == 0:
        return 1.0
    # Triangular fit with its peak at 75% of budget and zero at both endpoints.
    ratio = price / budget
    if ratio <= PRICE_COMFORT_RATIO:
        return ratio / PRICE_COMFORT_RATIO
    return (1 - ratio) / (1 - PRICE_COMFORT_RATIO)
