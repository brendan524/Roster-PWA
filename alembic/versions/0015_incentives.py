"""incentive claims (offshore staff): kind + client on expense_claims

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("expense_claims", sa.Column("kind", sa.String(10), nullable=False, server_default="expense"))
    op.add_column("expense_claims", sa.Column("client", sa.String(200), nullable=False, server_default=""))
    op.create_check_constraint("ck_expense_kind", "expense_claims", "kind IN ('expense', 'incentive')")
    op.drop_constraint("ck_expense_category", "expense_claims", type_="check")
    op.create_check_constraint("ck_expense_category", "expense_claims",
                               "category IN ('parking', 'travel', 'equipment', 'other', 'incentive')")


def downgrade() -> None:
    op.execute("DELETE FROM expense_claims WHERE kind = 'incentive'")
    op.drop_constraint("ck_expense_category", "expense_claims", type_="check")
    op.create_check_constraint("ck_expense_category", "expense_claims",
                               "category IN ('parking', 'travel', 'equipment', 'other')")
    op.drop_constraint("ck_expense_kind", "expense_claims", type_="check")
    op.drop_column("expense_claims", "client")
    op.drop_column("expense_claims", "kind")
