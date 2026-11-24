"""series.data_from：這天之前的數據不入庫（合約改過規格）

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-24 12:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('series', sa.Column('data_from', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('series') as batch:
        batch.drop_column('data_from')
