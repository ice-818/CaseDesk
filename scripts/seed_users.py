"""Explicit, repeatable account bootstrap. Never reset existing credentials."""

import argparse
import asyncio
import getpass
import os

from sqlalchemy.exc import SQLAlchemyError

from app.config.settings import get_settings
from app.repositories.auth_repository import AuthRepository
from app.repositories.db import create_engine, create_session_factory
from app.repositories.models import UserRow
from app.services.auth_service import AuthService, auth_storage_error, hash_password
from app.services.seed import ROLE_MERCHANTS, seed_demo_data

ACCOUNTS = {
    "u_demo": ("buyer", "CASEDESK_SEED_BUYER_PASSWORD"),
    "u_service_agent": ("service_agent", "CASEDESK_SEED_AGENT_PASSWORD"),
    "u_operator": ("operator", "CASEDESK_SEED_OPERATOR_PASSWORD"),
}


async def seed_users(factory, passwords: dict[str, str], *, with_demo_data=False):
    settings = get_settings()
    await AuthService(factory, settings).check_schema(factory.kw["bind"])
    missing, skipped, conflicts = [], [], []
    try:
        async with factory() as db:
            for user_id, (role, _) in ACCOUNTS.items():
                row = await AuthRepository(db).user(user_id)
                if row is None:
                    missing.append(user_id)
                elif row.role != role or row.merchant_id != ROLE_MERCHANTS[role]:
                    conflicts.append(user_id)
                else:
                    skipped.append(user_id)
        if conflicts:
            return {"created": [], "skipped": skipped, "conflicts": conflicts}
        encoded = {}
        for user_id in missing:
            if user_id not in passwords:
                raise ValueError(f"password required for {user_id}")
            encoded[user_id] = await hash_password(passwords[user_id])
        async with factory() as db:
            for user_id in missing:
                # A concurrent creator is a conflict, never an overwrite.
                if await AuthRepository(db).user(user_id) is not None:
                    raise ValueError(f"account changed concurrently: {user_id}")
                db.add(
                    UserRow(
                        user_id=user_id,
                        role=ACCOUNTS[user_id][0],
                        merchant_id=ROLE_MERCHANTS[ACCOUNTS[user_id][0]],
                        password_hash=encoded[user_id],
                        status="active",
                    )
                )
            await db.flush()
            if with_demo_data:
                await seed_demo_data(db)
            await db.commit()
        return {"created": missing, "skipped": skipped, "conflicts": []}
    except SQLAlchemyError:
        raise auth_storage_error() from None


async def main() -> None:
    parser = argparse.ArgumentParser(description="预置账号初始化；已有账号绝不覆盖")
    parser.add_argument("--with-demo-data", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        await AuthService(factory, settings).check_schema(engine)
        passwords = {}
        async with factory() as db:
            missing = [uid for uid in ACCOUNTS if await AuthRepository(db).user(uid) is None]
        for uid in missing:
            supplied = os.environ.get(ACCOUNTS[uid][1])
            passwords[uid] = (
                supplied
                if supplied is not None
                else await asyncio.to_thread(getpass.getpass, f"{uid} 密码（隐藏输入）: ")
            )
        result = await seed_users(factory, passwords, with_demo_data=args.with_demo_data)
        print("Created:", ", ".join(result["created"]) or "none")
        print("Existing (unchanged):", ", ".join(result["skipped"]) or "none")
        if result["conflicts"]:
            print("Role conflicts (unchanged):", ", ".join(result["conflicts"]))
            raise SystemExit(1)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
