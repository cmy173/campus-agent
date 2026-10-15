"""content_chunks（解析片段）

资源当前版本的文本片段表；chunk_count 徽标与 GET /resources/{id}/chunks 的持久层。

Revision ID: c9f4a7e21b58
Revises: b41c9e02f7a3
Create Date: 2026-10-09

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'c9f4a7e21b58'
down_revision = 'b41c9e02f7a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'content_chunks',
        sa.Column('id', sa.String(length=128), nullable=False),
        sa.Column('resource_id', sa.String(length=128), nullable=False),
        sa.Column('resource_version', sa.Integer(), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('locator', sa.JSON(), nullable=False),
        sa.Column('provenance', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_content_chunks_resource_id'), 'content_chunks', ['resource_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_content_chunks_resource_id'), table_name='content_chunks')
    op.drop_table('content_chunks')
