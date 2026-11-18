"""Members: ownership of the account by unit accounting. Seeds Mason ($600) and one member ($100 = 1/7) from the
original $700 when the founding capital is exactly $700 and no members exist yet.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None
NUM = sa.Numeric(28, 10)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table("members",
                    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
                    sa.Column("name", sa.String(120), nullable=False),
                    sa.Column("role", sa.String(16), nullable=False, server_default="MEMBER"),
                    sa.Column("note", sa.Text, nullable=False, server_default=""),
                    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
                    sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()))
    op.create_table("member_ledger",
                    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
                    sa.Column("member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("members.id"), nullable=False, index=True),
                    sa.Column("kind", sa.String(16), nullable=False),
                    sa.Column("amount", NUM, nullable=False), sa.Column("units", NUM, nullable=False),
                    sa.Column("nav_per_unit", NUM, nullable=False), sa.Column("equity_before", NUM, nullable=False),
                    sa.Column("original", sa.Boolean, nullable=False, server_default=sa.false()),
                    sa.Column("note", sa.Text, nullable=False, server_default=""),
                    sa.Column("username", sa.String(128), nullable=False),
                    sa.Column("effective_at", TS, nullable=False, server_default=sa.func.now()),
                    sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()))
    op.execute("""
        CREATE FUNCTION member_ledger_append_only() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'member_ledger is append-only; record an offsetting entry instead'; END $$ LANGUAGE plpgsql;
        CREATE TRIGGER member_ledger_append_only BEFORE UPDATE OR DELETE ON member_ledger
          FOR EACH ROW EXECUTE FUNCTION member_ledger_append_only();
    """)
    op.execute("""
        DO $$
        DECLARE founding numeric; started timestamptz; mason uuid := gen_random_uuid(); m1 uuid := gen_random_uuid();
        BEGIN
          IF EXISTS (SELECT 1 FROM members) THEN RETURN; END IF;
          SELECT COALESCE(SUM(CASE WHEN kind = 'WITHDRAW' THEN -amount ELSE amount END), 0), MIN(created_at)
            INTO founding, started FROM capital_events;
          IF founding <> 700 THEN RETURN; END IF;
          INSERT INTO members (id, name, role, note) VALUES
            (mason, 'Mason', 'OWNER', 'Founder'), (m1, 'Member 1', 'MEMBER', 'Rename me: 1/7 of the original $700');
          INSERT INTO member_ledger (member_id, kind, amount, units, nav_per_unit, equity_before, original, note, username, effective_at)
          VALUES (mason, 'CONTRIBUTION', 600, 600, 1, 0, true, 'Share of the original $700', 'system', started),
                 (m1, 'CONTRIBUTION', 100, 100, 1, 600, true, 'Share of the original $700 (1/7)', 'system', started);
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS member_ledger_append_only ON member_ledger; DROP FUNCTION IF EXISTS member_ledger_append_only();")
    op.drop_table("member_ledger")
    op.drop_table("members")
