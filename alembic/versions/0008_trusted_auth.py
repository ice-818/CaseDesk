"""Opaque server-side authentication; existing business ownership is unchanged."""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("merchant_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('buyer','service_agent','operator')", name="ck_users_role"),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_users_status"),
    )
    op.create_table(
        "auth_session",
        sa.Column("token_digest", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.user_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_via", sa.String(16), nullable=False),
        sa.Column("created_ip", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.CheckConstraint("length(token_digest) = 64", name="ck_auth_session_digest"),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_session_expiry"),
        sa.CheckConstraint(
            "(revoked = false AND revoked_at IS NULL) OR (revoked = true AND revoked_at IS NOT NULL)",
            name="ck_auth_session_revocation",
        ),
    )
    op.create_index("ix_auth_session_user_revoked", "auth_session", ["user_id", "revoked"])
    op.create_index("ix_auth_session_expires_at", "auth_session", ["expires_at"])


def downgrade() -> None:
    op.drop_table("auth_session")
    op.drop_table("users")
