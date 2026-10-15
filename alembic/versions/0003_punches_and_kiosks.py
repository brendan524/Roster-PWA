"""punches, kiosks, voids; PIN lockout on staff

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("staff", sa.Column("pin_failed", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("staff", sa.Column("pin_locked_until", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "kiosks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("location_type", sa.String(10), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("staff.staff_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pin_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pin_locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("location_type IN ('onshore', 'offshore')", name="ck_kiosk_location_type"),
    )

    op.create_table(
        "punches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("client_id", sa.String(64), nullable=False, unique=True),
        sa.Column("staff_id", sa.String(32), sa.ForeignKey("staff.staff_id"), nullable=False),
        sa.Column("kind", sa.String(3), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("kiosk_id", sa.Integer(), sa.ForeignKey("kiosks.id"), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("device_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clock_offset_s", sa.Integer(), nullable=True),
        sa.Column("queued", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("accuracy_m", sa.Float(), nullable=True),
        sa.Column("geo_status", sa.String(12), nullable=False),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("staff.staff_id"), nullable=True),
        sa.Column("reason", sa.String(300), nullable=False, server_default=""),
        sa.Column("user_agent", sa.String(255), nullable=False, server_default=""),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.CheckConstraint("kind IN ('in', 'out')", name="ck_punch_kind"),
        sa.CheckConstraint("source IN ('personal', 'kiosk', 'admin')", name="ck_punch_source"),
        sa.CheckConstraint("source != 'kiosk' OR kiosk_id IS NOT NULL", name="ck_punch_kiosk"),
        sa.CheckConstraint("source != 'admin' OR (created_by IS NOT NULL AND reason != '')", name="ck_punch_admin_reason"),
    )
    op.create_index("ix_punches_staff_id", "punches", ["staff_id"])
    op.create_index("ix_punches_occurred_at", "punches", ["occurred_at"])

    op.create_table(
        "punch_voids",
        sa.Column("punch_id", sa.BigInteger(), sa.ForeignKey("punches.id"), primary_key=True),
        sa.Column("voided_by", sa.String(32), sa.ForeignKey("staff.staff_id"), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("reason != ''", name="ck_void_reason"),
    )

    # Append-only at the database level too: the app's DB user can't change or remove punches.
    op.execute("""
        CREATE FUNCTION punches_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'punches are append-only; void and re-enter instead';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER punches_no_update_delete BEFORE UPDATE OR DELETE ON punches
        FOR EACH ROW EXECUTE FUNCTION punches_append_only();
    """)
    op.execute("""
        CREATE TRIGGER punch_voids_no_update_delete BEFORE UPDATE OR DELETE ON punch_voids
        FOR EACH ROW EXECUTE FUNCTION punches_append_only();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS punch_voids_no_update_delete ON punch_voids")
    op.execute("DROP TRIGGER IF EXISTS punches_no_update_delete ON punches")
    op.execute("DROP FUNCTION IF EXISTS punches_append_only()")
    op.drop_table("punch_voids")
    op.drop_table("punches")
    op.drop_table("kiosks")
    op.drop_column("staff", "pin_locked_until")
    op.drop_column("staff", "pin_failed")
