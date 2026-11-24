import pytest

from app.core.exceptions import ForbiddenError
from app.repositories.scope_policy import (
    merchant_for_new_resource,
    order_condition,
    product_condition,
    session_condition,
)
from tests.merchant_support import actor


def test_role_predicates_and_forced_new_merchant():
    with actor() as scope:
        assert "merchant_id" in str(order_condition(scope))
        assert "user_id" not in str(order_condition(scope))
        assert "channel" in str(session_condition(scope))
        assert merchant_for_new_resource(scope) == "m_demo"
    with actor("u_demo", "buyer", None) as scope:
        assert "user_id" in str(order_condition(scope))
        assert str(product_condition(scope)) == "true"
        with pytest.raises(ForbiddenError):
            merchant_for_new_resource(scope)


def test_old_scope_cannot_be_reused_in_new_shop():
    with actor() as old:
        pass
    with actor("b", merchant="m_second"):
        with pytest.raises(ForbiddenError):
            order_condition(old)
