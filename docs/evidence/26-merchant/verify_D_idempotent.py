import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.seed_merchants import seed_merchants
from tests.auth_support import prepare_auth_database


async def main() -> None:
    tmp = Path(tempfile.mkdtemp()) / "seed_idem.db"
    prepare_auth_database(tmp)  # real migration to 0009 plus u_demo buyer
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    first = await seed_merchants(factory)
    second = await seed_merchants(factory)

    print("run1 created:", first["created"])
    print("run1 conflicts:", first["conflicts"])
    print("run2 created:", second["created"])
    print("run2 conflicts:", second["conflicts"])
    print("run2 counts:", second["counts"])
    print("counts identical:", first["counts"] == second["counts"])
    assert not first["conflicts"] and not second["conflicts"]
    assert second["created"] == []
    assert first["counts"] == second["counts"]
    await engine.dispose()
    print("DONE")


asyncio.run(main())
