"""Member portal logins: users.member_id, must_change_password, disabled.

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("members.id"), nullable=True))
    op.add_column("users", sa.Column("must_change_password", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()))


def downgrade() -> None:
    for c in ("disabled", "must_change_password", "member_id"):
        op.drop_column("users", c)
