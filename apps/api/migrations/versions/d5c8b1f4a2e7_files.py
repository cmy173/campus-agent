"""files（受控文件存储元数据）

blob 在 config.FILES_DIR（默认 var/files，gitignore）；本表只存元数据与受控访问标识（id=fil_*）。

Revision ID: d5c8b1f4a2e7
Revises: c9f4a7e21b58
Create Date: 2026-10-09

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'd5c8b1f4a2e7'
down_revision = 'c9f4a7e21b58'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'files',
        sa.Column('id', sa.String(length=128), nullable=False),
        sa.Column('original_name', sa.String(length=512), nullable=False),
        sa.Column('mime_type', sa.String(length=128), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('content_hash', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_files_content_hash'), 'files', ['content_hash'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_files_content_hash'), table_name='files')
    op.drop_table('files')
