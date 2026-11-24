"""Conservative, deterministic extraction for explicit single-turn constraints.

Unsupported or conflicting price/color/size expressions require clarification.
This is deliberately not an unrestricted natural-language understanding system.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.agents.navigation import parse_navigation


class ProductRequirements(BaseModel):
    constraints: dict[str, Any] = Field(default_factory=dict)
    clarification: str | None = None
    source_query: str
    task_query: str | None = None
    active: bool = False
    evidence_tags: list[str] = Field(default_factory=list)


def extract_requirements(query: str) -> ProductRequirements:
    constraints: dict[str, Any] = {}
    problems = []
    if re.search(r"\d+(?:\.\d+)?\s*(?:元)?\s*[-~～至到]\s*\d", query):
        problems.append("价格或尺码区间需确认，当前请提供明确上限和单一尺码")
    if re.search(r"(?:深|浅|藏|酒|墨|乳|奶|银|金).{0,2}色|码半", query):
        problems.append("颜色或尺码表述暂不支持，请明确确认规格")
    remaining = query
    prices = []
    patterns = [
        r"(?:预算|不超过|最多|至多|最高|不高于)\s*(\d+(?:\.\d{1,2})?)\s*元?",
        r"(\d+(?:\.\d{1,2})?)\s*元?\s*(?:以内|以下)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, remaining):
            prices.append(int(Decimal(match[1]) * 100))
        remaining = re.sub(pattern, "", remaining)
    if prices:
        if len(set(prices)) != 1:
            problems.append("预算表述不一致")
        else:
            constraints["max_price_cents"] = prices[0]
    # Avoid interpreting an unsupported range, approximation or lower bound
    # as an unconstrained query, or accepting a partially parsed negation.
    if re.search(
        r"\d\s*(?:元|块)|预算|低于|超过|高于|至少|左右|大概|大约|到\s*\d|[-~～至]\s*\d", remaining
    ):
        problems.append("请用明确的最高预算，例如300元以内")
    colors = set(re.findall(r"(?:黑|白|红|蓝|绿|黄|灰|粉|紫|棕|米)色", query))
    if len(colors) > 1 or re.search(r"(?:不要|不选|除了|非|不是).{0,4}色", query):
        problems.append("请确认唯一的目标颜色")
    elif colors:
        constraints["color"] = colors.pop()
    elif "色" in query:
        problems.append("请确认目标颜色，当前支持黑色、白色等明确色名")
    sizes = set(re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*码", query))
    if len(sizes) > 1 or re.search(r"(?:不要|不选|非|不是).{0,4}码", query):
        problems.append("请确认唯一的目标尺码")
    elif sizes:
        constraints["size"] = sizes.pop()
    elif "码" in query:
        problems.append("请用明确的尺码，例如42码")
    return ProductRequirements(
        constraints=constraints,
        source_query=query,
        clarification="；".join(problems) if problems else None,
    )


def protect_arguments(arguments: dict, requirements: ProductRequirements) -> dict:
    """User-derived constraints take precedence over model-generated arguments."""
    if requirements.active:
        arguments = {
            key: value
            for key, value in arguments.items()
            if key
            not in {
                "min_price_cents",
                "max_price_cents",
                "color",
                "colors",
                "size",
                "category",
            }
        }
        arguments["query"] = requirements.task_query
    return {**arguments, **requirements.constraints}


def matches_requirements(product: dict, constraints: dict) -> bool:
    price = product.get("price_cents")
    if "max_price_cents" in constraints and (
        type(price) is not int or price > constraints["max_price_cents"]
    ):
        return False
    if "min_price_cents" in constraints and (
        type(price) is not int or price < constraints["min_price_cents"]
    ):
        return False
    tags = product.get("tags") or []
    if "colors" in constraints and not any(
        f"color:{color}" in tags for color in constraints["colors"]
    ):
        return False
    return all(
        f"{key}:{constraints[key]}" in tags for key in ("color", "size") if key in constraints
    )


def update_requirements(
    query: str, previous: ProductRequirements | None = None
) -> ProductRequirements:
    """Merge explicit edits in the current shopping task, never infer old data from chat text."""
    if parse_navigation(query).command != "unknown":
        if previous and previous.active:
            return previous.model_copy(deep=True, update={"source_query": query})
        return ProductRequirements(source_query=query)
    if re.search(r"退货|退款|售后|订单|物流|发货|保修|取消订单|开胶", query):
        return extract_requirements(query)
    keywords = {
        "跑步鞋": ("跑步鞋", "跑鞋"), "通勤鞋": ("通勤鞋",),
        "运动鞋": ("运动鞋",), "耳机": ("耳机",), "帽子": ("帽子",),
        "T恤": ("t恤", "体恤"), "鞋": ("鞋",), "杯子": ("杯子",),
    }
    keyword = next((word for word, aliases in keywords.items() if any(a in query.lower() for a in aliases)), None)
    previous = previous if previous and previous.active else None
    if previous is None and not re.search(r"推荐|买|找|选|看看|有没有|想要", query):
        return extract_requirements(query)
    new_task = keyword is not None and (
        previous is None
        or keyword != previous.task_query
        or bool(re.search(r"重新|再买|另外", query))
    )
    removals = {
        "colors": r"(?:颜色不限|不限颜色)",
        "max_price_cents": r"(?:预算不限|不限预算)",
        "size": r"(?:尺码不限|不限尺码)",
    }
    normalized = re.sub(r"预算(?:改成|改为|调整为)", "预算", query)
    for pattern in removals.values():
        normalized = re.sub(pattern, "", normalized)
    current = extract_requirements(normalized).model_copy(update={"source_query": query})
    is_edit = bool(
        current.constraints
        or current.clarification
        or "防滑" in query
        or any(re.search(pattern, query) for pattern in removals.values())
    )
    if keyword is None and (previous is None or not is_edit):
        return current  # unrelated turn ends the shopping task conservatively
    current.active = True
    current.task_query = keyword or (previous.task_query if previous else None)
    current.evidence_tags = (
        ["防滑"]
        if "防滑" in query
        else (list(previous.evidence_tags) if previous and not new_task else [])
    )
    if re.search(r"不用防滑|不要求防滑|取消防滑|不要防滑", query):
        current.evidence_tags = []
    base = dict(previous.constraints) if previous and not new_task else {}
    if current.clarification:
        current.constraints = base
        return current
    for field, pattern in removals.items():
        if re.search(pattern, query):
            base.pop(field, None)
            if field == "colors":
                base.pop("color", None)
    if "color" in current.constraints:
        color = current.constraints["color"]
        if previous and not new_task and re.search(r"也可以|也行|也接受|加上", query):
            colors = base.pop("colors", [])
            old = base.pop("color", None)
            current.constraints.pop("color")
            base["colors"] = sorted(set(colors + ([old] if old else []) + [color]))
        else:
            base.pop("colors", None)
            base.pop("color", None)
    base.update(current.constraints)
    current.constraints = base
    return current
