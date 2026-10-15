"""Durable conversation-bound plan authorization and atomic mutation receipts."""
from alembic import op
import sqlalchemy as sa

revision = "a13f47c908de"
down_revision = "e2b7c4a9d6f1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_plan_actions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("plan_id", sa.String(128), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_agent_plan_actions_conversation_id", "agent_plan_actions", ["conversation_id"])


def downgrade():
    op.drop_index("ix_agent_plan_actions_conversation_id", table_name="agent_plan_actions")
    op.drop_table("agent_plan_actions")
