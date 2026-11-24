"""Merchant roots and shop-order ownership; existing sessions remain platform sessions.

SQLite batch changes require the dedicated transaction managed by env.py.
No application model imports: this migration preserves its own historical schema.
"""

from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _preflight() -> None:
    connection = op.get_bind()
    unresolved = connection.execute(
        sa.text(
            "SELECT user_id FROM users WHERE role = 'service_agent' "
            "AND merchant_id IS NULL AND user_id <> 'u_service_agent' ORDER BY user_id"
        )
    ).scalars().all()
    if unresolved:
        raise RuntimeError("0009 stopped: service_agent merchant binding required for " + ", ".join(unresolved))
    conflicts = connection.execute(
        sa.text(
            "SELECT user_id FROM users WHERE "
            "(merchant_id IS NOT NULL AND merchant_id NOT IN ('m_demo','m_second')) OR "
            "(user_id = 'u_service_agent' AND "
            "(role <> 'service_agent' OR (merchant_id IS NOT NULL AND merchant_id <> 'm_demo'))) "
            "ORDER BY user_id"
        )
    ).scalars().all()
    if conflicts:
        raise RuntimeError("0009 stopped: existing merchant/role binding conflict for " + ", ".join(conflicts))


def upgrade() -> None:
    _preflight()
    merchants = op.create_table(
        "merchants",
        sa.Column("merchant_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_merchants_status"),
    )
    now = datetime.now(UTC)
    op.bulk_insert(merchants, [
        {"merchant_id": "m_demo", "name": "CaseDesk demo shop", "status": "active", "created_at": now, "updated_at": now},
        {"merchant_id": "m_second", "name": "CaseDesk second shop", "status": "active", "created_at": now, "updated_at": now},
    ])
    op.execute(sa.text("UPDATE users SET merchant_id='m_demo' WHERE user_id='u_service_agent'"))
    with op.batch_alter_table("users") as batch:
        batch.create_foreign_key("fk_users_merchant_id_merchants", "merchants", ["merchant_id"], ["merchant_id"], ondelete="RESTRICT")
        batch.create_check_constraint("ck_users_service_agent_merchant", "role <> 'service_agent' OR merchant_id IS NOT NULL")
        batch.create_index("ix_users_merchant_id", ["merchant_id"])
    for table in ("products", "orders"):
        op.add_column(table, sa.Column("merchant_id", sa.String(64), nullable=True))
        op.execute(sa.text(f"UPDATE {table} SET merchant_id='m_demo'"))
        with op.batch_alter_table(table) as batch:
            batch.alter_column("merchant_id", existing_type=sa.String(64), nullable=False)
            batch.create_foreign_key(f"fk_{table}_merchant_id_merchants", "merchants", ["merchant_id"], ["merchant_id"], ondelete="RESTRICT")
            batch.create_index(f"ix_{table}_merchant_id", ["merchant_id"])
            if table == "orders":
                batch.create_index("ix_orders_merchant_user", ["merchant_id", "user_id"])
    op.add_column("sessions", sa.Column("channel", sa.String(16), nullable=True))
    op.add_column("sessions", sa.Column("merchant_id", sa.String(64), nullable=True))
    op.execute(sa.text("UPDATE sessions SET channel='platform', merchant_id=NULL"))
    with op.batch_alter_table("sessions") as batch:
        batch.alter_column("channel", existing_type=sa.String(16), nullable=False, server_default="platform")
        batch.create_foreign_key("fk_sessions_merchant_id_merchants", "merchants", ["merchant_id"], ["merchant_id"], ondelete="RESTRICT")
        batch.create_check_constraint("ck_sessions_channel", "channel IN ('platform','shop')")
        batch.create_check_constraint("ck_sessions_channel_merchant", "(channel = 'platform' AND merchant_id IS NULL) OR (channel = 'shop' AND merchant_id IS NOT NULL)")
        batch.create_index("ix_sessions_merchant_user", ["merchant_id", "user_id"])


def downgrade() -> None:
    connection = op.get_bind()
    # Dropping ownership after second-shop activity would silently erase isolation.
    conditions = (
        "SELECT 1 FROM products WHERE merchant_id <> 'm_demo' LIMIT 1",
        "SELECT 1 FROM orders WHERE merchant_id <> 'm_demo' LIMIT 1",
        "SELECT 1 FROM sessions WHERE channel <> 'platform' LIMIT 1",
        "SELECT 1 FROM users WHERE merchant_id IS NOT NULL AND merchant_id <> 'm_demo' LIMIT 1",
        "SELECT 1 FROM merchants WHERE merchant_id NOT IN ('m_demo','m_second') LIMIT 1",
    )
    if any(connection.execute(sa.text(sql)).first() is not None for sql in conditions):
        raise RuntimeError("0009 downgrade refused: merchant-scoped data exists; use a reviewed backup/data rollback")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_index("ix_sessions_merchant_user")
        batch.drop_constraint("ck_sessions_channel_merchant", type_="check")
        batch.drop_constraint("ck_sessions_channel", type_="check")
        batch.drop_constraint("fk_sessions_merchant_id_merchants", type_="foreignkey")
        batch.drop_column("channel")
        batch.drop_column("merchant_id")
    for table in ("orders", "products"):
        with op.batch_alter_table(table) as batch:
            if table == "orders":
                batch.drop_index("ix_orders_merchant_user")
            batch.drop_index(f"ix_{table}_merchant_id")
            batch.drop_constraint(f"fk_{table}_merchant_id_merchants", type_="foreignkey")
            batch.drop_column("merchant_id")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_merchant_id")
        batch.drop_constraint("ck_users_service_agent_merchant", type_="check")
        batch.drop_constraint("fk_users_merchant_id_merchants", type_="foreignkey")
    # users.merchant_id existed in 0008: preserve it, including the demo binding.
    op.drop_table("merchants")
