"""教材版本和经过核对的习题索引。"""
from alembic import op
import sqlalchemy as sa

revision = 'f7c1d3e6a902'
down_revision = 'e2b7c4a9d6f1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('textbooks',
        sa.Column('id', sa.String(128), primary_key=True),
        sa.Column('course_id', sa.String(128), sa.ForeignKey('courses.id'), nullable=False),
        sa.Column('title', sa.String(512), nullable=False),
        sa.Column('author', sa.String(256), nullable=False),
        sa.Column('edition', sa.String(128), nullable=False),
        sa.Column('isbn', sa.String(32), nullable=False),
        sa.Column('source_url', sa.String(1024), nullable=False),
        sa.Column('resource_id', sa.String(128), sa.ForeignKey('resources.id')),
        sa.Column('confirmed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_textbooks_course_id', 'textbooks', ['course_id'])
    op.create_table('textbook_exercises',
        sa.Column('textbook_id', sa.String(128), sa.ForeignKey('textbooks.id'), primary_key=True),
        sa.Column('chapter', sa.Integer(), primary_key=True),
        sa.Column('number', sa.String(32), primary_key=True),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('page', sa.Integer()),
        sa.Column('resource_version', sa.Integer()),
        sa.Column('confirmed', sa.Boolean(), nullable=False))


def downgrade():
    op.drop_table('textbook_exercises')
    op.drop_index('ix_textbooks_course_id', table_name='textbooks')
    op.drop_table('textbooks')
