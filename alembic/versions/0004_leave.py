"""leave requests and balance adjustments

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "leave_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id"), nullable=False),
        sa.Column("leave_type", sa.String(20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("start_half", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("end_half", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("days", sa.Numeric(5, 1), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False, server_default=""),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_by", sa.String(32), sa.ForeignKey("staff.staff_id"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(500), nullable=False, server_default=""),
        sa.CheckConstraint("status IN ('pending', 'approved', 'declined', 'cancelled')", name="ck_leave_status"),
        sa.CheckConstraint("end_date >= start_date", name="ck_leave_dates"),
        sa.CheckConstraint("days > 0", name="ck_leave_days"),
    )
    op.create_index("ix_leave_requests_staff_id", "leave_requests", ["staff_id"])
    op.create_table(
        "leave_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id"), nullable=False),
        sa.Column("leave_type", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(4), nullable=False),
        sa.Column("amount", sa.Numeric(6, 2), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('set', 'add')", name="ck_adjust_kind"),
        sa.CheckConstraint("reason != ''", name="ck_adjust_reason"),
    )
    op.create_index("ix_leave_adjustments_staff_id", "leave_adjustments", ["staff_id"])
    op.execute("""
        CREATE TRIGGER leave_adjustments_no_update_delete BEFORE UPDATE OR DELETE ON leave_adjustments
        FOR EACH ROW EXECUTE FUNCTION punches_append_only();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS leave_adjustments_no_update_delete ON leave_adjustments")
    op.drop_table("leave_adjustments")
    op.drop_table("leave_requests")
