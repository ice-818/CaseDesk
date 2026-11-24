import pytest
from sqlalchemy import select

from app.config.settings import Settings
from app.repositories.db import create_engine, create_session_factory
from app.repositories.models import OrderRow, UserRow
from scripts.seed_users import ACCOUNTS, main, seed_users
from tests.auth_support import TEST_PASSWORD, prepare_auth_database


@pytest.mark.asyncio
async def test_seed_is_repeatable_preserves_password_status_and_business(tmp_path):
    path = tmp_path / "seed.db"
    prepare_auth_database(path)
    engine = create_engine(Settings(database_url=f"sqlite+aiosqlite:///{path}"))
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            user = await db.get(UserRow, "u_demo")
            previous = user.password_hash
            user.status = "disabled"
            await db.commit()
        result = await seed_users(
            factory, {uid: TEST_PASSWORD for uid in ACCOUNTS}, with_demo_data=True
        )
        assert result["created"] == ["u_service_agent", "u_operator"]
        assert not (await seed_users(factory, {}, with_demo_data=True))["created"]
        async with factory() as db:
            user = await db.get(UserRow, "u_demo")
            assert user.password_hash == previous and user.status == "disabled"
            assert len((await db.scalars(select(OrderRow))).all()) == 3
            assert (await db.get(UserRow, "u_service_agent")).merchant_id == "m_demo"
            assert (await db.get(UserRow, "u_operator")).merchant_id is None
            user.role = "service_agent"
            user.merchant_id = "m_demo"
            await db.commit()
        conflict = await seed_users(factory, {})
        assert conflict["conflicts"] == ["u_demo"]
        async with factory() as db:
            assert (await db.get(UserRow, "u_demo")).role == "service_agent"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seed_environment_passwords_are_not_printed(tmp_path, monkeypatch, capsys):
    path = tmp_path / "environment.db"
    prepare_auth_database(path)
    from app.config.settings import clear_settings_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr("sys.argv", ["seed_users.py"])
    for _, variable in ACCOUNTS.values():
        monkeypatch.setenv(variable, TEST_PASSWORD)
    clear_settings_cache()
    try:
        await main()
        assert TEST_PASSWORD not in capsys.readouterr().out
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_role_binding_applies_to_new_account_names_and_never_overwrites(tmp_path, monkeypatch):
    from tests.database_support import migrate_test_database
    engine = create_engine(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'roles.db'}"))
    await migrate_test_database(engine)
    factory = create_session_factory(engine)
    monkeypatch.setitem(ACCOUNTS, "another_agent", ("service_agent", "UNUSED_TEST_PASSWORD"))
    try:
        await seed_users(factory, {uid: TEST_PASSWORD for uid in ACCOUNTS})
        async with factory() as db:
            assert (await db.get(UserRow, "another_agent")).merchant_id == "m_demo"
            assert (await db.get(UserRow, "u_demo")).merchant_id is None
            assert (await db.get(UserRow, "u_operator")).merchant_id is None
            agent = await db.get(UserRow, "another_agent")
            previous = agent.password_hash
            agent.merchant_id = "m_second"
            await db.commit()
        assert "another_agent" in (await seed_users(factory, {}))["conflicts"]
        async with factory() as db:
            agent = await db.get(UserRow, "another_agent")
            assert agent.merchant_id == "m_second" and agent.password_hash == previous
    finally:
        await engine.dispose()
