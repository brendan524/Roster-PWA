"""First Aid travel eligibility, and opening teaching hours for Judy and Jody

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("staff", sa.Column("travel_eligible", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Judy Redfern (AB0018) and Jody Wilms (AB0016) are paid travel to client sites.
    op.execute("UPDATE staff SET travel_eligible = true WHERE staff_id IN ('AB0016', 'AB0018')")
    # Teaching hours delivered before the app started counting (up to the period ending 4 Oct 2026).
    for sid, hours in (("AB0018", 850), ("AB0016", 1690)):
        op.execute(f"UPDATE staff SET teaching_hours_opening = {hours}, teaching_hours_as_of = '2026-10-04' "
                   f"WHERE staff_id = '{sid}' AND teaching_hours_opening = 0")


def downgrade() -> None:
    op.drop_column("staff", "travel_eligible")
