"""Project verified shopping state onto the public chat response."""

from typing import Any

from app.agents.product_requirements import ProductRequirements


def build_shopping_view(
    loop: dict[str, Any] | None, requirements: ProductRequirements | None
) -> dict[str, Any] | None:
    if not requirements or not requirements.active or not loop or not loop.get("verified"):
        return None

    verified = loop["verified"]
    skus = {product["sku"] for product in verified}
    positions = {sku: index for index, sku in enumerate(loop.get("display_skus", []), start=1)}
    rankings = {item["sku"]: item for item in loop.get("verified_ranking", [])}
    products = []
    for product in verified:
        sku = product["sku"]
        ranking = rankings.get(sku, {})
        products.append({
            "sku": sku,
            "name": product["name"],
            "category": product["category"],
            "price_cents": product.get("price_cents"),
            "inventory": product.get("inventory"),
            "tags": list(product.get("tags") or []),
            # An interrupted run may have verified a SKU without displaying it yet.
            "position": positions.get(sku),
            "feature_scores": dict(ranking.get("feature_scores") or {}),
            "total_score": ranking.get("total_score"),
        })

    constraints = requirements.constraints
    color = constraints.get("color") or " / ".join(constraints.get("colors") or []) or None
    selected = loop.get("selected_sku")
    return {
        "view": loop.get("view", "list"),
        "display_index": loop.get("display_index", 0),
        "stop_reason": loop.get("stop_reason"),
        "notice": loop.get("navigation_notice"),
        "selected_sku": selected if selected in skus else None,
        "compare_skus": [sku for sku in loop.get("compare_skus", []) if sku in skus],
        "conditions": {
            "max_price_cents": constraints.get("max_price_cents"),
            "color": color,
            "size": constraints.get("size"),
            "evidence_tags": list(requirements.evidence_tags),
        },
        "products": products,
    }
