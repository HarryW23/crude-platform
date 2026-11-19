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
import threading
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd
from sqlalchemy import func, select
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


def _period_start(day: date, frequency: str) -> bool:
    if frequency == "quarterly":
        return day.day == 1 and day.month in (1, 4, 7, 10)
    return day.day == 1


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
    rows = {r.code: r for r in session.scalars(select(Series).where(Series.code.in_(codes), Series.active))}
    if codes - set(rows):
        raise IngestError(f"未知的序列代碼：{', '.join(sorted(codes - set(rows)))}")
    for o in observations:                       # 只在期間內有效的序列（例如官價）：日期必須是期間第一天
        row = rows[o.series_code]
        if row.within_period and not _period_start(o.obs_date, row.frequency):
            first = "季度第一天（1/4/7/10 月 1 日）" if row.frequency == "quarterly" else "當月 1 日"
            raise IngestError(f"{o.series_code} 的日期 {o.obs_date} 不對：這條序列的值只對一個期間生效，"
                              f"日期要記在它適用的那個期間的{first}（例如官價記裝船月，12 月裝船 → 2026-12-01），"
                              "不要用公布日期")

    latest: dict[tuple[str, date], Obs] = {}
    for o in observations:
        if isinstance(o.value, bool) or not isinstance(o.value, (int, float)) or not math.isfinite(o.value):
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
        df = df[df["value"].notna()]      # 找不到當時的舊值（不應發生）：寧可缺，也不讓空值佔掉優先級
    df = df.sort_values(["series_code", "obs_date", "priority", "source_code"])
    return df.drop_duplicates(["series_code", "obs_date"], keep="first")[POINT_COLUMNS].reset_index(drop=True)


_cache_lock = threading.Lock()
_load_lock = threading.Lock()
_cache: tuple[tuple, pd.DataFrame] | None = None


def _fingerprint(session: Session, codes: list[str]) -> tuple:
    """數據有沒有變：任何寫入、修訂、刪除都會改變條數、最新修改時間或最大編號；數據源優先級變了也算。"""
    count, last_update, last_id = session.execute(
        select(func.count(), func.max(Observation.updated_at), func.max(Observation.id))).one()
    revisions = session.scalar(select(func.count()).select_from(ObservationRevision))   # 每次改值都會多一條
    priorities = tuple(session.execute(select(DataSource.code, DataSource.priority).order_by(DataSource.code)).all())
    return (str(session.get_bind().url), tuple(codes), count, last_update, last_id, revisions, priorities)


def load_points_cached(session: Session, codes: list[str]) -> pd.DataFrame:
    """不帶 as_of 的 load_points，數據沒變時直接用上一次的結果（每個頁面請求都要讀全部數據點）。

    回傳的表是共用的：調用方只能讀，不能改。
    """
    global _cache
    key = _fingerprint(session, sorted(codes))
    with _cache_lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1]
    # 同一時間只讓一個請求去讀：頁面一次發好幾個請求，緩存失效時它們會同時去讀全表，互相拖慢十幾倍
    with _load_lock:
        with _cache_lock:
            if _cache is not None and _cache[0] == key:
                return _cache[1]
        points = load_points(session, codes)
        with _cache_lock:
            _cache = (key, points)
    return points


def to_series(points: pd.DataFrame, code: str) -> pd.Series:
    """從 load_points 的結果裡取出一條序列，以日期為索引。沒有數據時回傳空 Series。"""
    part = points[points["series_code"] == code]
    return pd.Series(part["value"].to_numpy(dtype=float),
                     index=pd.DatetimeIndex(pd.to_datetime(part["obs_date"])), name=code).sort_index()


def latest_market_date(session: Session, market_series: list[str], today: date | None = None) -> date | None:
    """「數據截至哪一天」：日度和周度序列裡、不晚於今天的最新日期。

    月度、季度數據記在期間第一天，還可能帶未來的預測期，不能用來回答這個問題。
    """
    if not market_series:
        return None
    return session.scalar(select(func.max(Observation.obs_date)).where(
        Observation.series_code.in_(market_series), Observation.obs_date <= (today or date.today())))
