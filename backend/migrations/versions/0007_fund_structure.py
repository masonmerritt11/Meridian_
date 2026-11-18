"""Fund structure: fund terms (management fee, performance allocation, high-water mark), management-company equity
grants with classes and vesting, and fee exemption per person. Seeds 2%/20% quarterly terms from now on (never
retroactive), founder fee exemption, and the founder's 100% Class A grant.

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None
NUM = sa.Numeric(28, 10)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("members", sa.Column("fee_exempt", sa.Boolean, nullable=False, server_default=sa.false()))
    op.execute("UPDATE members SET fee_exempt = true WHERE role = 'OWNER'")
    op.create_table("fund_terms",
                    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
                    sa.Column("mgmt_fee_pct", NUM, nullable=False), sa.Column("perf_alloc_pct", NUM, nullable=False),
                    sa.Column("hurdle_pct", NUM, nullable=False, server_default="0"),
                    sa.Column("high_water_mark", sa.Boolean, nullable=False, server_default=sa.true()),
                    sa.Column("perf_period", sa.String(16), nullable=False, server_default="QUARTERLY"),
                    sa.Column("reason", sa.Text, nullable=False, server_default=""),
                    sa.Column("changed_by", sa.String(128), nullable=False),
                    sa.Column("effective_at", TS, nullable=False, server_default=sa.func.now()))
    op.create_table("mgmt_equity_grants",
                    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
                    sa.Column("member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("members.id"), nullable=False, index=True),
                    sa.Column("equity_class", sa.String(1), nullable=False), sa.Column("pct", NUM, nullable=False),
                    sa.Column("voting", sa.Boolean, nullable=False, server_default=sa.false()),
                    sa.Column("vesting_months", sa.Integer, nullable=False, server_default="0"),
                    sa.Column("cliff_months", sa.Integer, nullable=False, server_default="0"),
                    sa.Column("vesting_start", TS, nullable=False, server_default=sa.func.now()),
                    sa.Column("note", sa.Text, nullable=False, server_default=""),
                    sa.Column("username", sa.String(128), nullable=False),
                    sa.Column("granted_at", TS, nullable=False, server_default=sa.func.now()),
                    sa.Column("revoked_at", TS))
    op.execute("""INSERT INTO fund_terms (mgmt_fee_pct, perf_alloc_pct, hurdle_pct, high_water_mark, perf_period, reason, changed_by)
                  VALUES (2, 20, 0, true, 'QUARTERLY', 'initial terms (2 and 20, high-water mark)', 'system')""")
    op.execute("""INSERT INTO mgmt_equity_grants (member_id, equity_class, pct, voting, note, username)
                  SELECT id, 'A', 100, true, 'Founder', 'system' FROM members WHERE role = 'OWNER'
                  ORDER BY created_at LIMIT 1""")


def downgrade() -> None:
    op.drop_table("mgmt_equity_grants")
    op.drop_table("fund_terms")
    op.drop_column("members", "fee_exempt")
