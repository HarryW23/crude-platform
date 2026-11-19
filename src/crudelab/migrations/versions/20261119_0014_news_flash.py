"""news flash

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-19 11:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'news_flash',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('ext_id', sa.String(length=64), nullable=False),
        sa.Column('time', sa.DateTime(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('url', sa.String(length=500), nullable=True),
        sa.Column('keywords', sa.JSON(), nullable=False),
        sa.Column('important', sa.Boolean(), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.Column('extracted_at', sa.DateTime(), nullable=True),
        sa.Column('drafts', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'ext_id', name=op.f('uq_news_flash_source')),
    )
    with op.batch_alter_table('news_flash', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_news_flash_time'), ['time'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('news_flash', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_news_flash_time'))
    op.drop_table('news_flash')
