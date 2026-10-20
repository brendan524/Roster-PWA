"""short-notice leave requests

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leave_requests", sa.Column("short_notice", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("leave_requests", "short_notice")
