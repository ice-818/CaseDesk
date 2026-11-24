"""Isolated databases and real credentials; no runtime authentication bypass."""

import asyncio
from pathlib import Path

import bcrypt
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config
from app.config.settings import Settings
from app.core.time import utcnow
from app.repositories.db import create_engine as async_engine, create_session_factory
from app.repositories.models import UserRow
from app.services.auth_service import AuthService

TEST_PASSWORD = "case-test-password"
TEST_USERS = {
    "u_demo": "buyer",
    "owner": "buyer",
    "u": "buyer",
    "other": "buyer",
    "op": "operator",
    "op2": "operator",
    "agent": "service_agent",
}
ROOT = Path(__file__).resolve().parents[1]


def prepare_auth_database(path: Path):
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "alembic"))
        config.attributes.update(connection=connection, configure_logger=False)
        command.upgrade(config, "head")
        connection.commit()
    # Low-cost fixture hashes only; production creation always uses rounds=12.
    encoded = bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    with Session(engine) as db:
        db.add_all(
            UserRow(
                user_id=uid,
                role=role,
                password_hash=encoded,
                status="active",
                merchant_id="m_demo" if role == "service_agent" else None,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            for uid, role in TEST_USERS.items()
        )
        db.commit()
    engine.dispose()


async def login_test_users(path: Path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{path}")
    engine = async_engine(settings)
    service = AuthService(create_session_factory(engine), settings)
    try:
        return {uid: (await service.login(uid, TEST_PASSWORD)).access_token for uid in TEST_USERS}
    finally:
        await engine.dispose()


def test_headers(path: Path):
    return asyncio.run(login_test_users(path))


test_headers.__test__ = False
