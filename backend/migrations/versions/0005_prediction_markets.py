"""Prediction markets desk: Kalshi 15-minute windows, 1-minute bars, BTC minutes, live ticks, signals, lab runs.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None
NUM = sa.Numeric(28, 10)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table("pm_windows",
                    sa.Column("ticker", sa.String(96), primary_key=True),
                    sa.Column("series", sa.String(32), nullable=False, index=True),
                    sa.Column("open_time", TS, nullable=False, index=True),
                    sa.Column("close_time", TS, nullable=False),
                    sa.Column("strike", NUM), sa.Column("result", sa.String(8)),
                    sa.Column("status", sa.String(24), nullable=False, server_default=""),
                    sa.Column("bars_loaded", sa.Boolean, nullable=False, server_default=sa.false()),
                    sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()))
    op.create_table("pm_bars",
                    sa.Column("ticker", sa.String(96), sa.ForeignKey("pm_windows.ticker", ondelete="CASCADE"), primary_key=True),
                    sa.Column("minute", sa.Integer, primary_key=True),
                    sa.Column("end_ts", TS, nullable=False),
                    *[sa.Column(c, NUM) for c in ("yes_bid", "yes_ask", "yes_bid_high", "yes_bid_low",
                                                  "yes_ask_high", "yes_ask_low", "volume")])
    op.create_table("pm_btc_minutes", sa.Column("ts", TS, primary_key=True), sa.Column("close", NUM, nullable=False))
    op.create_table("pm_ticks",
                    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
                    sa.Column("ticker", sa.String(96), nullable=False, index=True),
                    sa.Column("ts", TS, nullable=False, index=True),
                    sa.Column("sec_left", sa.Integer, nullable=False),
                    *[sa.Column(c, NUM) for c in ("yes_bid", "yes_ask", "btc", "fair_yes")])
    op.create_table("pm_signals",
                    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
                    sa.Column("ticker", sa.String(96), nullable=False, index=True),
                    sa.Column("kind", sa.String(24), nullable=False),
                    sa.Column("strategy_key", sa.String(160), nullable=False),
                    sa.Column("proven", sa.Boolean, nullable=False, server_default=sa.false()),
                    sa.Column("side", sa.String(4), nullable=False),
                    sa.Column("entry_price", NUM, nullable=False), sa.Column("fair", NUM), sa.Column("edge", NUM),
                    sa.Column("contracts", sa.Integer, nullable=False), sa.Column("max_loss", NUM, nullable=False),
                    sa.Column("take_profit", NUM), sa.Column("exit_by", TS, nullable=False),
                    sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
                    sa.Column("exit_price", NUM), sa.Column("paper_pnl", NUM),
                    sa.Column("detail", postgresql.JSONB, nullable=False, server_default="{}"),
                    sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
                    sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()))
    op.create_table("pm_lab_runs",
                    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
                    sa.Column("windows", sa.Integer, nullable=False),
                    sa.Column("result", postgresql.JSONB, nullable=False, server_default="{}"),
                    sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()))
    # Hard rule in the database: no setup may tell the CIO to hold within 2 minutes of close.
    op.execute("""
        CREATE FUNCTION pm_signal_exit_rule() RETURNS trigger AS $$
        DECLARE ct timestamptz;
        BEGIN
          SELECT close_time INTO ct FROM pm_windows WHERE ticker = NEW.ticker;
          IF ct IS NOT NULL AND NEW.exit_by > ct - interval '2 minutes' THEN
            RAISE EXCEPTION 'prediction setup % must exit at least 2 minutes before close', NEW.ticker;
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;
        CREATE TRIGGER pm_signal_exit_rule BEFORE INSERT OR UPDATE OF exit_by ON pm_signals
          FOR EACH ROW EXECUTE FUNCTION pm_signal_exit_rule();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS pm_signal_exit_rule ON pm_signals; DROP FUNCTION IF EXISTS pm_signal_exit_rule();")
    for t in ("pm_lab_runs", "pm_signals", "pm_ticks", "pm_btc_minutes", "pm_bars", "pm_windows"):
        op.drop_table(t)
