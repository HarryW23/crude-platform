"""數據新鮮度：哪些序列超過期限沒有更新。

斷更比報錯更隱蔽：取數任務每次都「跳過」、收件夾一直是空的，系統裡不會有任何錯誤，數字卻在悄悄變舊。
這裡按 config/freshness.yaml 的規則逐條序列檢查，結果有四種：
  fresh     在期限內
  stale     超過期限
  never     從沒有過數據（只對 required 裡的序列報）
  ignored   在 ignore 裡，不檢查
「今天」按 schedule.yaml 的時區算。
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .calendars import WEEKDAYS, Calendar
from .config import Config
from .models import Observation
from .schema import FreshnessRule

FREQUENCY_NAMES = {"daily": "日度", "weekly": "周度", "monthly": "月度", "quarterly": "季度"}


def today_in(cfg: Config, now: datetime | None = None) -> date:
    now = (now or datetime.now(UTC).replace(tzinfo=None)).replace(tzinfo=UTC)
    return now.astimezone(ZoneInfo(cfg.schedule.timezone)).date()


def rule_for(cfg: Config, code: str) -> FreshnessRule:
    for override in cfg.freshness.overrides:
        if override.series == code:
            return override
    return cfg.freshness.defaults[cfg.series[code].frequency]


def lag(rule: FreshnessRule, last: date, today: date, calendar: Calendar = WEEKDAYS) -> int:
    """從 last 到 today 隔了多少天（按規則數工作日或日曆天）。last 當天不算，today 當天算。
    數工作日時跳過這條序列所在市場的假期（交易日曆），長假不會誤報斷更。"""
    if last >= today:
        return 0
    if rule.count == "weekdays":
        return calendar.business_days_between(last, today)
    return (today - last).days


def describe_rule(rule: FreshnessRule) -> str:
    unit = " 個工作日" if rule.count == "weekdays" else " 天"
    what = "沒有新的數據日期" if rule.basis == "observed" else "沒有收到新數據"
    return f"超過 {rule.max_days}{unit}{what}"


def check(session: Session, cfg: Config, now: datetime | None = None) -> dict:
    today = today_in(cfg, now)
    observed = dict(session.execute(select(Observation.series_code, func.max(Observation.obs_date))
                                    .where(Observation.obs_date <= today).group_by(Observation.series_code)).all())
    received = dict(session.execute(select(Observation.series_code, func.max(Observation.updated_at))
                                    .group_by(Observation.series_code)).all())
    fr = cfg.freshness
    rows = []
    for code, s in cfg.series.items():
        has_data = code in received
        if code in fr.ignore:
            status: str | None = "ignored"
            rule = None
        elif not has_data and not (fr.watch == "all" or code in fr.required):
            continue                                  # 從沒導入過、也沒被要求的序列：不打擾
        else:
            rule = rule_for(cfg, code)
            status = "never" if not has_data else None
        row: dict[str, Any] = {"code": code, "name_zh": s.name_zh, "frequency": s.frequency,
               "frequency_name": FREQUENCY_NAMES[s.frequency], "status": status,
               "rule": describe_rule(rule) if rule else None,
               "basis": rule.basis if rule else None, "max_days": rule.max_days if rule else None,
               "last": None, "lag": None, "required": code in fr.required}
        if rule is not None and has_data:
            if rule.basis == "observed":
                last = observed.get(code)            # 只有未來日期的數據（純預測）時為 None
            else:
                last = received[code].replace(tzinfo=UTC).astimezone(ZoneInfo(cfg.schedule.timezone)).date()
            row["last"] = last.isoformat() if last else None
            if last is None:
                row["status"] = "stale"
            else:
                row["lag"] = lag(rule, last, today, cfg.calendar_of(code))
                row["status"] = "stale" if row["lag"] > rule.max_days else "fresh"
        rows.append(row)
    order = {"stale": 0, "never": 1, "fresh": 2, "ignored": 3}
    rows.sort(key=lambda r: (order[r["status"]], -(r["lag"] or 0), r["code"]))
    counts = {k: sum(r["status"] == k for r in rows) for k in order}
    return {"today": today.isoformat(), "checked": counts["stale"] + counts["never"] + counts["fresh"],
            "problems": counts["stale"] + counts["never"], **counts, "rows": rows}


def summary_line(row: dict) -> str:
    if row["status"] == "never":
        return f"{row['name_zh']}：從沒有過數據"
    if row["lag"] is None:
        return f"{row['name_zh']}：只有未來日期的數據"
    unit = " 個工作日" if "工作日" in (row["rule"] or "") else " 天"
    what = "最新數據" if row["basis"] == "observed" else "最近收到"
    return f"{row['name_zh']}：{what} {row['last']}，已 {row['lag']}{unit}（期限 {row['max_days']}）"
