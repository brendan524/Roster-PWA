"""weekly availability removed: casual/contract staff give specific dates and times only

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("availability_weekly")


def downgrade() -> None:
    op.create_table(
        "availability_weekly",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id", ondelete="CASCADE"), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
