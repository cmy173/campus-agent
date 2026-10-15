"""agent artifact/review receipts

Revision ID: a7c3e5b91d24
Revises: d02b61c738af
Create Date: 2026-10-15

Adds the two replay/ownership receipt tables introduced this round:

- `agent_artifact_receipts`: `artifacts.export` replay identity so the same trusted call id
  returns the same delivered file receipt (and different content conflicts) after a lost
  response, a new turn or a restart.
- `agent_review_receipts`: `reviews.generate` idempotency plus conversation ownership, so a
  repeated call never enqueues a second charged review job and another conversation cannot
  read or export the paper merely by knowing its id.

No existing table or column is modified.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7c3e5b91d24"
down_revision = "d02b61c738af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_artifact_receipts",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("request_key", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_artifact_receipts_conversation_id", "agent_artifact_receipts", ["conversation_id"])
    op.create_index("ix_agent_artifact_receipts_request_key", "agent_artifact_receipts", ["request_key"])

    op.create_table(
        "agent_review_receipts",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("request_key", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("course_id", sa.String(length=128), sa.ForeignKey("courses.id"), nullable=False),
        sa.Column("paper_id", sa.String(length=128), sa.ForeignKey("review_papers.id"), nullable=False),
        sa.Column("job_id", sa.String(length=128), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_review_receipts_conversation_id", "agent_review_receipts", ["conversation_id"])
    op.create_index("ix_agent_review_receipts_request_key", "agent_review_receipts", ["request_key"])


def downgrade() -> None:
    op.drop_index("ix_agent_review_receipts_request_key", table_name="agent_review_receipts")
    op.drop_index("ix_agent_review_receipts_conversation_id", table_name="agent_review_receipts")
    op.drop_table("agent_review_receipts")
    op.drop_index("ix_agent_artifact_receipts_request_key", table_name="agent_artifact_receipts")
    op.drop_index("ix_agent_artifact_receipts_conversation_id", table_name="agent_artifact_receipts")
    op.drop_table("agent_artifact_receipts")
