"""Immutable reception feedback, one record per closed reception."""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reception_feedback",
        sa.Column(
            "session_id", sa.String(64), sa.ForeignKey("receptions.session_id"), primary_key=True
        ),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "last_assistant_message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("reception_feedback")
