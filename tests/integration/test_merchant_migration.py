"""0009 is exercised on disposable SQLite files, never the running application DB."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from alembic.config import Config
from app.repositories.db import init_schema
from app.repositories.models import Base

ROOT = Path(__file__).resolve().parents[2]


def config_for(connection=None, *, database_url=None):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["configure_logger"] = False
    if connection is not None:
        config.attributes["connection"] = connection
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config


def migrate(engine, target="0009", *, downgrade=False):
    # 0009 must own its transaction so SQLite can safely rebuild FK parents.
    with engine.connect() as connection:
        (command.downgrade if downgrade else command.upgrade)(config_for(connection), target)
        connection.commit()
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "merchant-migration.db"
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    migrate(engine, "0008")
    yield engine, path
    engine.dispose()


def insert_old_data(engine):
    statements = (
        "INSERT INTO users VALUES ('u_demo','dummy','buyer',NULL,'active','2026-01-01','2026-01-01',NULL)",
        "INSERT INTO users VALUES ('u_service_agent','dummy','service_agent',NULL,'active','2026-01-01','2026-01-01',NULL)",
        "INSERT INTO auth_session(token_digest,user_id,expires_at,created_at,created_via) VALUES ('" + "a" * 64 + "','u_demo','2030-01-01','2026-01-01','test')",
        "INSERT INTO products(sku,name,category,price_cents,inventory) VALUES ('sku-old','Old product','apparel',29900,5)",
        "INSERT INTO orders(order_id,user_id,status,total_cents,created_at) VALUES ('ord-old','u_demo','paid',29900,'2026-01-01')",
        "INSERT INTO order_items(order_id,sku,name,quantity,unit_price_cents) VALUES ('ord-old','sku-old','Old product',1,29900)",
        "INSERT INTO refunds VALUES ('rf-old','ord-old','requested',29900,'reason','2026-01-01')",
        "INSERT INTO returns VALUES ('rt-old','ord-old','requested','reason','2026-01-01')",
        "INSERT INTO sessions VALUES ('sess-old','u_demo','Summary','2026-01-01','2026-01-01')",
        "INSERT INTO messages(session_id,role,content,created_at) VALUES ('sess-old','assistant','Old answer','2026-01-01')",
        "INSERT INTO receptions VALUES ('sess-old','closed','resolved','2026-01-01','2026-01-01')",
        "INSERT INTO reception_feedback VALUES ('sess-old',5,1,'fine',1,'2026-01-01')",
        "INSERT INTO shopping_tasks VALUES ('sess-old','{}')",
        "INSERT INTO shopping_checkpoints VALUES ('cp-old','sess-old','u_demo','interrupted','{}',NULL,'2026-01-01')",
        "INSERT INTO dataset_versions VALUES ('ds-old','family','v1','product_search','regression','draft','hash','{}',NULL,NULL,'2026-01-01')",
        "INSERT INTO evaluation_runs VALUES ('run-old','ds-old','old','completed','{}','{}','2026-01-01')",
        "INSERT INTO audit_logs(trace_id,action,payload,created_at) VALUES ('trace','refund','ord-old','2026-01-01')",
        "INSERT INTO idempotency_keys VALUES ('old-key','fingerprint','{}','2026-01-01')",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def snapshot(engine):
    with engine.connect() as connection:
        tables = inspect(connection).get_table_names()
        return {
            name: [dict(row) for row in connection.execute(text(f'SELECT * FROM "{name}"')).mappings()]
            for name in tables if name != "alembic_version"
        }


def assert_revision(engine, revision):
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == revision
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_upgrade_backfills_and_preserves_all_existing_rows(database):
    engine, _ = database
    insert_old_data(engine)
    before = snapshot(engine)
    migrate(engine)
    after = snapshot(engine)
    assert_revision(engine, "0009")
    assert {row["merchant_id"] for row in after["merchants"]} == {"m_demo", "m_second"}
    assert all(row["status"] == "active" for row in after["merchants"])
    for table, rows in before.items():
        assert len(after[table]) == len(rows)
        for old, current in zip(rows, after[table], strict=True):
            expected = dict(old)
            if table == "users" and old["user_id"] == "u_service_agent":
                expected["merchant_id"] = "m_demo"
            assert {key: current[key] for key in old} == expected
    assert after["products"][0]["merchant_id"] == "m_demo"
    assert after["orders"][0]["merchant_id"] == "m_demo"
    assert after["sessions"][0]["channel"] == "platform"
    assert after["sessions"][0]["merchant_id"] is None
    assert "merchant_id" not in after["refunds"][0] and "merchant_id" not in after["returns"][0]


def test_migrated_constraints_indexes_match_models(database):
    engine, _ = database
    migrate(engine)
    with engine.connect() as connection:
        inspector = inspect(connection)
        for table in ("products", "orders", "sessions", "users"):
            column = next(c for c in inspector.get_columns(table) if c["name"] == "merchant_id")
            assert column["nullable"] is (table in {"sessions", "users"})
            foreign_key = next(f for f in inspector.get_foreign_keys(table) if f["constrained_columns"] == ["merchant_id"])
            assert foreign_key["referred_table"] == "merchants"
            assert foreign_key["options"]["ondelete"] == "RESTRICT"
            assert foreign_key["name"] == f"fk_{table}_merchant_id_merchants"
            expected_indexes = {index.name for index in Base.metadata.tables[table].indexes}
            assert {index["name"] for index in inspector.get_indexes(table)} == expected_indexes
            expected_checks = {constraint.name for constraint in Base.metadata.tables[table].constraints if constraint.__class__.__name__ == "CheckConstraint"}
            assert {check["name"] for check in inspector.get_check_constraints(table)} == expected_checks
        assert next(c for c in inspector.get_columns("sessions") if c["name"] == "channel")["nullable"] is False


@pytest.mark.parametrize("statement", [
    "INSERT INTO products(sku,name,category,price_cents,inventory) VALUES ('bad','n','c',1,1)",
    "INSERT INTO products(sku,name,category,price_cents,inventory,merchant_id) VALUES ('bad','n','c',1,1,'missing')",
    "INSERT INTO orders(order_id,user_id,status,total_cents,created_at) VALUES ('bad','u','paid',1,'2026-01-01')",
    "INSERT INTO orders(order_id,user_id,status,total_cents,created_at,merchant_id) VALUES ('bad','u','paid',1,'2026-01-01','missing')",
    "INSERT INTO users VALUES ('bad','dummy','service_agent',NULL,'active','2026-01-01','2026-01-01',NULL)",
    "INSERT INTO users VALUES ('bad','dummy','buyer','missing','active','2026-01-01','2026-01-01',NULL)",
    "INSERT INTO sessions(session_id,user_id,summary,created_at,updated_at,channel) VALUES ('bad','u','','2026-01-01','2026-01-01','shop')",
    "INSERT INTO sessions(session_id,user_id,summary,created_at,updated_at,channel,merchant_id) VALUES ('bad','u','','2026-01-01','2026-01-01','platform','m_demo')",
    "INSERT INTO sessions(session_id,user_id,summary,created_at,updated_at,channel,merchant_id) VALUES ('bad','u','','2026-01-01','2026-01-01','shop','missing')",
    "INSERT INTO sessions(session_id,user_id,summary,created_at,updated_at,channel) VALUES ('bad','u','','2026-01-01','2026-01-01','other')",
    "INSERT INTO sessions(session_id,user_id,summary,created_at,updated_at,channel) VALUES ('bad','u','','2026-01-01','2026-01-01',NULL)",
    "INSERT INTO merchants VALUES ('bad','n','other','2026-01-01','2026-01-01')",
])
def test_invalid_ownership_is_rejected_by_database(database, statement):
    engine, _ = database
    migrate(engine)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(statement))
    assert_revision(engine, "0009")


def test_valid_platform_shop_sessions_and_restrict_delete(database):
    engine, _ = database
    insert_old_data(engine)
    migrate(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO sessions(session_id,user_id,summary,created_at,updated_at) VALUES ('platform','u_demo','','2026-01-01','2026-01-01')"))
        connection.execute(text("INSERT INTO sessions(session_id,user_id,summary,created_at,updated_at,channel,merchant_id) VALUES ('shop','u_demo','','2026-01-01','2026-01-01','shop','m_demo')"))
        assert connection.execute(text("SELECT channel FROM sessions WHERE session_id='platform'")).scalar_one() == "platform"
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("DELETE FROM merchants WHERE merchant_id='m_demo'"))


@pytest.mark.parametrize("user_id,role,merchant", [
    ("unassigned-agent", "service_agent", None),
    ("disabled-unassigned", "service_agent", None),
    ("unknown-shop", "buyer", "unknown"),
    ("u_service_agent", "service_agent", "m_second"),
    ("u_service_agent", "buyer", None),
])
def test_preflight_conflicts_stop_without_partial_schema(database, user_id, role, merchant):
    engine, _ = database
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users VALUES (:uid,'dummy',:role,:merchant,:status,'2026-01-01','2026-01-01',NULL)"), {"uid": user_id, "role": role, "merchant": merchant, "status": "disabled" if user_id.startswith("disabled") else "active"})
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="0009 stopped"):
        migrate(engine)
    assert snapshot(engine) == before
    assert_revision(engine, "0008")


def test_existing_known_binding_is_preserved(database):
    engine, _ = database
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users VALUES ('second-agent','dummy','service_agent','m_second','active','2026-01-01','2026-01-01',NULL)"))
    migrate(engine)
    assert snapshot(engine)["users"][0]["merchant_id"] == "m_second"


def test_failed_table_rebuild_rolls_back_ddl_backfill_and_restores_foreign_keys(database):
    engine, _ = database
    insert_old_data(engine)
    before = snapshot(engine)

    def fail_on_sessions(_connection, _cursor, statement, _parameters, _context, _many):
        if "CREATE TABLE _alembic_tmp_sessions" in statement:
            raise RuntimeError("injected batch rebuild failure")

    event.listen(engine, "before_cursor_execute", fail_on_sessions)
    try:
        with pytest.raises(RuntimeError, match="injected batch"):
            migrate(engine)
    finally:
        event.remove(engine, "before_cursor_execute", fail_on_sessions)
    assert snapshot(engine) == before
    assert_revision(engine, "0008")


def test_dedicated_connection_required_before_changes(database):
    engine, _ = database
    before = snapshot(engine)
    with engine.begin() as connection:
        with pytest.raises(RuntimeError, match="dedicated connection"):
            command.upgrade(config_for(connection), "0009")
    assert snapshot(engine) == before
    assert_revision(engine, "0008")


def test_downgrade_to_0008_preserves_business_and_reserved_user_field(database):
    engine, _ = database
    insert_old_data(engine)
    before = snapshot(engine)
    migrate(engine)
    migrate(engine, "0008", downgrade=True)
    assert_revision(engine, "0008")
    after = snapshot(engine)
    assert set(after) == set(before)
    for old in before["users"]:
        if old["user_id"] == "u_service_agent":
            old["merchant_id"] = "m_demo"  # Reserved field exists in historical 0008.
    assert after == before
    migrate(engine)
    assert_revision(engine, "0009")


@pytest.mark.parametrize("statement", [
    "INSERT INTO products(sku,name,category,price_cents,inventory,merchant_id) VALUES ('second','n','c',1,1,'m_second')",
    "INSERT INTO orders(order_id,user_id,status,total_cents,created_at,merchant_id) VALUES ('second','u','paid',1,'2026-01-01','m_second')",
    "INSERT INTO sessions(session_id,user_id,summary,created_at,updated_at,channel,merchant_id) VALUES ('shop','u','','2026-01-01','2026-01-01','shop','m_demo')",
    "INSERT INTO users VALUES ('second-agent','dummy','service_agent','m_second','active','2026-01-01','2026-01-01',NULL)",
    "INSERT INTO merchants VALUES ('third','n','active','2026-01-01','2026-01-01')",
])
def test_downgrade_refuses_to_discard_scoped_data(database, statement):
    engine, _ = database
    migrate(engine)
    with engine.begin() as connection:
        connection.execute(text(statement))
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="downgrade refused"):
        migrate(engine, "0008", downgrade=True)
    assert snapshot(engine) == before
    assert_revision(engine, "0009")


def test_fresh_upgrade_uses_async_migration_connection(tmp_path):
    path = tmp_path / "async-migration.db"
    command.upgrade(config_for(database_url=f"sqlite+aiosqlite:///{path}"), "head")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0010"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert {"merchants", "products", "orders", "users"}.issubset(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def test_relative_upgrade_and_downgrade_use_safe_batch_transaction(database):
    engine, _ = database
    insert_old_data(engine)
    migrate(engine, "+1")
    assert_revision(engine, "0009")
    migrate(engine, "-1", downgrade=True)
    assert_revision(engine, "0008")


def test_downgrade_can_continue_to_historical_auth_revision(database):
    engine, _ = database
    insert_old_data(engine)
    migrate(engine)
    migrate(engine, "0007", downgrade=True)
    assert_revision(engine, "0007")
    after = snapshot(engine)
    assert "users" not in after and "auth_session" not in after and "merchants" not in after
    assert after["order_items"][0]["order_id"] == "ord-old"
    assert after["sessions"][0]["user_id"] == "u_demo"


def test_foreign_key_integrity_failure_rolls_back_entire_migration(database):
    engine, _ = database
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        connection.execute(text("INSERT INTO order_items(order_id,sku,name,quantity,unit_price_cents) VALUES ('missing-order','sku','name',1,1)"))
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="foreign key integrity check failed"):
        migrate(engine)
    assert snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0008"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


@pytest.mark.asyncio
async def test_init_schema_requires_current_revision_and_does_not_upgrade_0008(database):
    engine, path = database
    before = snapshot(engine)
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        with pytest.raises(RuntimeError, match="migrations required"):
            await init_schema(async_engine)
        assert snapshot(engine) == before
        assert_revision(engine, "0008")
        migrate(engine, "0010")
        before = snapshot(engine)
        await init_schema(async_engine)
        assert snapshot(engine) == before
    finally:
        await async_engine.dispose()


def test_offline_upgrade_refuses_to_skip_preflight():
    with pytest.raises(RuntimeError, match="online migration connection"):
        command.upgrade(config_for(database_url="sqlite+aiosqlite:///:memory:"), "0009", sql=True)
