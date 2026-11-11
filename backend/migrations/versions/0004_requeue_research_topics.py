"""Re-queue research topics that failed because of the length-limit parsing bug on 2026-11-11.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE research_topics SET status = 'QUEUED', done_at = NULL WHERE status = 'FAILED'")


def downgrade() -> None:
    pass
