"""key register: keys / remotes and who holds them

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "key_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("number", sa.String(20), nullable=False),
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        sa.Column("retired", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('key', 'remote')", name="ck_key_kind"),
        sa.UniqueConstraint("kind", "number", name="uq_key_kind_number"),
    )
    op.create_table(
        "key_issues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("key_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("issued_by", sa.String(32), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("accept_ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("terms", sa.Text(), nullable=False, server_default=""),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.String(32), nullable=True),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        sa.CheckConstraint("status IN ('pending', 'held', 'returned', 'lost', 'cancelled')", name="ck_key_issue_status"),
    )
    op.create_index("ix_key_issues_item_id", "key_issues", ["item_id"])
    op.create_index("ix_key_issues_staff_id", "key_issues", ["staff_id"])
    # A key/remote can only be out with one person at a time.
    op.create_index("uq_key_issue_open", "key_issues", ["item_id"], unique=True,
                    postgresql_where=sa.text("status IN ('pending', 'held')"))


def downgrade() -> None:
    op.drop_table("key_issues")
    op.drop_table("key_items")
