"""Atomic job creation receipts and conversation ownership."""
from alembic import op
import sqlalchemy as sa

revision = "f27a68db491c"
down_revision = "a13f47c908de"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_job_requests",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("course_id", sa.String(128), sa.ForeignKey("courses.id"), nullable=False),
        sa.Column("job_id", sa.String(128), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_agent_job_requests_conversation_id", "agent_job_requests", ["conversation_id"])


def downgrade():
    op.drop_index("ix_agent_job_requests_conversation_id", table_name="agent_job_requests")
    op.drop_table("agent_job_requests")
