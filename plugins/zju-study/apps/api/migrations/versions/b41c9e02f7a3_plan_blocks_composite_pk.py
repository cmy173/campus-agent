"""plan_blocks composite pk (plan_id, id)

计划引擎块 ID（blk:{task_id}:{i}）只在计划内唯一，跨计划会重复；
将主键从 (id) 改为 (plan_id, id)。SQLite 不支持改主键 → 重建表并搬运数据。

Revision ID: b41c9e02f7a3
Revises: ff82c52062dc
Create Date: 2026-10-09

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'b41c9e02f7a3'
down_revision = 'ff82c52062dc'
branch_labels = None
depends_on = None

_COLUMNS = "plan_id, id, task_id, starts_at, ends_at, locked"


def _create_plan_blocks(name: str, *, composite_pk: bool) -> None:
    op.create_table(
        name,
        sa.Column('id', sa.String(length=128), nullable=False),
        sa.Column('plan_id', sa.String(length=128), nullable=False),
        sa.Column('task_id', sa.String(length=128), nullable=False),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('locked', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], ),
        sa.ForeignKeyConstraint(['task_id'], ['learning_tasks.id'], ),
        sa.PrimaryKeyConstraint('plan_id', 'id') if composite_pk else sa.PrimaryKeyConstraint('id'),
    )


def upgrade() -> None:
    op.drop_index(op.f('ix_plan_blocks_task_id'), table_name='plan_blocks')
    op.drop_index(op.f('ix_plan_blocks_plan_id'), table_name='plan_blocks')
    _create_plan_blocks('plan_blocks_new', composite_pk=True)
    op.execute(f"INSERT INTO plan_blocks_new ({_COLUMNS}) SELECT {_COLUMNS} FROM plan_blocks")
    op.drop_table('plan_blocks')
    op.rename_table('plan_blocks_new', 'plan_blocks')
    op.create_index(op.f('ix_plan_blocks_task_id'), 'plan_blocks', ['task_id'], unique=False)


def downgrade() -> None:
    # 旧结构无法表达跨计划重复的块 ID（如果存在重复，本降级会因主键冲突失败，属预期）
    op.drop_index(op.f('ix_plan_blocks_task_id'), table_name='plan_blocks')
    _create_plan_blocks('plan_blocks_old', composite_pk=False)
    op.execute(f"INSERT INTO plan_blocks_old ({_COLUMNS}) SELECT {_COLUMNS} FROM plan_blocks")
    op.drop_table('plan_blocks')
    op.rename_table('plan_blocks_old', 'plan_blocks')
    op.create_index(op.f('ix_plan_blocks_plan_id'), 'plan_blocks', ['plan_id'], unique=False)
    op.create_index(op.f('ix_plan_blocks_task_id'), 'plan_blocks', ['task_id'], unique=False)
