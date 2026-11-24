"""series.bad_days：核對過是錯的日子，不入庫

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-24 15:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('series', sa.Column('bad_days', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('series') as batch:
        batch.drop_column('bad_days')
