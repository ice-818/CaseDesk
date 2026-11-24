"""Async engine, session factory and schema initialisation."""

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import Connection, event, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config.settings import Settings
from app.repositories.models import Base

# The single schema revision this build is allowed to run against.
EXPECTED_REVISION = "0010"


def create_engine(settings: Settings) -> AsyncEngine:
    connect_args: dict[str, object] = {}
    kwargs: dict[str, object] = {
        "echo": False,
        "hide_parameters": True,
        "pool_pre_ping": True,
        "connect_args": connect_args,
    }
    if settings.use_sqlite:
        Path("data/local").mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False
        if ":memory:" in settings.database_url:
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
        kwargs["pool_timeout"] = settings.db_pool_timeout
        kwargs["pool_recycle"] = 1800
    engine = create_async_engine(settings.database_url, **kwargs)
    if settings.use_sqlite:
        @event.listens_for(engine.sync_engine, "connect")
        def enable_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def _column_names(connection: Connection, table_name: str) -> set[str]:
    return {str(column["name"]) for column in inspect(connection).get_columns(table_name)}


async def init_schema(engine: AsyncEngine) -> None:
    """Check migration-owned schema; never create or stamp a live database.

    Merchant ownership and its backfill go through 0009; the after-sale guard
    goes through 0010. Creating current metadata on an unversioned/older
    database would bypass those guarantees.
    """
    async with engine.connect() as connection:
        names = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names()))
        required = set(Base.metadata.tables)
        if "alembic_version" not in names or not required.issubset(names):
            raise RuntimeError(
                f"database migrations required: upgrade explicitly to {EXPECTED_REVISION}"
            )
        revisions = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
        if revisions != [EXPECTED_REVISION]:
            raise RuntimeError(
                f"database migrations required: expected revision {EXPECTED_REVISION}"
            )
        for table_name in ("products", "orders", "sessions", "users"):
            columns = await connection.run_sync(_column_names, table_name)
            expected = {"merchant_id", "channel"} if table_name == "sessions" else {"merchant_id"}
            if not expected.issubset(columns):
                raise RuntimeError(
                    f"database schema does not match migration {EXPECTED_REVISION}"
                )


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
