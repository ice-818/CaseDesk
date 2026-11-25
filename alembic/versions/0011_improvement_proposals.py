"""Human-in-the-loop self-evolution improvement proposals.

Adds the ``improvement_proposals`` table that orchestrates the semi-automatic
feedback loop: collect -> attribute -> regress -> gate -> human review ->
promote. No automatic prompt optimization or automatic promotion is possible;
terminal promotion requires an explicit human decision.
"""

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

VALID_STATUS = (
    "status IN ('collect','attribute','regress','gate','review',"
    "'promoted','rejected','blocked')"
)
VALID_REVIEW = (
    "review_decision IS NULL OR review_decision IN ('approved','rejected')"
)


def upgrade() -> None:
    op.create_table(
        "improvement_proposals",
        sa.Column("proposal_id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("candidate_label", sa.String(80), nullable=False),
        sa.Column("source_dataset_id", sa.String(64), nullable=True, index=True),
        sa.Column("baseline_run_id", sa.String(64), nullable=True),
        sa.Column("candidate_run_id", sa.String(64), nullable=True),
        sa.Column("attribution_json", sa.Text(), nullable=False),
        sa.Column("gate_json", sa.Text(), nullable=False),
        sa.Column("review_decision", sa.String(16), nullable=True),
        sa.Column("reviewer", sa.String(80), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(VALID_STATUS, name="ck_improvement_status"),
        sa.CheckConstraint(VALID_REVIEW, name="ck_improvement_review_decision"),
        sa.ForeignKeyConstraint(
            ["source_dataset_id"],
            ["dataset_versions.dataset_id"],
            name="fk_improvement_source_dataset",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_run_id"],
            ["evaluation_runs.run_id"],
            name="fk_improvement_baseline_run",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_run_id"],
            ["evaluation_runs.run_id"],
            name="fk_improvement_candidate_run",
        ),
    )


def downgrade() -> None:
    op.drop_table("improvement_proposals")
