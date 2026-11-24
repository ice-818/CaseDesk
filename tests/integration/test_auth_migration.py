from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from alembic.config import Config
from app.repositories.db import init_schema

ROOT = Path(__file__).resolve().parents[2]


def migrate(connection, target="0008", *, downgrade=False):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes.update(connection=connection, configure_logger=False)
    (command.downgrade if downgrade else command.upgrade)(config, target)


def test_auth_upgrade_constraints_and_downgrade_preserve_business(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        migrate(connection, "0007")
        connection.execute(
            text(
                "INSERT INTO sessions(session_id,user_id,summary,created_at,updated_at) VALUES ('old','u_demo','','2026-01-01','2026-01-01')"
            )
        )
        migrate(connection)
        inspector = inspect(connection)
        assert {"users", "auth_session"}.issubset(inspector.get_table_names())
        assert {i["name"] for i in inspector.get_indexes("auth_session")} == {
            "ix_auth_session_user_revoked",
            "ix_auth_session_expires_at",
        }
        assert inspector.get_foreign_keys("auth_session")[0]["referred_table"] == "users"
        assert len(inspector.get_check_constraints("users")) == 2
        assert len(inspector.get_check_constraints("auth_session")) == 3
        for role, status in [("admin", "active"), ("buyer", "unknown")]:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO users VALUES ('bad','hash',:role,NULL,:status,'2026-01-01','2026-01-01',NULL)"
                    ),
                    {"role": role, "status": status},
                )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(
                    "INSERT INTO auth_session(token_digest,user_id,expires_at,created_at,created_via) VALUES ('missing','unknown','2026-02-01','2026-01-01','test')"
                )
            )
        connection.execute(
            text(
                "INSERT INTO users VALUES ('valid','hash','buyer',NULL,'active','2026-01-01','2026-01-01',NULL)"
            )
        )
        for uid, expiry, revoked, stamp in [
            ("unknown", "2026-02-01", False, None),
            ("valid", "2025-01-01", False, None),
            ("valid", "2026-02-01", True, None),
        ]:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO auth_session(token_digest,user_id,expires_at,created_at,created_via,revoked,revoked_at) VALUES (:digest,:uid,:expiry,'2026-01-01','test',:revoked,:stamp)"
                    ),
                    {
                        "digest": "a" * 64,
                        "uid": uid,
                        "expiry": expiry,
                        "revoked": revoked,
                        "stamp": stamp,
                    },
                )
        migrate(connection, "0007", downgrade=True)
        assert "users" not in inspect(connection).get_table_names()
        assert (
            connection.execute(
                text("SELECT user_id FROM sessions WHERE session_id='old'")
            ).scalar_one()
            == "u_demo"
        )
    engine.dispose()


@pytest.mark.asyncio
async def test_init_schema_does_not_create_or_stamp_unmigrated_tables():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        with pytest.raises(RuntimeError, match="migrations required"):
            await init_schema(engine)
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        assert names == []
    finally:
        await engine.dispose()
