"""Real migration fixtures; never replace production schema validation."""
from pathlib import Path

from sqlalchemy import event

from alembic import command
from alembic.config import Config


async def migrate_test_database(engine):
    # Existing callers supply disposable in-memory/file engines only.
    if engine.url.get_backend_name() != "sqlite":
        raise ValueError("fixture helper expects a disposable SQLite database")
    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
    def upgrade(connection):
        root=Path(__file__).resolve().parents[1]
        config=Config(str(root/"alembic.ini"))
        config.set_main_option("script_location",str(root/"alembic"))
        config.attributes.update(connection=connection,configure_logger=False)
        command.upgrade(config, "0011")
    async with engine.connect() as connection:
        await connection.run_sync(upgrade)
