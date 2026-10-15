"""jobs.warnings（任务非致命告警；B13 任务生成接入）

jobs 表新增 warnings JSON 列：任务生成告警（内容重复合并、估算回退等）对用户可见，
与 errors（失败）语义分离（CR003，兼容新增）。既有行由 server_default 填空数组。

Revision ID: b7e2d94c1a05
Revises: c3f8a1b27e04
Create Date: 2026-10-10

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'b7e2d94c1a05'
down_revision = 'c3f8a1b27e04'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('warnings', sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    op.drop_column('jobs', 'warnings')
