"""llm calls and pauses

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-18 08:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'llm_call',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=False),
        sa.Column('purpose', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('llm_call', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_llm_call_started_at'), ['started_at'], unique=False)
    op.create_table(
        'llm_pause',
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('until', sa.DateTime(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('strikes', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('provider'),
    )


def downgrade() -> None:
    op.drop_table('llm_pause')
    with op.batch_alter_table('llm_call', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_llm_call_started_at'))
    op.drop_table('llm_call')
