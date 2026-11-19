"""external calls and economic calendar

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-19 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'external_call',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('tool', sa.String(length=64), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('external_call', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_external_call_day'), ['day'], unique=False)
    op.create_table(
        'calendar_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('pub_time', sa.DateTime(), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('star', sa.Integer(), nullable=True),
        sa.Column('previous', sa.String(length=64), nullable=True),
        sa.Column('consensus', sa.String(length=64), nullable=True),
        sa.Column('actual', sa.String(length=64), nullable=True),
        sa.Column('revised', sa.String(length=64), nullable=True),
        sa.Column('affect', sa.String(length=64), nullable=True),
        sa.Column('consensus_at_release', sa.String(length=64), nullable=True),
        sa.Column('released_at', sa.DateTime(), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'key', name=op.f('uq_calendar_event_source')),
    )
    with op.batch_alter_table('calendar_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_calendar_event_pub_time'), ['pub_time'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('calendar_event', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_calendar_event_pub_time'))
    op.drop_table('calendar_event')
    with op.batch_alter_table('external_call', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_external_call_day'))
    op.drop_table('external_call')
