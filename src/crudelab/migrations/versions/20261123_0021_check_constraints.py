"""取值約束（CHECK）：狀態、類別、角色等列只能取規定的值；事件的方向判斷和機率限定範圍

代碼有 bug 寫進拼錯的狀態時，數據庫直接拒絕，而不是默默存下、讓頁面和統計出錯。
允許的值是 0.49.0 時 domain.py 的快照，寫死在這裡（遷移自包含，以後應用代碼怎麼改都不影響它）。

加約束前先檢查已有數據：有不合規的值就停下並列出是哪張表、哪一列、什麼值（整個遷移回滾，數據不變），
不會為了加約束去改或刪你的數據。

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-23 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None

CHECKED = {'commodity': {'category': ['crude', 'product', 'chem', 'freight', 'fx', 'fundamental', 'metals']},
 'data_source': {'kind': ['manual', 'excel', 'api'], 'status': ['active', 'reserved']},
 'series': {'kind': ['futures',
                     'swap',
                     'spot',
                     'diff',
                     'osp',
                     'freight',
                     'fx',
                     'stock',
                     'flow',
                     'ratio',
                     'position'],
            'frequency': ['daily', 'weekly', 'monthly', 'quarterly'],
            'module': ['spread', 'balance', 'geo', 'refinery', 'positioning', 'common']},
 'series_alias': {'origin': ['config', 'learned']},
 'import_batch': {'status': ['previewed', 'committed']},
 'event': {'category': ['conflict',
                        'chokepoint',
                        'sanction',
                        'opec_policy',
                        'outage',
                        'policy',
                        'demand',
                        'other'],
           'status': ['rumor', 'confirmed', 'ongoing', 'resolved', 'dismissed'],
           'origin': ['manual', 'llm']},
 'report': {'kind': ['daily', 'weekly', 'monthly']},
 'backtest_run': {'status': ['running', 'done', 'failed'], 'mode': ['observation', 'vintage']},
 'user_account': {'role': ['viewer', 'analyst', 'admin']},
 'job_run': {'trigger': ['schedule', 'manual'],
             'status': ['running', 'ok', 'skipped', 'failed', 'retry', 'retried']},
 'notification': {'event': ['failure', 'reminder', 'recovery', 'test']},
 'external_call': {'status': ['ok', 'failed']},
 'llm_call': {'purpose': ['chat', 'extract', 'narrative', 'test', 'models', 'other'],
              'status': ['running', 'ok', 'failed']}}

RANGES = {'event': [['bias', 'bias BETWEEN -2 AND 2'],
           ['probability', 'probability IS NULL OR (probability >= 0 AND probability <= 1)']]}


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _rules():
    for table, columns in CHECKED.items():
        for column, values in columns.items():
            yield table, f"ck_{table}_{column}", _in(column, values), column
    for table, rules in RANGES.items():
        for suffix, condition in rules:
            yield table, f"ck_{table}_{suffix}", condition, suffix


def upgrade() -> None:
    conn = op.get_bind()
    bad = []
    for table, _name, condition, column in _rules():
        rows = conn.execute(sa.text(f"SELECT {column}, COUNT(*) FROM {table} WHERE NOT ({condition}) "
                                    f"GROUP BY {column}")).all()
        bad += [f"{table}.{column} = {value!r}（{n} 行）" for value, n in rows]
    if bad:
        raise RuntimeError("已有數據裡有不合規的值，沒有加約束（數據沒有改動）。請先修正這些行：" + "；".join(bad))
    by_table = {}
    for table, name, condition, _column in _rules():
        by_table.setdefault(table, []).append((name, condition))
    for table, constraints in by_table.items():
        with op.batch_alter_table(table) as batch:
            for name, condition in constraints:
                batch.create_check_constraint(name, condition)


def downgrade() -> None:
    by_table = {}
    for table, name, _condition, _column in _rules():
        by_table.setdefault(table, []).append(name)
    for table, names in by_table.items():
        with op.batch_alter_table(table) as batch:
            for name in names:
                batch.drop_constraint(name, type_="check")
