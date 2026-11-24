"""Alembic migrations.

Authentication tables are created only through migration 0008.
Use the configured async database URL or an explicit test connection.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from alembic.migration import MigrationContext
from app.config.settings import get_settings
from app.repositories.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
_LEGACY_REVISIONS = ("base", "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008")


def run_migrations_offline() -> None:
    if (context.get_revision_argument() not in _LEGACY_REVISIONS
            or context.get_starting_revision_argument() == "0009"):
        raise RuntimeError("0009 requires an online migration connection for ownership preflight")
    url = config.attributes.get("database_url") or get_settings().database_url
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_connection(connection, *, transactional_ddl=None) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, transactional_ddl=transactional_ddl)
    with context.begin_transaction():
        context.run_migrations()


def run_connection(connection) -> None:
    if connection.dialect.name != "sqlite":
        _run_connection(connection)
        return
    external_transaction = connection.in_transaction()
    current = MigrationContext.configure(connection).get_current_revision()
    target = context.get_revision_argument()
    # Relative targets (+1/-1), head aliases and 0009 need the same safe path.
    # Explicit historical migrations retain their previous connection contract.
    crosses_merchant_revision = current == "0009" or target not in _LEGACY_REVISIONS
    if not crosses_merchant_revision:
        _run_connection(connection)
        return
    if context.is_offline_mode():
        raise RuntimeError("0009 requires an online migration connection for ownership preflight")
    if external_transaction:
        raise RuntimeError("0009 SQLite migration requires a dedicated connection outside an existing transaction")
    # The revision probe starts SQLAlchemy's implicit transaction, not a SQLite
    # write transaction. End it before changing this migration connection's PRAGMA.
    connection.rollback()
    original_foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    connection.rollback()
    try:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        with connection.begin():
            # Explicit BEGIN makes SQLite DDL transactional even under sqlite3's
            # legacy transaction mode; injected failures roll back table rebuilds.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            _run_connection(connection, transactional_ddl=True)
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                raise RuntimeError("migration foreign key integrity check failed")
    finally:
        connection.exec_driver_sql(f"PRAGMA foreign_keys={int(original_foreign_keys)}")
        connection.commit()


async def run_migrations_online() -> None:
    engine = create_async_engine(
        config.attributes.get("database_url") or get_settings().database_url,
        poolclass=pool.NullPool,
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_connection)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    if config.attributes.get("connection") is not None:
        run_connection(config.attributes["connection"])
    else:
        asyncio.run(run_migrations_online())
