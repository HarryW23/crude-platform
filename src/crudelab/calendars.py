"""交易所節假日日曆：哪些日子是工作日（有結算價 / 有數據）。

用在兩處：
  - 斷更檢查（freshness.py）：按工作日數期限時，跳過這條序列所在市場的假期，國內長假不再誤報
  - 換月（contracts.py）：最後交易日按交易所工作日算，遇到假期往前推

日曆在 config/calendars.yaml 定義，序列和曲線用 calendar: 代碼 指定；沒指定的只按周一到周五算（WEEKDAYS）。
兩種寫法：
  - rules：每年固定的規則（固定日期、第 n 個星期幾、復活節前後），自動推出每一年
  - dates：逐年登記的日期（例如中國的長假，每年由國務院公布），years 寫明哪幾年已經登記全
只有 dates、沒有 rules 的日曆，在沒登記的年份退回只按周一到周五算，crudelab check 會提醒補上。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from .schema import CalendarDef, HolidayRule

RULE_YEARS = range(1990, 2061)          # 規則型日曆推出的年份範圍


@dataclass(frozen=True)
class Calendar:
    code: str
    name_zh: str
    holidays: frozenset[date] = frozenset()
    years: frozenset[int] | None = None         # dates 登記全的年份；None = 規則型，每年都有
    verified: bool = True
    _array: np.ndarray = field(default_factory=lambda: np.array([], dtype="datetime64[D]"), compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_array", np.array(sorted(self.holidays), dtype="datetime64[D]"))

    def covers(self, year: int) -> bool:
        return self.years is None or year in self.years

    def is_business(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def prev_business(self, day: date) -> date:
        """day 本身是工作日就是 day，否則往前找最近的工作日。"""
        while not self.is_business(day):
            day -= timedelta(days=1)
        return day

    def minus_business(self, day: date, n: int) -> date:
        """從 day 往前數 n 個工作日。"""
        for _ in range(n):
            day = self.prev_business(day - timedelta(days=1))
        return day

    def business_days_between(self, after: date, through: date) -> int:
        """after 之後（不含）到 through（含）有幾個工作日。"""
        if through <= after:
            return 0
        one = np.timedelta64(1, "D")
        return int(np.busday_count(np.datetime64(after, "D") + one, np.datetime64(through, "D") + one,
                                   holidays=self._array))


WEEKDAYS = Calendar("WEEKDAYS", "只按周一到周五")


def _easter(year: int) -> date:
    """格里曆復活節（Anonymous Gregorian algorithm）。"""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    return date(year, month, (h + m - 7 * n + 114) % 31 + 1)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date | None:
    """這個月第 nth 個星期幾（nth = -1 是最後一個）。這個月沒有第 nth 個（例如第 5 個星期一）時回 None：
    不能讓它滑到下個月去，那樣會多出一個錯誤的假期。"""
    if nth > 0:
        first = date(year, month, 1)
        day = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))
        return day if day.month == month else None
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def rule_dates(rule: HolidayRule, year: int) -> list[date]:
    """一條規則在某一年落在哪天（含順延）。"""
    if (rule.since and year < rule.since) or (rule.until and year > rule.until):
        return []
    if rule.easter_offset is not None:
        return [_easter(year) + timedelta(days=rule.easter_offset)]
    assert rule.month is not None                  # 校驗保證：非復活節規則一定有月份
    if rule.nth is not None:
        assert rule.weekday is not None
        found = _nth_weekday(year, rule.month, rule.weekday, rule.nth)
        return [found] if found else []
    assert rule.day is not None
    if rule.month == 2 and rule.day == 29 and not calendar.isleap(year):
        return []                                  # 2 月 29 日的假期：平年沒有
    day = date(year, rule.month, rule.day)
    if rule.observe == "nearest":                 # 周六 → 周五，周日 → 周一
        day = day - timedelta(days=1) if day.weekday() == 5 else day + timedelta(days=1) if day.weekday() == 6 else day
    elif rule.observe == "sunday_to_monday":      # 只有周日順延到周一；周六不補
        day = day + timedelta(days=1) if day.weekday() == 6 else day
    elif rule.observe == "next_weekdays":         # 落在周末就順延到之後的工作日（連續兩天的假期，例如英國聖誕和節禮日）
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return [day]


def build(defn: CalendarDef) -> Calendar:
    days: set[date] = set(defn.dates)
    for rule in defn.rules:
        for year in RULE_YEARS:
            for d in rule_dates(rule, year):
                if rule.observe == "next_weekdays":
                    while d in days:                # 聖誕順延到周一，節禮日就再往後一天
                        d += timedelta(days=1)
                        while d.weekday() >= 5:
                            d += timedelta(days=1)
                days.add(d)
    years = None if defn.rules else frozenset(defn.years)
    return Calendar(defn.code, defn.name_zh, frozenset(days), years, defn.verified)
