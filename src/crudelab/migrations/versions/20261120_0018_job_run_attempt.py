"""job_run.attempt（失敗重試）

唯一約束從 (job_code, slot) 改成 (job_code, slot, attempt)：同一個時點可以有重試。
SQLite 改約束要重建表；job_run 沒有被別的表引用，而且遷移在一個事務裡、期間關閉外鍵檢查（見 db.migrate）。

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-20 18:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('job_run') as batch:
        batch.add_column(sa.Column('attempt', sa.Integer(), nullable=False, server_default='1'))
        batch.drop_constraint('uq_job_run_job_code', type_='unique')
        batch.create_unique_constraint('uq_job_run_job_code', ['job_code', 'slot', 'attempt'])


def downgrade() -> None:
    with op.batch_alter_table('job_run') as batch:
        batch.drop_constraint('uq_job_run_job_code', type_='unique')
        batch.create_unique_constraint('uq_job_run_job_code', ['job_code', 'slot'])
        batch.drop_column('attempt')
