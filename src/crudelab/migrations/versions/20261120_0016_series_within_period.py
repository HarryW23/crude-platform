"""series.within_period

用 ADD COLUMN 加列，不重建表：SQLite 重建表要刪掉舊表，而數據點、別名都引用著 series，外鍵檢查開著時刪不掉
（0.30.0 的寫法就是這樣在已有數據的庫上失敗的，並留下了 _alembic_tmp_series）。這裡先清掉那張殘留的臨時表。

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
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_series")          # 上次失敗留下的；原來的 series 表沒有被改動
    op.add_column('series', sa.Column('within_period', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('series') as batch:
        batch.drop_column('within_period')
