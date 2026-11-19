"""member_ledger.open_marks: open-position P&L recorded at each deposit/withdrawal (mark-to-market at flows).

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE member_ledger DISABLE TRIGGER member_ledger_append_only")
    op.add_column("member_ledger", sa.Column("open_marks", postgresql.JSONB, nullable=False, server_default="{}"))
    op.execute("ALTER TABLE member_ledger ENABLE TRIGGER member_ledger_append_only")


def downgrade() -> None:
    op.drop_column("member_ledger", "open_marks")
