"""series.within_period

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-20 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('series') as batch:
        batch.add_column(sa.Column('within_period', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('series') as batch:
        batch.drop_column('within_period')
