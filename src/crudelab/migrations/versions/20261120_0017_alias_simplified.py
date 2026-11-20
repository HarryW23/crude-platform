"""series_alias: normalise to simplified Chinese

別名的歸一化加了「繁體轉簡體」（config.normalize_alias），已經存著的別名（特別是導入時人工確認、自動記住的）
要按新規則重算 alias_norm，否則以後導入時對不上。重算後撞在一起的：同一序列只留一條；
指向不同序列的，留配置裡登記的（origin = config），都是自動記住的就留最早的那條。

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-20 14:00:00
"""
import re

import sqlalchemy as sa
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None

_NORM_RE = re.compile(r"[\s\-_/().,#:：（）|｜]+")      # 和 config.py 當時的寫法相同（遷移自包含，不引用它）


def _norm(text: str) -> str:
    from opencc import OpenCC
    return _NORM_RE.sub("", OpenCC("t2s").convert(str(text))).lower()


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, alias, alias_norm, origin FROM series_alias ORDER BY id")).mappings().all()
    keep: dict[str, dict] = {}
    drop: list[int] = []
    for r in rows:
        norm = _norm(r["alias"])
        held = keep.get(norm)
        if held is None:
            keep[norm] = {**r, "new": norm}
        elif held["origin"] != "config" and r["origin"] == "config":
            drop.append(held["id"])
            keep[norm] = {**r, "new": norm}
        else:
            drop.append(r["id"])
    for i in drop:
        conn.execute(sa.text("DELETE FROM series_alias WHERE id = :id"), {"id": i})
    # 先改成臨時值再改成新值：避免改的過程中和另一條的舊值撞上唯一約束
    for kept in keep.values():
        if kept["new"] != kept["alias_norm"]:
            conn.execute(sa.text("UPDATE series_alias SET alias_norm = :t WHERE id = :id"),
                         {"t": f"~{kept['id']}", "id": kept["id"]})
    for kept in keep.values():
        if kept["new"] != kept["alias_norm"]:
            conn.execute(sa.text("UPDATE series_alias SET alias_norm = :n WHERE id = :id"), {"n": kept["new"], "id": kept["id"]})


def downgrade() -> None:
    pass        # 簡體歸一化是單向的，舊值無法也不需要還原
