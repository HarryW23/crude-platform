"""入庫與讀取：所有數據點都從這裡進出，保證去重、修訂留痕、來源優先級一致。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog, DataSource, Observation, Series, utcnow


@dataclass(frozen=True)
class Obs:
    """connector 交給入庫層的標準數據點。"""
    series_code: str
    obs_date: date
    value: float
    source_ref: str | None = None


class IngestError(ValueError):
    pass


def write_observations(session: Session, observations: list[Obs], source_code: str,
                       batch_id: int | None = None, actor: str = "local") -> dict[str, int]:
    """寫入一批數據點。同序列、同日、同來源已有值時：相同則跳過，不同則更新並記審計。"""
    if session.get(DataSource, source_code) is None:
        raise IngestError(f"未知的數據源：{source_code}")
    codes = {o.series_code for o in observations}
    known = set(session.scalars(select(Series.code).where(Series.code.in_(codes)))) if codes else set()
    if codes - known:
        raise IngestError(f"未知的序列代碼：{', '.join(sorted(codes - known))}")

    latest: dict[tuple[str, date], Obs] = {}
    for o in observations:
        if not math.isfinite(o.value):
            raise IngestError(f"{o.series_code} {o.obs_date} 的值不是有效數字")
        latest[(o.series_code, o.obs_date)] = o          # 同批重複時後者為準

    existing = {
        (row.series_code, row.obs_date): row
        for row in session.scalars(select(Observation).where(
            Observation.source_code == source_code, Observation.series_code.in_(codes)))
    } if codes else {}

    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    for key, o in latest.items():
        row = existing.get(key)
        if row is None:
            session.add(Observation(series_code=o.series_code, obs_date=o.obs_date, value=o.value,
                                    source_code=source_code, batch_id=batch_id, source_ref=o.source_ref))
            stats["inserted"] += 1
        elif math.isclose(row.value, o.value, rel_tol=1e-12, abs_tol=1e-12):
            stats["unchanged"] += 1
        else:
            session.add(AuditLog(actor=actor, action="revise", entity="observation",
                                 entity_id=f"{o.series_code}@{o.obs_date}@{source_code}",
                                 detail={"old": row.value, "new": o.value, "batch_id": batch_id,
                                         "old_batch_id": row.batch_id, "old_ref": row.source_ref}))
            row.value, row.batch_id, row.source_ref, row.updated_at = o.value, batch_id, o.source_ref, utcnow()
            stats["updated"] += 1
    return stats


def load_points(session: Session, codes: list[str], start: date | None = None,
                end: date | None = None) -> pd.DataFrame:
    """讀取數據點（含來源信息），每個 序列×日期 只保留優先級最高的來源。

    回傳列：series_code, obs_date, value, source_code, batch_id, source_ref
    """
    cols = ["series_code", "obs_date", "value", "source_code", "batch_id", "source_ref"]
    if not codes:
        return pd.DataFrame(columns=cols)
    stmt = (select(Observation.series_code, Observation.obs_date, Observation.value, Observation.source_code,
                   Observation.batch_id, Observation.source_ref, DataSource.priority)
            .join(DataSource, DataSource.code == Observation.source_code)
            .where(Observation.series_code.in_(codes)))
    if start:
        stmt = stmt.where(Observation.obs_date >= start)
    if end:
        stmt = stmt.where(Observation.obs_date <= end)
    df = pd.DataFrame(session.execute(stmt).all(), columns=cols + ["priority"])
    if df.empty:
        return df[cols]
    df = df.sort_values(["series_code", "obs_date", "priority", "source_code"])
    return df.drop_duplicates(["series_code", "obs_date"], keep="first")[cols].reset_index(drop=True)


def load_series(session: Session, codes: list[str], start: date | None = None,
                end: date | None = None) -> dict[str, pd.Series]:
    """讀取多條序列，回傳 {代碼: 以日期為索引的 Series}。沒有數據的序列回傳空 Series。"""
    df = load_points(session, codes, start, end)
    out: dict[str, pd.Series] = {}
    for code in codes:
        part = df[df["series_code"] == code]
        out[code] = pd.Series(part["value"].to_numpy(dtype=float),
                              index=pd.DatetimeIndex(pd.to_datetime(part["obs_date"])), name=code).sort_index()
    return out
