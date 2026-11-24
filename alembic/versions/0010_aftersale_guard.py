"""After-sale status machine and database-level duplicate guard.

Adds a CHECK constraint for the refund/return lifecycle and a partial unique
index so each order can have at most one active (requested/approved) document
per table. Terminal states rejected/completed do not hold the slot, allowing a
new request afterwards. No application model imports: historical schema is
described inline.
"""

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

ACTIVE = "status IN ('requested', 'approved')"
VALID = "status IN ('requested', 'approved', 'rejected', 'completed')"


def _preflight() -> None:
    connection = op.get_bind()
    for table in ("refunds", "returns"):
        bad = connection.execute(
            sa.text(
                f"SELECT order_id, COUNT(*) FROM {table} "
                f"WHERE {ACTIVE} GROUP BY order_id HAVING COUNT(*) > 1"
            )
        ).first()
        if bad is not None:
            raise RuntimeError(
                f"0010 stopped: {table} has multiple active documents for order {bad[0]}"
            )
        unknown = connection.execute(
            sa.text(f"SELECT DISTINCT status FROM {table} WHERE NOT ({VALID})")
        ).scalars().all()
        if unknown:
            raise RuntimeError(
                f"0010 stopped: {table} has unknown status values {', '.join(unknown)}"
            )


def upgrade() -> None:
    _preflight()
    with op.batch_alter_table("refunds") as batch:
        batch.create_check_constraint("ck_refunds_status", VALID)
    with op.batch_alter_table("returns") as batch:
        batch.create_check_constraint("ck_returns_status", VALID)
    where = sa.text(ACTIVE)
    op.create_index(
        "uq_refunds_active_order",
        "refunds",
        ["order_id"],
        unique=True,
        sqlite_where=where,
        postgresql_where=where,
    )
    op.create_index(
        "uq_returns_active_order",
        "returns",
        ["order_id"],
        unique=True,
        sqlite_where=where,
        postgresql_where=where,
    )


def downgrade() -> None:
    op.drop_index("uq_returns_active_order", table_name="returns")
    op.drop_index("uq_refunds_active_order", table_name="refunds")
    with op.batch_alter_table("returns") as batch:
        batch.drop_constraint("ck_returns_status", type_="check")
    with op.batch_alter_table("refunds") as batch:
        batch.drop_constraint("ck_refunds_status", type_="check")
