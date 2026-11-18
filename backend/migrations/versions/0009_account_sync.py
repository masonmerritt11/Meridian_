"""Read-only Coinbase account sync: position source + fees, sync state.

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("source", sa.String(16), nullable=False, server_default="MANUAL"))
    op.add_column("positions", sa.Column("fees", sa.Numeric(28, 10), nullable=False, server_default="0"))
    op.create_table("account_sync_state",
                    sa.Column("id", sa.String(32), primary_key=True), sa.Column("fills_cursor", sa.String(64)),
                    sa.Column("seen_fill_ids", postgresql.JSONB, nullable=False, server_default="[]"),
                    sa.Column("last_ok", sa.DateTime(timezone=True)), sa.Column("last_error", sa.Text),
                    sa.Column("permissions", postgresql.JSONB, nullable=False, server_default="{}"),
                    sa.Column("pending", postgresql.JSONB, nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_table("account_sync_state")
    op.drop_column("positions", "fees")
    op.drop_column("positions", "source")
