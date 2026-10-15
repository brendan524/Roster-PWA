"""staff.has_leave (contractors take no leave) and Xero start dates (for long service)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("staff", sa.Column("has_leave", sa.Boolean(), nullable=False, server_default=sa.true()))
    # Onshore contractors aren't employees, so no leave. (Offshore staff are "Contractor" but do get leave.)
    op.execute("UPDATE staff SET has_leave = false "
               "WHERE location_type = 'onshore' AND lower(employment_status) LIKE 'contract%'")
    op.add_column("xero_employee_links", sa.Column("start_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("xero_employee_links", "start_date")
    op.drop_column("staff", "has_leave")
