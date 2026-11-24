"""Scope-enforced order, product and after-sale storage."""

import json

from sqlalchemy import func, literal, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.data_scope import require_data_scope
from app.core.enums import AftersaleStatus, OrderStatus
from app.core.error_codes import ErrorCode
from app.core.exceptions import (
    AppError,
    DatabaseError,
    ForbiddenError,
    InvalidRequestError,
    ResourceNotFoundError,
)
from app.core.ids import new_id
from app.repositories.models import AuditLogRow, OrderRow, ProductRow, RefundRow, ReturnRow
from app.repositories.scope_policy import order_condition, product_condition
from app.schemas.order import Order, OrderItem, Product

# Orders in these states can open a new after-sale request.
AFTERSALE_ORDER_STATUSES = {
    OrderStatus.PAID,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
}
_ACTIVE_AFTERSALE_STATUSES = (
    AftersaleStatus.REQUESTED.value,
    AftersaleStatus.APPROVED.value,
)
_AFTERSALE_TRANSITIONS: dict[AftersaleStatus, set[AftersaleStatus]] = {
    AftersaleStatus.REQUESTED: {AftersaleStatus.APPROVED, AftersaleStatus.REJECTED},
    AftersaleStatus.APPROVED: {AftersaleStatus.COMPLETED},
    AftersaleStatus.REJECTED: set(),
    AftersaleStatus.COMPLETED: set(),
}


