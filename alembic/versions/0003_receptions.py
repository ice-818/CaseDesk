"""Reception lifecycle without altering existing chat history.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "receptions",
        sa.Column(
            "session_id", sa.String(64), sa.ForeignKey("sessions.session_id"), primary_key=True
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("end_reason", sa.String(24), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("receptions")
