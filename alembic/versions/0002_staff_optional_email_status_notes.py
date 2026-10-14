"""staff: optional email, employment status, notes

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("staff", "email", existing_type=sa.String(254), nullable=True)
    op.add_column("staff", sa.Column("employment_status", sa.String(40), nullable=False, server_default=""))
    op.add_column("staff", sa.Column("notes", sa.String(500), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("staff", "notes")
    op.drop_column("staff", "employment_status")
    op.alter_column("staff", "email", existing_type=sa.String(254), nullable=False)
