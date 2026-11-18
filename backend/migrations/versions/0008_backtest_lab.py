"""Backtest lab: hourly history, backtest runs, strategy proposals, paper-tested recommendations.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None
NUM = sa.Numeric(28, 10)
TS = sa.DateTime(timezone=True)
J = postgresql.JSONB


def upgrade() -> None:
    op.create_table("bt_candles",
                    sa.Column("product", sa.String(64), primary_key=True), sa.Column("ts", TS, primary_key=True),
                    sa.Column("asset", sa.String(16), nullable=False, index=True),
                    *[sa.Column(c, NUM, nullable=False) for c in ("open", "high", "low", "close")])
    op.create_table("bt_runs",
                    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
                    sa.Column("config_version", sa.Integer, nullable=False), sa.Column("config", J, nullable=False),
                    sa.Column("result", J, nullable=False), sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()))
    op.create_table("strategy_proposals",
                    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
                    sa.Column("key", sa.String(300), nullable=False, index=True), sa.Column("title", sa.Text, nullable=False),
                    sa.Column("overrides", J, nullable=False), sa.Column("base_config", J, nullable=False),
                    sa.Column("base_config_version", sa.Integer, nullable=False), sa.Column("evidence", J, nullable=False),
                    sa.Column("status", sa.String(16), nullable=False, server_default="PROPOSED"),
                    sa.Column("note", sa.Text, nullable=False, server_default=""), sa.Column("decided_by", sa.String(128)),
                    sa.Column("decided_at", TS), sa.Column("applied_config_version", sa.Integer),
                    sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
                    sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()))
    op.create_table("rec_sims",
                    sa.Column("memo_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("committee_memos.id"), primary_key=True),
                    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
                    sa.Column("asset", sa.String(16), nullable=False), sa.Column("direction", sa.String(8), nullable=False),
                    sa.Column("decision", sa.String(16), nullable=False), sa.Column("made_at", TS, nullable=False, index=True),
                    *[sa.Column(c, NUM, nullable=False) for c in ("entry", "stop", "target")],
                    sa.Column("status", sa.String(16), nullable=False), sa.Column("outcome", sa.String(16)), sa.Column("r", NUM),
                    sa.Column("result", J, nullable=False, server_default="{}"),
                    sa.Column("done", sa.Boolean, nullable=False, server_default=sa.false()),
                    sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()))


def downgrade() -> None:
    for t in ("rec_sims", "strategy_proposals", "bt_runs", "bt_candles"):
        op.drop_table(t)
