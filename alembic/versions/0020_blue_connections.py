"""blue connections: each staff member's own Blue account (encrypted OAuth tokens)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blue_connections",
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("client_id", sa.String(200), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blue_user_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("blue_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("blue_email", sa.String(254), nullable=False, server_default=""),
        sa.Column("connected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("blue_connections")
