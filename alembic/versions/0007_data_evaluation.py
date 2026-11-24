"""Versioned data and offline evaluation control plane."""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_versions",
        sa.Column("dataset_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, index=True),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("task", sa.String(32), nullable=False),
        sa.Column("split", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("reviewer", sa.String(80), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_dataset_version"),
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.String(64),
            sa.ForeignKey("dataset_versions.dataset_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("candidate_label", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("evaluation_runs")
    op.drop_table("dataset_versions")
