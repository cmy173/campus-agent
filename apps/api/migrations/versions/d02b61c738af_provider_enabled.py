"""Saved provider enable state; configuring a service authorizes normal task context."""
from alembic import op
import sqlalchemy as sa

revision = "d02b61c738af"
down_revision = "c61e294bd037"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("provider_configs", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    op.drop_column("provider_configs", "enabled")
