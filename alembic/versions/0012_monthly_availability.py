"""monthly availability: entries can be 'available', per-person mode, submitted months

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("availability_dates", sa.Column("kind", sa.String(12), nullable=False, server_default="unavailable"))
    op.create_check_constraint("ck_avail_kind", "availability_dates", "kind IN ('unavailable', 'available')")
    op.add_column("staff", sa.Column("availability_mode", sa.String(12), nullable=False, server_default="unavailable"))
    op.create_check_constraint("ck_staff_avail_mode", "staff", "availability_mode IN ('unavailable', 'available')")
    op.create_table(
        "availability_months",
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("month", sa.String(7), primary_key=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("availability_months")
    op.drop_constraint("ck_staff_avail_mode", "staff", type_="check")
    op.drop_column("staff", "availability_mode")
    op.drop_constraint("ck_avail_kind", "availability_dates", type_="check")
    op.drop_column("availability_dates", "kind")
