"""incentive type and booking-table details on claims

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-19
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("expense_claims", sa.Column("incentive_type", sa.String(20), nullable=False, server_default=""))
    op.add_column("expense_claims", sa.Column("details", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("expense_claims", "details")
    op.drop_column("expense_claims", "incentive_type")
