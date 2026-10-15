"""availability (weekly + one-off), email outbox, app state

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "availability_weekly",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id", ondelete="CASCADE"), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_avail_weekday"),
        sa.CheckConstraint("(start_time IS NULL) = (end_time IS NULL) AND (start_time IS NULL OR start_time < end_time)",
                           name="ck_avail_weekly_times"),
    )
    op.create_index("ix_availability_weekly_staff_id", "availability_weekly", ["staff_id"])
    op.create_table(
        "availability_dates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id", ondelete="CASCADE"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("end_date >= start_date", name="ck_avail_dates_order"),
        sa.CheckConstraint("(start_time IS NULL) = (end_time IS NULL) AND (start_time IS NULL OR start_time < end_time)",
                           name="ck_avail_dates_times"),
    )
    op.create_index("ix_availability_dates_staff_id", "availability_dates", ["staff_id"])
    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("to_addr", sa.String(254), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=False, server_default=""),
    )
    op.create_index("ix_email_outbox_pending", "email_outbox", ["sent_at", "next_attempt_at"])
    op.create_table(
        "app_state",
        sa.Column("key", sa.String(60), primary_key=True),
        sa.Column("value", sa.String(500), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_state")
    op.drop_table("email_outbox")
    op.drop_table("availability_dates")
    op.drop_table("availability_weekly")
