"""pay report: venues, travel legs, saved reports, teaching-hours opening balance

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "venues",
        sa.Column("name", sa.String(120), primary_key=True),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False, server_default="unknown"),
        sa.Column("address", sa.String(300), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('campus', 'client', 'unknown')", name="ck_venue_kind"),
    )
    op.create_table(
        "travel_legs",
        sa.Column("origin", sa.String(300), primary_key=True),
        sa.Column("destination", sa.String(300), primary_key=True),
        sa.Column("km", sa.Float(), nullable=False),
        sa.Column("minutes", sa.Float(), nullable=False),
        sa.Column("source", sa.String(10), nullable=False, server_default="google"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "pay_reports",
        sa.Column("period_end", sa.Date(), primary_key=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("data", sa.String(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("staff", sa.Column("teaching_hours_opening", sa.Float(), nullable=False, server_default="0"))
    op.add_column("staff", sa.Column("teaching_hours_as_of", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("staff", "teaching_hours_as_of")
    op.drop_column("staff", "teaching_hours_opening")
    op.drop_table("pay_reports")
    op.drop_table("travel_legs")
    op.drop_table("venues")
