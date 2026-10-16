"""expense claims with receipts

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "expense_claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id", ondelete="CASCADE"), nullable=False),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_by", sa.String(32), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(500), nullable=False, server_default=""),
        sa.Column("paid_in_period", sa.Date(), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_expense_amount"),
        sa.CheckConstraint("status IN ('pending', 'approved', 'declined', 'cancelled')", name="ck_expense_status"),
        sa.CheckConstraint("category IN ('parking', 'travel', 'equipment', 'other')", name="ck_expense_category"),
    )
    op.create_index("ix_expense_claims_staff_id", "expense_claims", ["staff_id"])
    op.create_table(
        "expense_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("claim_id", sa.Integer(), sa.ForeignKey("expense_claims.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("content_type", sa.String(60), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_expense_attachments_claim_id", "expense_attachments", ["claim_id"])


def downgrade() -> None:
    op.drop_table("expense_attachments")
    op.drop_table("expense_claims")
