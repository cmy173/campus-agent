"""Atomic task feedback receipts."""
from alembic import op
import sqlalchemy as sa

revision = "c61e294bd037"
down_revision = "f27a68db491c"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_feedback_receipts",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("learning_tasks.id"), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_agent_feedback_receipts_conversation_id", "agent_feedback_receipts", ["conversation_id"])


def downgrade():
    op.drop_index("ix_agent_feedback_receipts_conversation_id", table_name="agent_feedback_receipts")
    op.drop_table("agent_feedback_receipts")
