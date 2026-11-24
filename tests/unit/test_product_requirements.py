import pytest

from app.agents.product_requirements import extract_requirements


@pytest.mark.parametrize(
    "query",
    [
        "推荐300元以内黑色42码通勤鞋",
        "预算300元，黑色，42码",
        "找鞋，不超过300元，黑色42码",
    ],
)
def test_extract_explicit_requirements(query):
    result = extract_requirements(query)
    assert result.clarification is None
    assert result.constraints == {"max_price_cents": 30000, "color": "黑色", "size": "42"}


@pytest.mark.parametrize(
    "query",
    [
        "300元左右的鞋",
        "200到300元以内的鞋",
        "不要黑色42码的鞋",
        "黑色或白色42码",
        "42码或43码",
        "300块以下的鞋",
        "深蓝色42码",
        "42码半",
    ],
)
def test_ambiguous_or_unsupported_constraints_need_clarification(query):
    assert extract_requirements(query).clarification
