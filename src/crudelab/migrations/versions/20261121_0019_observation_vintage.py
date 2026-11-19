"""observation_vintage：機構預測按報告期保存（#23）

只加列、加表，不重建已有的表。

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-21 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('series', sa.Column('vintaged', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('import_batch', sa.Column('vintage', sa.Date(), nullable=True))
    op.create_table(
        'observation_vintage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('series_code', sa.String(length=64), nullable=False),
        sa.Column('obs_date', sa.Date(), nullable=False),
        sa.Column('source_code', sa.String(length=32), nullable=False),
        sa.Column('vintage', sa.Date(), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('batch_id', sa.Integer(), nullable=True),
        sa.Column('source_ref', sa.String(length=256), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['batch_id'], ['import_batch.id'], name=op.f('fk_observation_vintage_batch_id_import_batch')),
        sa.ForeignKeyConstraint(['series_code'], ['series.code'], name=op.f('fk_observation_vintage_series_code_series')),
        sa.ForeignKeyConstraint(['source_code'], ['data_source.code'],
                                name=op.f('fk_observation_vintage_source_code_data_source')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_observation_vintage')),
        sa.UniqueConstraint('series_code', 'obs_date', 'source_code', 'vintage',
                            name=op.f('uq_observation_vintage_series_code')),
    )
    op.create_index('ix_observation_vintage_series_code_vintage', 'observation_vintage', ['series_code', 'vintage'])


def downgrade() -> None:
    op.drop_index('ix_observation_vintage_series_code_vintage', table_name='observation_vintage')
    op.drop_table('observation_vintage')
    with op.batch_alter_table('import_batch') as batch:
        batch.drop_column('vintage')
    with op.batch_alter_table('series') as batch:
        batch.drop_column('vintaged')
