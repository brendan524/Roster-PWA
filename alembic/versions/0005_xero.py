"""xero connection, employee links, leave balances

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "xero_connection",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("tenant_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("access_token_enc", sa.String(8000), nullable=False),
        sa.Column("refresh_token_enc", sa.String(2000), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("connected_by", sa.String(32), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=False, server_default=""),
        sa.CheckConstraint("id = 1", name="ck_xero_single_row"),
    )
    op.create_table(
        "xero_employee_links",
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("xero_employee_id", sa.String(64), nullable=False, unique=True),
        sa.Column("xero_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("linked_by", sa.String(32), nullable=False),
    )
    op.create_table(
        "xero_leave_balances",
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("leave_type_id", sa.String(64), primary_key=True),
        sa.Column("leave_name", sa.String(200), nullable=False),
        sa.Column("units", sa.Numeric(10, 4), nullable=False),
        sa.Column("unit_type", sa.String(20), nullable=False, server_default="Hours"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("xero_leave_balances")
    op.drop_table("xero_employee_links")
    op.drop_table("xero_connection")
