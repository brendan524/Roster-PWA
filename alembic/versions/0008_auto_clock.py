"""staff.auto_clock (punches at rostered times) and Xero employment basis (casuals)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_punch_source", "punches", type_="check")
    op.create_check_constraint("ck_punch_source", "punches", "source IN ('personal', 'kiosk', 'admin', 'auto')")
    op.add_column("staff", sa.Column("auto_clock", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("xero_employee_links", sa.Column("employment_basis", sa.String(20), nullable=False, server_default=""))
    # Brendan and Aaron don't clock in themselves.
    op.execute("UPDATE staff SET auto_clock = true WHERE staff_id IN ('AB0001', 'AB0002')")


def downgrade() -> None:
    op.drop_column("xero_employee_links", "employment_basis")
    op.drop_column("staff", "auto_clock")
    op.drop_constraint("ck_punch_source", "punches", type_="check")
    op.create_check_constraint("ck_punch_source", "punches", "source IN ('personal', 'kiosk', 'admin')")
