"""Demo data seeding."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import OrderStatus
from app.core.time import utcnow
from app.repositories.models import OrderItemRow, OrderRow, ProductRow

DEMO_MERCHANT_ID = "m_demo"
SECOND_MERCHANT_ID = "m_second"
ROLE_MERCHANTS = {"buyer": None, "service_agent": DEMO_MERCHANT_ID, "operator": None}
MERCHANT_SEEDS = {
    DEMO_MERCHANT_ID: "CaseDesk demo shop",
    SECOND_MERCHANT_ID: "CaseDesk second shop",
}


class SeedConflictError(ValueError):
    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts
        super().__init__("seed ownership conflict: " + ", ".join(conflicts))


def demo_rows(now=None):
    now = now or utcnow()
    products = [
        ProductRow(
            merchant_id=DEMO_MERCHANT_ID,
            sku="SKU-HEADSET",
            name="降噪耳机 Pro",
            category="数码",
            price_cents=29900,
            inventory=42,
            tags="耳机,降噪",
            description="头戴式主动降噪耳机",
        ),
        ProductRow(
            merchant_id=DEMO_MERCHANT_ID,
            sku="SKU-MUG-CUSTOM",
            name="定制马克杯",
            category="家居",
            price_cents=3900,
            inventory=200,
            tags="定制,杯子",
            description="支持印字的定制杯，定制商品不适用7天无理由",
        ),
        ProductRow(
            merchant_id=DEMO_MERCHANT_ID,
            sku="SKU-TEE",
            name="纯棉T恤",
            category="服饰",
            price_cents=7900,
            inventory=0,
            tags="衣服,基础款",
            description="基础纯棉短袖",
        ),
    ]
    orders = [
        OrderRow(
            merchant_id=DEMO_MERCHANT_ID,
            order_id="ORD10001",
            user_id="u_demo",
            status=OrderStatus.SHIPPED.value,
            total_cents=29900,
            created_at=now - timedelta(days=3),
            paid_at=now - timedelta(days=3),
            shipped_at=now - timedelta(days=2),
            tracking_no="SF1234567890",
            receiver_city="上海",
        ),
        OrderRow(
            merchant_id=DEMO_MERCHANT_ID,
            order_id="ORD10002",
            user_id="u_demo",
            status=OrderStatus.DELIVERED.value,
            total_cents=3900,
            created_at=now - timedelta(days=20),
            paid_at=now - timedelta(days=20),
            shipped_at=now - timedelta(days=19),
            delivered_at=now - timedelta(days=12),
            tracking_no="YT998877",
            receiver_city="北京",
        ),
        OrderRow(
            merchant_id=DEMO_MERCHANT_ID,
            order_id="ORD10003",
            user_id="u_demo",
            status=OrderStatus.PAID.value,
            total_cents=7900,
            created_at=now - timedelta(hours=5),
            paid_at=now - timedelta(hours=5),
            receiver_city="杭州",
        ),
    ]
    items = [
        OrderItemRow(
            order_id="ORD10001",
            sku="SKU-HEADSET",
            name="降噪耳机 Pro",
            quantity=1,
            unit_price_cents=29900,
            customized=False,
        ),
        OrderItemRow(
            order_id="ORD10002",
            sku="SKU-MUG-CUSTOM",
            name="定制马克杯",
            quantity=1,
            unit_price_cents=3900,
            customized=True,
        ),
        OrderItemRow(
            order_id="ORD10003",
            sku="SKU-TEE",
            name="纯棉T恤",
            quantity=1,
            unit_price_cents=7900,
            customized=False,
        ),
    ]
    return products, orders, items


def merchant_comparison_rows():
    products, orders, items = [], [], []
    for merchant_id, suffix in ((DEMO_MERCHANT_ID, "DEMO"), (SECOND_MERCHANT_ID, "SECOND")):
        sku = "SKU-SHOE-" + suffix
        order_id = "ORD-SHOE-" + suffix
        name = "\u8f7b\u4fbf\u901a\u52e4\u978b"
        products.append(
            ProductRow(
                merchant_id=merchant_id,
                sku=sku,
                name=name,
                category="\u670d\u9970",
                price_cents=29900,
                inventory=20,
                tags="color:\u9ed1\u8272,size:42,feature:\u8f7b\u4fbf,feature:\u7f13\u9707",
                description="Synthetic comparison fixture, not a real product claim",
            )
        )
        orders.append(
            OrderRow(
                merchant_id=merchant_id,
                order_id=order_id,
                user_id="u_demo",
                status=OrderStatus.PAID.value,
                total_cents=29900,
            )
        )
        items.append(
            OrderItemRow(
                order_id=order_id,
                sku=sku,
                name=name,
                quantity=1,
                unit_price_cents=29900,
                customized=False,
            )
        )
    return products, orders, items


async def seed_conflicts(session: AsyncSession, products, orders, items) -> list[str]:
    conflicts = []
    planned_products = {row.sku: row for row in products}
    planned_orders = {row.order_id: row for row in orders}
    for product in products:
        existing_product = await session.get(ProductRow, product.sku)
        if existing_product is not None and existing_product.merchant_id != product.merchant_id:
            conflicts.append("products:" + product.sku)
    for order in orders:
        existing_order = await session.get(OrderRow, order.order_id)
        if existing_order is not None and (existing_order.merchant_id, existing_order.user_id) != (
            order.merchant_id,
            order.user_id,
        ):
            conflicts.append("orders:" + order.order_id)
    existing_items = list(
        await session.scalars(select(OrderItemRow).where(OrderItemRow.order_id.in_(planned_orders)))
    )
    seen = set()
    for item in existing_items:
        key = (item.order_id, item.sku)
        product = await session.get(ProductRow, item.sku)
        if product is None:
            product = planned_products.get(item.sku)
        if (
            key in seen
            or product is None
            or product.merchant_id != planned_orders[item.order_id].merchant_id
        ):
            conflicts.append("order_items:" + item.order_id + "/" + item.sku)
        seen.add(key)
    return sorted(set(conflicts))


async def insert_missing_demo_rows(session: AsyncSession, products, orders, items):
    created, skipped = [], []
    for row, model, key, label in [(p, ProductRow, p.sku, "products") for p in products] + [
        (o, OrderRow, o.order_id, "orders") for o in orders
    ]:
        identifier = label + ":" + key
        if await session.get(model, key) is None:
            session.add(row)
            created.append(identifier)
        else:
            skipped.append(identifier)
    await session.flush()
    existing_items = {
        (order_id, sku)
        for order_id, sku in (
            await session.execute(select(OrderItemRow.order_id, OrderItemRow.sku))
        ).all()
    }
    for item in items:
        identifier = "order_items:" + item.order_id + "/" + item.sku
        if (item.order_id, item.sku) not in existing_items:
            session.add(item)
            existing_items.add((item.order_id, item.sku))
            created.append(identifier)
        else:
            skipped.append(identifier)
    await session.flush()
    return created, skipped


async def seed_demo_data(session: AsyncSession) -> None:
    """Preserve existing data; ownership conflicts abort this unit of work."""
    products, orders, items = demo_rows()
    conflicts = await seed_conflicts(session, products, orders, items)
    if conflicts:
        raise SeedConflictError(conflicts)
    await insert_missing_demo_rows(session, products, orders, items)
