"""sync_cursors（每连接×课程的增量游标）

游标不透明（A4 §3）；本表只存适配器分配的字符串，服务端不解析其结构。

Revision ID: c3f8a1b27e04
Revises: d5c8b1f4a2e7
Create Date: 2026-10-09

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'c3f8a1b27e04'
down_revision = 'd5c8b1f4a2e7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'sync_cursors',
        sa.Column('connection_id', sa.String(length=128), nullable=False),
        sa.Column('course_id', sa.String(length=128), nullable=False),
        sa.Column('cursor', sa.String(length=512), nullable=False),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['connections.id']),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id']),
        sa.PrimaryKeyConstraint('connection_id', 'course_id'),
    )


def downgrade() -> None:
    op.drop_table('sync_cursors')
