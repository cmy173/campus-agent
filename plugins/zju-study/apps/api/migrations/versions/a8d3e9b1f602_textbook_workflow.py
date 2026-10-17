"""教材来源设置与逐本许可信息检查结果。"""
from alembic import op
import sqlalchemy as sa

revision = 'a8d3e9b1f602'
down_revision = 'f7c1d3e6a902'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('textbook_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('rights_check_enabled', sa.Boolean(), nullable=False),
        sa.Column('source_base_url', sa.String(1024), nullable=False))
    op.create_table('textbook_rights_reports',
        sa.Column('textbook_id', sa.String(128), sa.ForeignKey('textbooks.id'), primary_key=True),
        sa.Column('fingerprint', sa.String(128), nullable=False),
        sa.Column('result', sa.JSON(), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table('textbook_rights_reports')
    op.drop_table('textbook_settings')
