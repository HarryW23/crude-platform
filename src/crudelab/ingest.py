"""入庫與讀取：所有數據點都從這裡進出。

寫入保證：
  - 只接受配置裡登記的序列和數據源
  - 同一 序列 × 日期 × 來源 只有一個當前值；值相同則跳過，不同則改寫，舊值存進 observation_revision
  - 按報告期保存的序列（vintaged，機構預測）：每一期記進 observation_vintage（數字沒變不多記）；
    主表只在這一期不比已有的舊時才更新，所以補錄舊報告不會蓋掉新一期
讀取保證：
  - 同一 序列 × 日期 有多個來源時，取優先級最高（priority 最小）的來源
  - 給了 as_of 就還原那個時點當時看到的數據
  - vintage_points：按期保存的序列在某天之前發佈的最新一期
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from .models import DataSource, Observation, ObservationRevision, ObservationVintage, Series, utcnow

POINT_COLUMNS = ["series_code", "obs_date", "value", "source_code", "batch_id", "source_ref"]


@dataclass(frozen=True)
class Obs:
    """connector 交給入庫層的標準數據點。"""
    series_code: str
    obs_date: date
    value: float
    source_ref: str | None = None
    vintage: date | None = None          # 機構報告的發佈日期；只用於按期保存的序列，不給就是入庫當天


class IngestError(ValueError):
    pass


def _period_start(day: date, frequency: str) -> bool:
    if frequency == "quarterly":
        return day.day == 1 and day.month in (1, 4, 7, 10)
    return day.day == 1


def write_observations(session: Session, observations: list[Obs], source_code: str, *,
                       batch_id: int | None = None, actor: str = "local",
                       now: datetime | None = None, undo: dict | None = None) -> dict[str, int]:
    """寫入一批數據點，回傳 {inserted, updated, unchanged}。now 只在測試時傳入。
    undo：給了就記下這批對按期保存表（observation_vintage）的改動，撤銷導入時用（見 imports.revert_batch）；
    主表的改動不用另記，被覆蓋的舊值都在 observation_revision 裡。"""
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

    vintaged = {code for code, row in rows.items() if row.vintaged}
    if vintaged:
        today = now.date()
        by_issue: dict[tuple[str, date, date], Obs] = {}      # 同一批裡可以有同一個點的好幾期：按 期 去重
        for o in observations:
            if o.series_code in vintaged:
                by_issue[(o.series_code, o.obs_date, o.vintage or today)] = o
        ordered = sorted(by_issue.values(), key=lambda o: o.vintage or today)
        for o in ordered:                                      # 主表的候選：這一批裡最新的一期
            latest[(o.series_code, o.obs_date)] = o
        stats.update(_write_vintages(session, ordered, source_code, batch_id, now, latest, undo))

    existing = {(obs.series_code, obs.obs_date): obs for obs in session.scalars(
        select(Observation).where(Observation.source_code == source_code, Observation.series_code.in_(codes)))}

    new_rows: list[dict] = []
    for key, o in latest.items():
        current = existing.get(key)
        if current is None:
            new_rows.append({"series_code": o.series_code, "obs_date": o.obs_date, "value": float(o.value),
                             "source_code": source_code, "batch_id": batch_id, "source_ref": o.source_ref,
                             "created_at": now, "updated_at": now})
            stats["inserted"] += 1
        elif math.isclose(current.value, o.value, rel_tol=1e-12, abs_tol=1e-12):
            stats["unchanged"] += 1
        else:
            session.add(ObservationRevision(
                series_code=current.series_code, obs_date=current.obs_date, source_code=source_code,
                value=current.value, batch_id=current.batch_id, source_ref=current.source_ref,
                recorded_at=current.updated_at,
                replaced_at=now, replaced_by=actor))
            current.value, current.batch_id = float(o.value), batch_id
            current.source_ref, current.updated_at = o.source_ref, now
            stats["updated"] += 1
    if new_rows:            # 新點批量插入：十年的寬表有二十多萬個點，一個個建 ORM 對象要十幾秒
        session.execute(insert(Observation), new_rows)
    return stats


def _write_vintages(session: Session, observations: list[Obs], source_code: str, batch_id: int | None,
                    now: datetime, latest: dict, undo: dict | None = None) -> dict[str, int]:
    """把按期保存的序列寫進 observation_vintage，並決定主表取哪個數（整批處理完後，取每個點最新一期的值）。

    兩種來源分開處理（見 models.ObservationVintage）：
      帶報告期（補錄報告、導入時填了發佈日期）：每一期都記一行，數字沒變也記。後來補錄更早的一期時，
          靠這些行知道之後那期確認過什麼，補錄不會覆蓋之後已經確認的數字。
      不帶報告期（每天自動取數，期號 = 今天）：和最新一行數字相同時不加行，只把那行的 confirmed_at 延到今天，
          表示這段期間每天取到的都是這個數。
    同一期再寫一次：數字不同就更正那一期。
    補錄的一期落在某段「每天取數確認」的期間中間、數字又不同時：那一期顯示補錄的數，第二天起恢復那段期間確認的數
    （每天取數的確認日期只記了起止，中間按每天都確認過處理）。
    """
    out = {"vintages": 0, "older_vintage": 0}
    if not observations:
        return out
    today = now.date()
    for o in observations:
        if o.vintage is not None and o.vintage > today:
            raise IngestError(f"{o.series_code} 的報告期 {o.vintage} 晚於今天：期號是報告的發佈日期，不能是未來")
    codes = {o.series_code for o in observations}
    history: dict[tuple[str, date], list[ObservationVintage]] = {}
    for existing in session.scalars(select(ObservationVintage).where(
            ObservationVintage.source_code == source_code, ObservationVintage.series_code.in_(codes))):
        history.setdefault((existing.series_code, existing.obs_date), []).append(existing)
    one_day = timedelta(days=1)
    added: list[ObservationVintage] = []
    before_change: dict[int, tuple[ObservationVintage, dict]] = {}      # 改動前的值（每行只記第一次）

    def keep(row: ObservationVintage) -> None:
        """記下這一行改動前的值，撤銷時還原。這一批自己新加的行不用記（撤銷時整行刪掉）。"""
        if undo is not None and row not in added and id(row) not in before_change:
            before_change[id(row)] = (row, {"value": row.value, "batch_id": row.batch_id, "source_ref": row.source_ref,
                                            "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None})

    def add(o: Obs, vintage: date, value: float, *, ref: str | None, batch: int | None,
            confirmed: date | None = None) -> ObservationVintage:
        row = ObservationVintage(series_code=o.series_code, obs_date=o.obs_date, source_code=source_code,
                                 vintage=vintage, value=value, batch_id=batch, source_ref=ref, created_at=now,
                                 confirmed_at=confirmed)
        session.add(row)
        added.append(row)
        history.setdefault((o.series_code, o.obs_date), []).append(row)
        out["vintages"] += 1
        return row

    touched: dict[tuple[str, date], list[date]] = {}
    for o in observations:
        key, v, implicit = (o.series_code, o.obs_date), o.vintage or today, o.vintage is None
        touched.setdefault(key, []).append(v)
        rows = sorted(history.get(key, []), key=lambda r: r.vintage)
        same = next((r for r in rows if r.vintage == v), None)
        after = [r for r in rows if r.vintage > v]
        if same is not None:
            if not _close(same.value, o.value):
                keep(same)
                rest, old_value, old_ref, old_batch = same.confirmed_at, same.value, same.source_ref, same.batch_id
                same.value, same.batch_id, same.source_ref = float(o.value), batch_id, o.source_ref
                out["vintages"] += 1
                if rest is not None and rest > v:              # 這一行原本是一段確認期間的開頭：只改這一天
                    same.confirmed_at = None
                    if not after or after[0].vintage > v + one_day:
                        add(o, v + one_day, old_value, ref=old_ref, batch=old_batch,
                            confirmed=rest if rest > v + one_day else None)
            continue
        before = [r for r in rows if r.vintage < v]
        prev = before[-1] if before else None
        if implicit and prev is not None and not after and _close(prev.value, o.value):
            keep(prev)
            prev.confirmed_at = max(prev.confirmed_at or prev.vintage, v)      # 每天取數，數字沒變：延長確認期
            continue
        row = add(o, v, float(o.value), ref=o.source_ref, batch=batch_id)
        if prev is not None and prev.confirmed_at is not None and prev.confirmed_at > v:
            keep(prev)
            rest = prev.confirmed_at                                          # 補錄落在一段確認期間的中間
            prev.confirmed_at = v - one_day if v - one_day > prev.vintage else None
            if _close(prev.value, o.value):
                row.confirmed_at = rest                                       # 同一個數：這一行接着確認下去
            elif not after or after[0].vintage > v + one_day:
                add(o, v + one_day, prev.value, ref=prev.source_ref, batch=prev.batch_id,
                    confirmed=rest if rest > v + one_day else None)           # 第二天起恢復原來確認的數

    if undo is not None:
        session.flush()                                   # 拿到新行的編號
        undo.setdefault("vintage_added", []).extend(r.id for r in added)
        undo.setdefault("vintage_modified", []).extend(
            {"id": row.id, **old} for row, old in before_change.values() if row not in added)
    for key, vintages in touched.items():                 # 整批處理完再定主表：取這個點最新的一期
        newest = max(history[key], key=lambda r: r.vintage)
        latest[key] = Obs(key[0], key[1], newest.value, newest.source_ref)
        out["older_vintage"] += sum(1 for v in vintages if v < newest.vintage)
    return out


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


def vintage_points(session: Session, codes: list[str], on: date) -> pd.DataFrame:
    """按期保存的序列在 on 那天（含）之前發佈的最新一期。沒有任何期記錄的序列不在結果裡。回傳列見 POINT_COLUMNS。"""
    if not codes:
        return pd.DataFrame(columns=POINT_COLUMNS)
    stmt = (select(ObservationVintage.series_code, ObservationVintage.obs_date, ObservationVintage.value,
                   ObservationVintage.source_code, ObservationVintage.batch_id, ObservationVintage.source_ref,
                   ObservationVintage.vintage, DataSource.priority)
            .join(DataSource, DataSource.code == ObservationVintage.source_code)
            .where(ObservationVintage.series_code.in_(codes), ObservationVintage.vintage <= on))
    df = pd.DataFrame(session.execute(stmt).all(), columns=[*POINT_COLUMNS, "vintage", "priority"])
    if df.empty:
        return df[POINT_COLUMNS]
    df = df.sort_values("vintage").drop_duplicates(["series_code", "obs_date", "source_code"], keep="last")
    df = df.sort_values(["series_code", "obs_date", "priority", "source_code"])
    return df.drop_duplicates(["series_code", "obs_date"], keep="first")[POINT_COLUMNS].reset_index(drop=True)


def vintaged_with_history(session: Session, codes: list[str]) -> list[str]:
    """這些序列裡，已經有期記錄的（只有它們能按期還原）。"""
    if not codes:
        return []
    return list(session.scalars(select(ObservationVintage.series_code).distinct()
                                .where(ObservationVintage.series_code.in_(codes))))


def vintage_dates(session: Session, codes: list[str]) -> list[date]:
    """這些序列所有報告期（發佈日期），由早到晚。"""
    if not codes:
        return []
    return sorted(session.scalars(select(ObservationVintage.vintage).distinct()
                                  .where(ObservationVintage.series_code.in_(codes))))


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
    if as_of is not None:
        df = _with_removed_points(session, df, codes, start, end, as_of)
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


def _with_removed_points(session: Session, df: pd.DataFrame, codes: list[str], start: date | None,
                         end: date | None, as_of: datetime) -> pd.DataFrame:
    """as_of 當時存在、之後被移除的數據點（例如按配置的 bad_days 清理掉的）：主表裡已經沒有了，只剩修訂記錄。
    修訂記錄裡 as_of 之後第一次被取代的那一條，如果是 as_of 之前就記錄的，就是當時看得到的值。"""
    stmt = (select(ObservationRevision.series_code, ObservationRevision.obs_date, ObservationRevision.source_code,
                   ObservationRevision.value, ObservationRevision.batch_id, ObservationRevision.source_ref,
                   ObservationRevision.recorded_at, ObservationRevision.replaced_at, DataSource.priority)
            .join(DataSource, DataSource.code == ObservationRevision.source_code)
            .where(ObservationRevision.series_code.in_(codes), ObservationRevision.replaced_at > as_of))
    if start:
        stmt = stmt.where(ObservationRevision.obs_date >= start)
    if end:
        stmt = stmt.where(ObservationRevision.obs_date <= end)
    revisions = pd.DataFrame(session.execute(stmt).all(), columns=[
        "series_code", "obs_date", "source_code", "value", "batch_id", "source_ref", "recorded_at", "replaced_at",
        "priority"])
    if revisions.empty:
        return df
    first = revisions.sort_values("replaced_at").drop_duplicates(["series_code", "obs_date", "source_code"])
    present = set(zip(df["series_code"], df["obs_date"], df["source_code"], strict=True))
    gone = pd.Series([k not in present for k in zip(first["series_code"], first["obs_date"], first["source_code"],
                                                    strict=True)], index=first.index)
    removed = first[gone & (first["recorded_at"] <= as_of)]
    if removed.empty:
        return df
    removed = removed.assign(updated_at=removed["recorded_at"])[[*POINT_COLUMNS, "updated_at", "priority"]]
    return removed if df.empty else pd.concat([df, removed], ignore_index=True)


_cache_lock = threading.Lock()
_load_lock = threading.Lock()
_cache: tuple[tuple, pd.DataFrame] | None = None


def data_fingerprint(session: Session, codes: list[str]) -> tuple:
    """數據有沒有變：任何寫入、修訂、刪除都會改變條數、最新修改時間或最大編號；數據源優先級變了也算。"""
    count, last_update, last_id = session.execute(
        select(func.count(), func.max(Observation.updated_at), func.max(Observation.id))).one()
    revisions = session.scalar(select(func.count()).select_from(ObservationRevision))   # 每次改值都會多一條
    priorities = tuple(session.execute(select(DataSource.code, DataSource.priority).order_by(DataSource.code)).all())
    return (str(session.get_bind().engine.url), tuple(codes), count, last_update, last_id, revisions, priorities)


def load_points_cached(session: Session, codes: list[str]) -> pd.DataFrame:
    """不帶 as_of 的 load_points，數據沒變時直接用上一次的結果（每個頁面請求都要讀全部數據點）。

    回傳的表是共用的：調用方只能讀，不能改。
    """
    global _cache
    key = data_fingerprint(session, sorted(codes))
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
