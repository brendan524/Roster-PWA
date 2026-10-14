"""staff and auth sessions

Revision ID: 0001
Revises:
Create Date: 2026-10-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff",
        sa.Column("staff_id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("department", sa.String(60), nullable=False, server_default=""),
        sa.Column("location_type", sa.String(10), nullable=False),
        sa.Column("join_date", sa.Date(), nullable=True),
        sa.Column("role", sa.String(10), nullable=False, server_default="staff"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("pin_hash", sa.String(64), nullable=True, unique=True),
        sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("location_type IN ('onshore', 'offshore')", name="ck_staff_location_type"),
        sa.CheckConstraint("role IN ('staff', 'admin')", name="ck_staff_role"),
        sa.CheckConstraint("staff_id = upper(staff_id)", name="ck_staff_id_upper"),
        sa.CheckConstraint("email = lower(email)", name="ck_staff_email_lower"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.String(255), nullable=False, server_default=""),
    )
    op.create_index("ix_auth_sessions_staff_id", "auth_sessions", ["staff_id"])


def downgrade() -> None:
    op.drop_table("auth_sessions")
    op.drop_table("staff")
