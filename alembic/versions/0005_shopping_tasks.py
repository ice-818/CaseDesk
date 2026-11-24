"""Persist per-session shopping requirements."""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shopping_tasks",
        sa.Column(
            "session_id", sa.String(64), sa.ForeignKey("sessions.session_id"), primary_key=True
        ),
        sa.Column("requirements_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("shopping_tasks")
