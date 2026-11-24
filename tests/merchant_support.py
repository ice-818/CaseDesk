from contextlib import contextmanager

from app.config.settings import Settings
from app.core.data_scope import bind_data_scope, scope_from_identity
from app.repositories.db import create_engine, create_session_factory
from app.repositories.models import MessageRow, OrderRow, ProductRow, SessionRow
from app.schemas.auth import AuthIdentity
from tests.auth_support import prepare_auth_database


@contextmanager
def actor(uid="agent", role="service_agent", merchant="m_demo"):
    scope = scope_from_identity(AuthIdentity(uid, role, merchant_id=merchant))
    with bind_data_scope(scope):
        yield scope


async def database(path):
    prepare_auth_database(path)
    engine = create_engine(Settings(database_url=f"sqlite+aiosqlite:///{path}"))
    factory = create_session_factory(engine)
    async with factory.begin() as db:
        for prefix, merchant in (("a", "m_demo"), ("b", "m_second")):
            db.add(
                ProductRow(
                    sku=prefix + "-shoe",
                    name="shoe",
                    category="shoe",
                    price_cents=100,
                    inventory=2,
                    merchant_id=merchant,
                )
            )
            for uid in ("u_demo", "other"):
                db.add(
                    OrderRow(
                        order_id=prefix + uid,
                        user_id=uid,
                        status="paid",
                        total_cents=100,
                        merchant_id=merchant,
                    )
                )
                db.add(
                    SessionRow(
                        session_id=prefix + uid, user_id=uid, channel="shop", merchant_id=merchant
                    )
                )
        db.add(SessionRow(session_id="platform", user_id="u_demo"))
        await db.flush()
        db.add(MessageRow(session_id="aother", role="assistant", content="private"))
    return engine, factory
