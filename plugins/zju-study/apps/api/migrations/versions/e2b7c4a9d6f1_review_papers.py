"""复习卷：provider_configs / review_material_snapshots / review_papers / review_paper_revisions + jobs.phase（CR008）

- provider_configs：AI 服务配置（base_url/model/timeout/最近测试摘要）；密钥不在本表（本地 DPAPI 密钥存储）。
- review_material_snapshots：受理出卷时冻结的材料+备注快照（正文原文 JSON；fingerprint 供跨版本复查）。
- review_papers：试卷身份行（latest_revision=0 表示尚未生成成功，对列表不可见）。
- review_paper_revisions：按 revision 的完整产物（JSON 列存契约结构；重做不覆盖旧版）。
- jobs.phase：出卷作业阶段进度（review_generate 用；可空，旧行与旧类型为 null）。

全部为增量变更（新表 + 可空列），不动既有表语义与既有数据。

Revision ID: e2b7c4a9d6f1
Revises: b7e2d94c1a05
Create Date: 2026-10-11

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'e2b7c4a9d6f1'
down_revision = 'b7e2d94c1a05'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('phase', sa.String(length=32), nullable=True))

    op.create_table(
        'provider_configs',
        sa.Column('id', sa.String(length=128), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('protocol', sa.String(length=32), nullable=False, server_default='openai_chat_completions'),
        sa.Column('base_url', sa.String(length=1024), nullable=False),
        sa.Column('model', sa.String(length=256), nullable=False),
        sa.Column('timeout_seconds', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('last_tested_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_test_result', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'review_material_snapshots',
        sa.Column('id', sa.String(length=128), nullable=False),
        sa.Column('course_id', sa.String(length=128), nullable=False),
        sa.Column('engine_version', sa.String(length=32), nullable=False),
        sa.Column('fingerprint', sa.String(length=128), nullable=False),
        sa.Column('constraints', sa.JSON(), nullable=False),
        sa.Column('notes', sa.JSON(), nullable=False),
        sa.Column('items', sa.JSON(), nullable=False),
        sa.Column('missing', sa.JSON(), nullable=False),
        sa.Column('total_chars', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_review_material_snapshots_course_id'), 'review_material_snapshots', ['course_id'], unique=False)
    op.create_index(op.f('ix_review_material_snapshots_fingerprint'), 'review_material_snapshots', ['fingerprint'], unique=False)

    op.create_table(
        'review_papers',
        sa.Column('id', sa.String(length=128), nullable=False),
        sa.Column('course_id', sa.String(length=128), nullable=False),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=False),
        sa.Column('latest_revision', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_review_papers_course_id'), 'review_papers', ['course_id'], unique=False)

    op.create_table(
        'review_paper_revisions',
        sa.Column('paper_id', sa.String(length=128), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('revision_reason', sa.String(length=16), nullable=False),
        sa.Column('based_on_revision', sa.Integer(), nullable=True),
        sa.Column('snapshot_id', sa.String(length=128), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=False),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('engine_version', sa.String(length=32), nullable=False),
        sa.Column('fingerprint', sa.String(length=128), nullable=False),
        sa.Column('constraints', sa.JSON(), nullable=False),
        sa.Column('provider', sa.JSON(), nullable=False),
        sa.Column('blueprint', sa.JSON(), nullable=False),
        sa.Column('questions', sa.JSON(), nullable=False),
        sa.Column('warnings', sa.JSON(), nullable=False),
        sa.Column('summary', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['paper_id'], ['review_papers.id'], ),
        sa.ForeignKeyConstraint(['snapshot_id'], ['review_material_snapshots.id'], ),
        sa.PrimaryKeyConstraint('paper_id', 'revision'),
    )


def downgrade() -> None:
    op.drop_table('review_paper_revisions')
    op.drop_index(op.f('ix_review_papers_course_id'), table_name='review_papers')
    op.drop_table('review_papers')
    op.drop_index(op.f('ix_review_material_snapshots_fingerprint'), table_name='review_material_snapshots')
    op.drop_index(op.f('ix_review_material_snapshots_course_id'), table_name='review_material_snapshots')
    op.drop_table('review_material_snapshots')
    op.drop_table('provider_configs')
    op.drop_column('jobs', 'phase')
