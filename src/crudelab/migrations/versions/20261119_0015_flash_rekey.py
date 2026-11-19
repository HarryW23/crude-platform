"""news flash: re-key by url

金十的快訊沒有 id，0.29.0 用「時間 + 內容」的雜湊當編號，0.29.1 起改用網址（u: + 網址的雜湊）。
已經收錄的快訊要換成新編號，否則下次同步會把它們當成新的再收一次。
已經因此重複的：同一個網址只留一條（優先留抽取過草稿的，其次最早收錄的），抽取信息合併到留下的那條。
只改數據，不改表結構；自包含（不引用應用代碼），以後應用代碼怎麼改都不影響這個遷移。

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-19 20:00:00
"""
import hashlib

import sqlalchemy as sa
from alembic import op

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def _key(url: str) -> str:
    return "u:" + hashlib.sha256(url.strip().encode()).hexdigest()[:40]


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, ext_id, url, first_seen_at, extracted_at, drafts FROM news_flash "
        "WHERE source = 'jin10' AND url IS NOT NULL AND url <> ''")).mappings().all()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(_key(r["url"]), []).append(r)
    for key, group in groups.items():
        group.sort(key=lambda r: (r["extracted_at"] is None, -(r["drafts"] or 0), str(r["first_seen_at"]), r["id"]))
        keep, extra = group[0], group[1:]
        extracted = [r["extracted_at"] for r in group if r["extracted_at"] is not None]
        drafts = max((r["drafts"] or 0) for r in group)
        for r in extra:
            conn.execute(sa.text("DELETE FROM news_flash WHERE id = :id"), {"id": r["id"]})
        if keep["ext_id"] != key or extra:
            conn.execute(sa.text("UPDATE news_flash SET ext_id = :key, drafts = :drafts, "
                                 "extracted_at = :extracted WHERE id = :id"),
                         {"key": key, "drafts": drafts, "extracted": max(extracted) if extracted else None,
                          "id": keep["id"]})


def downgrade() -> None:
    pass        # 舊編號是內容雜湊，無法也不需要還原
