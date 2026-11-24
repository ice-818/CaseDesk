"""Central SQL predicates. Platform catalog visibility is not buyer ownership."""

from sqlalchemy import and_, true
from sqlalchemy.sql.elements import ColumnElement

from app.core.data_scope import DataScope
from app.core.exceptions import ForbiddenError
from app.repositories.models import OrderRow, ProductRow, SessionRow


def product_condition(scope: DataScope) -> ColumnElement[bool]:
    scope.assert_current()
    return ProductRow.merchant_id == scope.merchant_id if scope.mode == "shop" else true()


def order_condition(scope: DataScope) -> ColumnElement[bool]:
    scope.assert_current()
    if scope.role == "buyer":
        return OrderRow.user_id == scope.actor_user_id
    if scope.role == "service_agent":
        return OrderRow.merchant_id == scope.merchant_id
    return true()


def session_condition(scope: DataScope) -> ColumnElement[bool]:
    scope.assert_current()
    if scope.role == "buyer":
        return SessionRow.user_id == scope.actor_user_id
    if scope.role == "service_agent":
        return and_(SessionRow.channel == "shop", SessionRow.merchant_id == scope.merchant_id)
    return true()


def merchant_for_new_resource(scope: DataScope) -> str:
    scope.assert_current()
    if scope.mode != "shop" or not scope.merchant_id:
        raise ForbiddenError("a trusted shop scope is required for new shop resources")
    return scope.merchant_id


def require_operator(scope: DataScope) -> None:
    scope.assert_current()
    if scope.role != "operator":
        raise ForbiddenError()
