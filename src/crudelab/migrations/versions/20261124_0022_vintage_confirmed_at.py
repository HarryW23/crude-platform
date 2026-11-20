"""observation_vintage.confirmed_at：每天自動取數確認同一個數到哪天

只加一列（可為空），不改已有數據。已有的行 confirmed_at 為空，等同「只有 vintage 那一期」：
舊數據裡每天取數被合併掉的確認日期本來就沒有記下，無法補回。

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-24 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('observation_vintage') as batch:
        batch.add_column(sa.Column('confirmed_at', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('observation_vintage') as batch:
        batch.drop_column('confirmed_at')
