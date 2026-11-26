"""撤銷導入：import_batch 加 undo（撤銷用的記錄）、reverted_at / reverted_by，狀態多一個 reverted

undo 記的是這一批對「按期保存」表（observation_vintage）的改動：新加了哪些行、改了哪些行原來的值。
主表的改動不用另記：被覆蓋的舊值本來就在 observation_revision 裡。

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-26 12:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None

OLD = "status IN ('previewed', 'committed')"
NEW = "status IN ('previewed', 'committed', 'reverted')"


def upgrade() -> None:
    with op.batch_alter_table('import_batch') as batch:
        batch.add_column(sa.Column('undo', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('reverted_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('reverted_by', sa.String(64), nullable=True))
        batch.drop_constraint('ck_import_batch_status', type_='check')
        batch.create_check_constraint('ck_import_batch_status', NEW)


def downgrade() -> None:
    conn = op.get_bind()
    n = conn.execute(sa.text("SELECT COUNT(*) FROM import_batch WHERE status = 'reverted'")).scalar()
    if n:
        raise RuntimeError(f"有 {n} 個已撤銷的導入批次，舊版本不認識這個狀態，不能降級（數據沒有改動）")
    with op.batch_alter_table('import_batch') as batch:
        batch.drop_constraint('ck_import_batch_status', type_='check')
        batch.create_check_constraint('ck_import_batch_status', OLD)
        batch.drop_column('reverted_by')
        batch.drop_column('reverted_at')
        batch.drop_column('undo')
