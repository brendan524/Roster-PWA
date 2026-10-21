"""leave recorded by an admin on someone's behalf

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-21
"""
import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leave_requests", sa.Column("requested_by", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("leave_requests", "requested_by")
