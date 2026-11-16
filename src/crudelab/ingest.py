"""入庫與讀取：所有數據點都從這裡進出。

寫入保證：
  - 只接受配置裡登記的序列和數據源
  - 同一 序列 × 日期 × 來源 只有一個當前值；值相同則跳過，不同則改寫，舊值存進 observation_revision
讀取保證：
  - 同一 序列 × 日期 有多個來源時，取優先級最高（priority 最小）的來源
  - 給了 as_of 就還原那個時點當時看到的數據
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DataSource, Observation, ObservationRevision, Series, utcnow

POINT_COLUMNS = ["series_code", "obs_date", "value", "source_code", "batch_id", "source_ref"]


@dataclass(frozen=True)
class Obs:
    """connector 交給入庫層的標準數據點。"""
    series_code: str
    obs_date: date
    value: float
    source_ref: str | None = None


class IngestError(ValueError):
    pass


def write_observations(session: Session, observations: list[Obs], source_code: str, *,
                       batch_id: int | None = None, actor: str = "local",
                       now: datetime | None = None) -> dict[str, int]:
    """寫入一批數據點，回傳 {inserted, updated, unchanged}。now 只在測試時傳入。"""
    now = now or utcnow()
    if session.get(DataSource, source_code) is None:
        raise IngestError(f"未知的數據源：{source_code}")
    codes = {o.series_code for o in observations}
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    if not codes:
        return stats
    known = set(session.scalars(select(Series.code).where(Series.code.in_(codes), Series.active)))
    if codes - known:
        raise IngestError(f"未知的序列代碼：{', '.join(sorted(codes - known))}")

    latest: dict[tuple[str, date], Obs] = {}
    for o in observations:
        if not isinstance(o.value, (int, float)) or not math.isfinite(o.value):
            raise IngestError(f"{o.series_code} {o.obs_date} 的值不是有效數字")
        latest[(o.series_code, o.obs_date)] = o          # 同一批裡重複時以後面的為準

    existing = {(row.series_code, row.obs_date): row for row in session.scalars(
        select(Observation).where(Observation.source_code == source_code, Observation.series_code.in_(codes)))}

    for key, o in latest.items():
        row = existing.get(key)
        if row is None:
            session.add(Observation(series_code=o.series_code, obs_date=o.obs_date, value=float(o.value),
                                    source_code=source_code, batch_id=batch_id, source_ref=o.source_ref,
                                    created_at=now, updated_at=now))
            stats["inserted"] += 1
        elif math.isclose(row.value, o.value, rel_tol=1e-12, abs_tol=1e-12):
            stats["unchanged"] += 1
        else:
            session.add(ObservationRevision(
                series_code=row.series_code, obs_date=row.obs_date, source_code=source_code, value=row.value,
                batch_id=row.batch_id, source_ref=row.source_ref, recorded_at=row.updated_at,
                replaced_at=now, replaced_by=actor))
            row.value, row.batch_id, row.source_ref, row.updated_at = float(o.value), batch_id, o.source_ref, now
            stats["updated"] += 1
    return stats


def load_points(session: Session, codes: list[str], start: date | None = None, end: date | None = None,
                as_of: datetime | None = None) -> pd.DataFrame:
    """讀取數據點，每個 序列 × 日期 只保留優先級最高的來源。回傳列見 POINT_COLUMNS。"""
    if not codes:
        return pd.DataFrame(columns=POINT_COLUMNS)
    stmt = (select(Observation.series_code, Observation.obs_date, Observation.value, Observation.source_code,
                   Observation.batch_id, Observation.source_ref, Observation.updated_at, DataSource.priority)
            .join(DataSource, DataSource.code == Observation.source_code)
            .where(Observation.series_code.in_(codes)))
    if start:
        stmt = stmt.where(Observation.obs_date >= start)
    if end:
        stmt = stmt.where(Observation.obs_date <= end)
    if as_of is not None:
        stmt = stmt.where(Observation.created_at <= as_of)
    df = pd.DataFrame(session.execute(stmt).all(), columns=[*POINT_COLUMNS, "updated_at", "priority"])
    if df.empty:
        return df[POINT_COLUMNS]

    if as_of is not None and (df["updated_at"] > as_of).any():
        # 當前值是 as_of 之後才寫入的：換回 as_of 當時的值，即 as_of 之後第一次被取代的那個舊值
        revisions = pd.DataFrame(session.execute(
            select(ObservationRevision.series_code, ObservationRevision.obs_date, ObservationRevision.source_code,
                   ObservationRevision.value, ObservationRevision.batch_id, ObservationRevision.source_ref,
                   ObservationRevision.replaced_at)
            .where(ObservationRevision.series_code.in_(codes), ObservationRevision.replaced_at > as_of)).all(),
            columns=["series_code", "obs_date", "source_code", "old_value", "old_batch_id", "old_ref", "replaced_at"])
        first = revisions.sort_values("replaced_at").drop_duplicates(["series_code", "obs_date", "source_code"])
        df = df.merge(first, how="left", on=["series_code", "obs_date", "source_code"])
        stale = df["updated_at"] > as_of
        df.loc[stale, "value"] = df.loc[stale, "old_value"]
        df.loc[stale, "batch_id"] = df.loc[stale, "old_batch_id"]
        df.loc[stale, "source_ref"] = df.loc[stale, "old_ref"]

    df = df.sort_values(["series_code", "obs_date", "priority", "source_code"])
    return df.drop_duplicates(["series_code", "obs_date"], keep="first")[POINT_COLUMNS].reset_index(drop=True)


def to_series(points: pd.DataFrame, code: str) -> pd.Series:
    """從 load_points 的結果裡取出一條序列，以日期為索引。沒有數據時回傳空 Series。"""
    part = points[points["series_code"] == code]
    return pd.Series(part["value"].to_numpy(dtype=float),
                     index=pd.DatetimeIndex(pd.to_datetime(part["obs_date"])), name=code).sort_index()
