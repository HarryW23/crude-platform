"""login_failure：登錄失敗計數存數據庫（#36）

只加表。

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-22 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'login_failure',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username_key', sa.String(length=128), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_login_failure_username_key', 'login_failure', ['username_key'])
    op.create_index('ix_login_failure_at', 'login_failure', ['at'])


def downgrade() -> None:
    op.drop_index('ix_login_failure_at', table_name='login_failure')
    op.drop_index('ix_login_failure_username_key', table_name='login_failure')
    op.drop_table('login_failure')
