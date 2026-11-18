"""market views journal

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18 13:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'market_view',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('made_on', sa.Date(), nullable=False),
        sa.Column('horizon', sa.String(length=8), nullable=False),
        sa.Column('target_date', sa.Date(), nullable=False),
        sa.Column('series_code', sa.String(length=64), nullable=False),
        sa.Column('direction', sa.Integer(), nullable=False),
        sa.Column('low', sa.Float(), nullable=True),
        sa.Column('high', sa.Float(), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('reasons', sa.JSON(), nullable=False),
        sa.Column('rationale', sa.Text(), nullable=True),
        sa.Column('start_price', sa.Float(), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('daily_vol', sa.Float(), nullable=True),
        sa.Column('system', sa.JSON(), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('cancel_reason', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('market_view', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_market_view_target_date'), ['target_date'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('market_view', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_market_view_target_date'))
    op.drop_table('market_view')