def escape_like(term: str) -> str:
    """Escape LIKE wildcards so user input cannot broaden a search."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.scope = require_data_scope()

    async def _order_row(self, order_id: str, *, write: bool = False) -> OrderRow | None:
        condition = order_condition(self.scope)
        try:
            row = await self._session.scalar(
                select(OrderRow).options(selectinload(OrderRow.items))
                .where(OrderRow.order_id == order_id, condition)
                .execution_options(populate_existing=True)
            )
        except Exception as exc:
            raise DatabaseError("failed to load order") from exc
        if row is None and write:
            raise ForbiddenError()
        return row

    async def get_order(self, order_id: str) -> Order | None:
        row = await self._order_row(order_id)
        if row is None:
            return None
        return _to_order(row)

    async def get_visible_order(self, order_id: str, user_id: str | None = None, *, write: bool = False) -> Order:
        """Legacy user_id is ignored: the captured trusted scope authorizes access."""
        row = await self._order_row(order_id, write=write)
        if row is None:
            raise ResourceNotFoundError()
        return _to_order(row)

    async def list_orders_for_user(self, user_id: str, *, limit: int = 10) -> list[Order]:
        result = await self._session.execute(
            select(OrderRow)
            .options(selectinload(OrderRow.items))
            .where(OrderRow.user_id == user_id, order_condition(self.scope))
            .order_by(OrderRow.created_at.desc())
            .limit(limit)
        )
        return [_to_order(row) for row in result.scalars().all()]

    async def update_status(self, order_id: str, status: OrderStatus) -> None:
        order = await self._order_row(order_id, write=True)
        assert order is not None
        order.status = status.value

    async def _has_active_document(
        self,
        order_id: str,
        model: type[RefundRow] | type[ReturnRow],
    ) -> bool:
        """Whether an active (requested/approved) after-sale row exists."""
        count = await self._session.scalar(
            select(func.count())
            .select_from(model)
            .where(
                model.order_id == order_id,
                model.status.in_(_ACTIVE_AFTERSALE_STATUSES),
            )
        )
        return int(count or 0) > 0

    async def _completed_refund_total(self, order_id: str) -> int:
        total = await self._session.scalar(
            select(func.coalesce(func.sum(RefundRow.amount_cents), 0)).where(
                RefundRow.order_id == order_id,
                RefundRow.status == AftersaleStatus.COMPLETED.value,
            )
        )
        return int(total or 0)

    def _assert_aftersale_allowed(self, order: Order) -> None:
        if order.status not in AFTERSALE_ORDER_STATUSES:
            raise InvalidRequestError(
                f"order in status {order.status.value} cannot open an after-sale request"
            )

    def _raise_active_conflict(self, exc: IntegrityError) -> None:
        """Map a concurrent duplicate insert to a deterministic 409."""
        raise AppError(
            "after-sale request conflicts with an existing active document",
            error_code=ErrorCode.IDEMPOTENCY_CONFLICT,
            http_status=409,
        ) from exc

    async def _lock_order_for_write(self, order_id: str) -> None:
        """Serialize after-sale writers on one order before any read snapshot.

        SQLite has no row locks, so an explicit BEGIN IMMEDIATE takes the
        database write lock up front. Other backends lock the parent order row
        FOR UPDATE. A second writer blocks until the first commits, then reads
        fresh state and is rejected by the active-document guard instead of
        acting on a stale snapshot (which previously let a refund and a return
        become active on the same order).
        """
        if self._session.get_bind().dialect.name == "sqlite":
            # If the session already owns a transaction we cannot open another
            # one; the lock is only meaningful at the start of a fresh request.
            if not self._session.in_transaction():
                await self._session.execute(text("BEGIN IMMEDIATE"))
        else:
            await self._session.execute(
                select(OrderRow.order_id)
                .where(OrderRow.order_id == order_id)
                .with_for_update()
            )

    async def create_refund(
        self,
        *,
        order_id: str,
        reason: str,
        trace_id: str,
    ) -> dict:
        """Insert refund + mark order refunding in the same Unit of Work.

        The request session commits in ChatService; this method only flushes.
        If either write fails, the outer rollback drops both. Business rules
        reject a duplicate active document, a conflicting return, an illegal
        order status and any amount that would exceed the order total.
        """
        await self._lock_order_for_write(order_id)
        order = await self.get_visible_order(order_id, write=True)
        if await self._has_active_document(order.order_id, RefundRow):
            raise InvalidRequestError("an active refund already exists for this order")
        if await self._has_active_document(order.order_id, ReturnRow):
            raise InvalidRequestError("an active return exists; resolve it before requesting a refund")
        self._assert_aftersale_allowed(order)
        amount_cents = order.total_cents
        already_refunded = await self._completed_refund_total(order.order_id)
        if already_refunded + amount_cents > order.total_cents:
            raise InvalidRequestError("refund amount exceeds the order total")
        refund_id = new_id("rf_")[:16]
        try:
            self._session.add(
                RefundRow(
                    refund_id=refund_id,
                    order_id=order.order_id,
                    status=AftersaleStatus.REQUESTED.value,
                    amount_cents=amount_cents,
                    reason=reason,
                )
            )
            await self.update_status(order.order_id, OrderStatus.REFUNDING)
            await self.write_audit(trace_id, "create_refund", order.order_id)
            await self._session.flush()
        except IntegrityError as exc:
            self._raise_active_conflict(exc)
        except AppError:
            raise
        except Exception as exc:
            raise DatabaseError("failed to create refund") from exc
        return {
            "refund_id": refund_id,
            "status": AftersaleStatus.REQUESTED.value,
            "order_id": order.order_id,
        }

    async def create_return(
        self,
        *,
        order_id: str,
        reason: str,
        trace_id: str,
    ) -> dict:
        await self._lock_order_for_write(order_id)
        order = await self.get_visible_order(order_id, write=True)
        if await self._has_active_document(order.order_id, ReturnRow):
            raise InvalidRequestError("an active return already exists for this order")
        if await self._has_active_document(order.order_id, RefundRow):
            raise InvalidRequestError("an active refund exists; resolve it before requesting a return")
        self._assert_aftersale_allowed(order)
        return_id = new_id("rt_")[:16]
        try:
            self._session.add(
                ReturnRow(
                    return_id=return_id,
                    order_id=order.order_id,
                    status=AftersaleStatus.REQUESTED.value,
                    reason=reason,
                )
            )
            await self.write_audit(trace_id, "create_return", order.order_id)
            await self._session.flush()
        except IntegrityError as exc:
            self._raise_active_conflict(exc)
        except AppError:
            raise
        except Exception as exc:
            raise DatabaseError("failed to create return") from exc
        return {
            "return_id": return_id,
            "order_id": order.order_id,
            "status": AftersaleStatus.REQUESTED.value,
        }

    async def cancel_order(self, order_id: str, *, trace_id: str) -> dict:
        order = await self.get_visible_order(order_id, write=True)
        if order.status not in {OrderStatus.CREATED, OrderStatus.PAID}:
            raise InvalidRequestError("only unpaid or paid-unshipped orders can be cancelled")
        try:
            await self.update_status(order.order_id, OrderStatus.CANCELLED)
            await self.write_audit(trace_id, "cancel_order", order.order_id)
            await self._session.flush()
        except AppError:
            raise
        except Exception as exc:
            raise DatabaseError("failed to cancel order") from exc
        return {"order_id": order.order_id, "status": "cancelled"}

    async def write_audit(self, trace_id: str, action: str, order_id: str) -> None:
        order = await self.get_visible_order(order_id, write=True)
        payload = json.dumps({"order_id": order.order_id, "merchant_id": order.merchant_id, "actor_user_id": self.scope.actor_user_id}, ensure_ascii=False)
        self._session.add(AuditLogRow(trace_id=trace_id, action=action, payload=payload))

    async def get_product(self, sku: str) -> Product | None:
        row = await self._session.scalar(select(ProductRow).where(ProductRow.sku == sku, product_condition(self.scope)))
        if row is None:
            return None
        return Product(
            sku=row.sku,
            merchant_id=row.merchant_id,
            name=row.name,
            category=row.category,
            price_cents=row.price_cents,
            inventory=row.inventory,
            tags=[tag for tag in row.tags.split(",") if tag],
            description=row.description,
        )

    async def search_products(self, query: str, *, limit: int = 5) -> list[Product]:
        pattern = f"%{escape_like(query)}%"
        result = await self._session.execute(
            select(ProductRow)
            .where(product_condition(self.scope))
            .where(
                or_(
                    ProductRow.name.ilike(pattern),
                    ProductRow.category.ilike(pattern),
                    ProductRow.tags.ilike(pattern),
                )
            )
            .limit(limit)
        )
        return [
            Product(
                sku=row.sku,
                merchant_id=row.merchant_id,
                name=row.name,
                category=row.category,
                price_cents=row.price_cents,
                inventory=row.inventory,
                tags=[tag for tag in row.tags.split(",") if tag],
                description=row.description,
            )
            for row in result.scalars().all()
        ]

    async def find_matching_products(
        self,
        query: str = "",
        *,
        category: str | None = None,
        min_price_cents: int | None = None,
        max_price_cents: int | None = None,
        color: str | None = None,
        colors: list[str] | None = None,
        size: str | None = None,
        limit: int = 5,
        offset: int = 0,
    ) -> list[Product]:
        """Filter before LIMIT; one SKU is one sellable color/size variant.

        Explicit catalog tags color:黑色,size:42 are required for constrained
        queries. Do not infer specifications from names, prose or another SKU.
        """
        statement = select(ProductRow).where(ProductRow.inventory > 0, product_condition(self.scope))
        if query:
            pattern = f"%{escape_like(query)}%"
            statement = statement.where(
                or_(
                    ProductRow.name.ilike(pattern, escape="\\"),
                    ProductRow.category.ilike(pattern, escape="\\"),
                    ProductRow.tags.ilike(pattern, escape="\\"),
                )
            )
        if category:
            statement = statement.where(ProductRow.category == category)
        if min_price_cents is not None:
            statement = statement.where(ProductRow.price_cents >= min_price_cents)
        if max_price_cents is not None:
            statement = statement.where(ProductRow.price_cents <= max_price_cents)
        tags = literal(",") + ProductRow.tags + literal(",")
        if colors is not None:
            statement = statement.where(
                or_(
                    *[tags.like(f"%,color:{escape_like(value)},%", escape="\\") for value in colors]
                )
            )
        for key, value in (("color", color), ("size", size)):
            if value is not None:
                statement = statement.where(
                    tags.like(f"%,{key}:{escape_like(value)},%", escape="\\")
                )
        # SKU stabilizes page boundaries; the application ranks recalled products.
        result = await self._session.execute(
            statement.order_by(ProductRow.sku).offset(offset).limit(limit)
        )
        return [
            Product(
                sku=row.sku,
                merchant_id=row.merchant_id,
                name=row.name,
                category=row.category,
                price_cents=row.price_cents,
                inventory=row.inventory,
                tags=[tag for tag in row.tags.split(",") if tag],
                description=row.description,
            )
            for row in result.scalars()
        ]

    async def get_refunds(self, order_id: str) -> list[RefundRow]:
        await self.get_visible_order(order_id)
        result = await self._session.execute(
            select(RefundRow).join(OrderRow, RefundRow.order_id == OrderRow.order_id)
            .where(RefundRow.order_id == order_id, order_condition(self.scope))
        )
        return list(result.scalars().all())

    async def _get_scoped_refund(self, refund_id: str) -> RefundRow:
        row = await self._session.scalar(
            select(RefundRow)
            .join(OrderRow, RefundRow.order_id == OrderRow.order_id)
            .where(RefundRow.refund_id == refund_id, order_condition(self.scope))
        )
        if row is None:
            raise ResourceNotFoundError()
        return row

    async def _get_scoped_return(self, return_id: str) -> ReturnRow:
        row = await self._session.scalar(
            select(ReturnRow)
            .join(OrderRow, ReturnRow.order_id == OrderRow.order_id)
            .where(ReturnRow.return_id == return_id, order_condition(self.scope))
        )
        if row is None:
            raise ResourceNotFoundError()
        return row

    @staticmethod
    def _restore_order_status(order: OrderRow) -> OrderStatus:
        """Best-effort pre-refund status used when a refund is rejected."""
        if order.delivered_at is not None:
            return OrderStatus.DELIVERED
        if order.shipped_at is not None:
            return OrderStatus.SHIPPED
        return OrderStatus.PAID

    async def transition_refund(
        self,
        *,
        refund_id: str,
        target: AftersaleStatus,
        trace_id: str,
    ) -> dict:
        refund = await self._get_scoped_refund(refund_id)
        current = AftersaleStatus(refund.status)
        if target not in _AFTERSALE_TRANSITIONS[current]:
            raise InvalidRequestError(
                f"refund cannot transition from {current.value} to {target.value}"
            )
        order = await self._order_row(refund.order_id, write=True)
        assert order is not None
        try:
            refund.status = target.value
            if target is AftersaleStatus.COMPLETED:
                order.status = OrderStatus.REFUNDED.value
            elif target is AftersaleStatus.REJECTED:
                order.status = self._restore_order_status(order).value
            await self.write_audit(trace_id, f"refund_{target.value}", refund.order_id)
            await self._session.flush()
        except AppError:
            raise
        except Exception as exc:
            raise DatabaseError("failed to transition refund") from exc
        return {
            "refund_id": refund.refund_id,
            "status": target.value,
            "order_id": refund.order_id,
            "order_status": order.status,
        }

    async def transition_return(
        self,
        *,
        return_id: str,
        target: AftersaleStatus,
        trace_id: str,
    ) -> dict:
        ret = await self._get_scoped_return(return_id)
        current = AftersaleStatus(ret.status)
        if target not in _AFTERSALE_TRANSITIONS[current]:
            raise InvalidRequestError(
                f"return cannot transition from {current.value} to {target.value}"
            )
        order = await self._order_row(ret.order_id, write=True)
        assert order is not None
        try:
            ret.status = target.value
            if target is AftersaleStatus.COMPLETED:
                # Return received: close the loop with an automatic full refund.
                refund_id = new_id("rf_")[:16]
                self._session.add(
                    RefundRow(
                        refund_id=refund_id,
                        order_id=order.order_id,
                        status=AftersaleStatus.COMPLETED.value,
                        amount_cents=order.total_cents,
                        reason="return received, automatic refund",
                    )
                )
                order.status = OrderStatus.REFUNDED.value
            await self.write_audit(trace_id, f"return_{target.value}", ret.order_id)
            await self._session.flush()
        except AppError:
            raise
        except Exception as exc:
            raise DatabaseError("failed to transition return") from exc
        return {
            "return_id": ret.return_id,
            "status": target.value,
            "order_id": ret.order_id,
            "order_status": order.status,
        }


def _to_order(row: OrderRow) -> Order:
    return Order(
        order_id=row.order_id,
        merchant_id=row.merchant_id,
        user_id=row.user_id,
        status=OrderStatus(row.status),
        items=[
            OrderItem(
                sku=item.sku,
                name=item.name,
                quantity=item.quantity,
                unit_price_cents=item.unit_price_cents,
                customized=item.customized,
            )
            for item in row.items
        ],
        total_cents=row.total_cents,
        created_at=row.created_at,
        paid_at=row.paid_at,
        shipped_at=row.shipped_at,
        delivered_at=row.delivered_at,
        tracking_no=row.tracking_no,
        receiver_city=row.receiver_city,
    )
