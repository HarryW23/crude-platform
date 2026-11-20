"""期貨合約日曆與換月處理。

數據裡存的是連續合約（M1 = 當天最近的未到期合約，M2 = 下一個……），換月當天 M1 會變成原來的 M2，
直接拿 M1 算漲跌就會把「兩個合約之間的價差」當成一天的漲跌。這裡按曲線配置裡的 roll 規則推出：

  - 每個合約月份的最後交易日（last_trading_day）
  - 每天的 M1 是哪個合約（front_ordinal：合約月份的序號 = 年 × 12 + 月 − 1，Mn 的序號 = M1 的序號 + n − 1）
  - 兩天之間換了幾次月（rolls_between）

在此之上提供兩種「不受換月影響」的算法：

  同一合約的對比（pinned）   固定某一天（錨定日）的合約，把過去每一天的 Mn 換成「那一天上錨定日的 Mn 合約」的價格，
                             即 M(n + 換月次數)。指標的日變化、5 日 / 20 日變化用它算：前後兩個值是同一組合約。
                             需要的遠月不在曲線範圍內（例如 M12 往回 20 天要 M13）時取不到，由調用方決定退回原值並標注。
  換月調整收益（chained_returns）  每天的收益 = log(今天的 Mn / 昨天同一合約的價格)。昨天同一合約 = 昨天的 M(n + k)，
                             k 是兩天之間的換月次數。取不到時退回原始收益並計數。
                             adjusted_prices 把收益連乘成比例調整後的價格，以最新一天為準（最新值等於原始價格）。

約定：最後交易日當天合約仍然是 M1，第二天才換。工作日按曲線的交易日曆算（config/calendars.yaml，
沒指定的只按周一到周五）；日曆管不到的特殊情況用 overrides 修正。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .calendars import WEEKDAYS, Calendar
from .config import Config
from .schema import CurveDef, PricingView, RollRule


@dataclass(frozen=True)
class CurveMember:
    curve: CurveDef
    n: int


def ordinal(year: int, month: int) -> int:
    return year * 12 + month - 1


def contract_label(o: int) -> str:
    return f"{o // 12}-{o % 12 + 1:02d}"


def roll_of(curve: CurveDef) -> RollRule:
    """有換月規則的曲線的規則。調用處都已經篩過（member()、rolled_curves、配置校驗），這裡把前提寫明。"""
    if curve.roll is None:
        raise ValueError(f"曲線 {curve.code} 沒有換月規則（roll），不知道每天的 Mn 是哪個月份")
    return curve.roll


def _calendar(rule: RollRule) -> Calendar:
    """換月規則用的交易日曆（曲線的 calendar；沒指定的只按周一到周五算）。"""
    return rule._calendar or WEEKDAYS


def last_trading_day(rule: RollRule, o: int) -> date:
    """合約月份序號 o 的最後交易日。"""
    key = contract_label(o)
    if key in rule.overrides:
        return rule.overrides[key]
    m = o - rule.months_before
    year, month = m // 12, m % 12 + 1
    if rule.day == "last_business":
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        anchor = _calendar(rule).prev_business(nxt - timedelta(days=1))
    else:
        anchor = _calendar(rule).prev_business(date(year, month, rule.day))
    return _calendar(rule).minus_business(anchor, rule.business_days_before)


def member(cfg: Config, code: str) -> CurveMember | None:
    """序列 code 如果是某條有換月規則的曲線上的 Mn，返回（曲線, n）。"""
    for curve in cfg.curves.values():
        if curve.roll is not None and code.startswith(curve.code + "_M"):
            tail = code[len(curve.code) + 2:]
            if tail.isdigit() and int(tail) in curve.months:
                return CurveMember(curve, int(tail))
    return None


def front_ordinal(rule: RollRule, day: date) -> int:
    """day 這天 M1 合約的序號：最後交易日 >= day 的最早合約。"""
    o = ordinal(day.year, day.month) - 1
    while last_trading_day(rule, o) < day:
        o += 1
    while last_trading_day(rule, o - 1) >= day:      # overrides 可能讓更早的合約還沒到期
        o -= 1
    return o


def front_ordinals(rule: RollRule, index: pd.DatetimeIndex) -> np.ndarray:
    """一串日期各自的 M1 序號（按月緩存最後交易日，長歷史也快）。"""
    if len(index) == 0:
        return np.array([], dtype=int)
    first, last = index.min().date(), index.max().date()
    lo, hi = ordinal(first.year, first.month) - 2, ordinal(last.year, last.month) + rule.months_before + 3
    ltd = pd.DatetimeIndex([pd.Timestamp(last_trading_day(rule, o)) for o in range(lo, hi + 1)])
    # 最後交易日一般隨合約遞增；overrides 若打亂順序，按「最後交易日 >= 當天的最早合約」逐日定位仍然正確
    if ltd.is_monotonic_increasing:
        return lo + np.searchsorted(ltd.values, index.normalize().values, side="left")
    return np.array([front_ordinal(rule, ts.date()) for ts in index])


def rolls_between(rule: RollRule, d0: date, d1: date) -> int:
    """d0 到 d1 之間換了幾次月（d0 上的 M1 在 d1 上是第幾個月份減一）。"""
    return front_ordinal(rule, d1) - front_ordinal(rule, d0)


def roll_dates(rule: RollRule, start: date, end: date) -> list[date]:
    """[start, end] 之間的換月日（最後交易日的下一個工作日，即新合約當上 M1 的第一天）。"""
    out: list[date] = []
    o = front_ordinal(rule, start)
    while True:
        first_day = last_trading_day(rule, o) + timedelta(days=1)
        while first_day.weekday() >= 5:
            first_day += timedelta(days=1)
        if first_day > end:
            return out
        if first_day >= start:
            out.append(first_day)
        o += 1


def contracts_on(cfg: Config, code: str, day: date) -> list[dict]:
    """某天曲線上各個月份對應的具體合約。"""
    curve = cfg.curves[code]
    if curve.roll is None:
        return [{"month": n, "contract": None} for n in curve.months]
    front = front_ordinal(curve.roll, day)
    return [{"month": n, "contract": contract_label(front + n - 1)} for n in curve.months]


# ------------------------------ 同一合約的對比 ------------------------------

def pinned_series(cfg: Config, code: str, anchor: pd.Timestamp, get) -> pd.Series:
    """把 code（某曲線的 Mn）換成「anchor 那天的 Mn 合約」在每一天的價格。get(code) 取原始序列。

    非曲線序列原樣返回。anchor 之後的日期可能需要 M0 以下的月份，取不到就是空。
    """
    m = member(cfg, code)
    if m is None:
        return get(code)
    raw = {n: get(f"{m.curve.code}_M{n}") for n in m.curve.months}
    index = pd.DatetimeIndex([])
    for s in raw.values():
        index = index.union(s.index)
    if len(index) == 0:
        return get(code)
    target = front_ordinal(roll_of(m.curve), anchor.date()) + m.n - 1
    need = target - front_ordinals(roll_of(m.curve), index) + 1          # 每天需要的月序
    values = np.full(len(index), np.nan)
    for n, s in raw.items():
        hit = need == n
        if hit.any():
            values[hit] = s.reindex(index[hit]).to_numpy(dtype=float)
    return pd.Series(values, index=index, name=code).dropna()


# ------------------------------ 計價視角 ------------------------------
#
# 現貨升貼水、官價對應的基準不是「當天的 M1」，而是計價月份那個合約（見 schema.PricingView）。
# 每天的計價月份序號 = 當月序號 + months_ahead（日期 > roll_day 再 + 1）；在曲線上就是 M(計價序號 − M1 序號 + 1)。
# 計價月份的合約已經到期（序號 < 當天 M1）時，用它到期前最後一天的價格（最後結算價）。只用當天及以前的數據。

def view_target(view: PricingView, day: date) -> int:
    """某一天的計價月份序號。"""
    target = ordinal(day.year, day.month) + view.months_ahead
    return target + 1 if view.roll_day is not None and day.day > view.roll_day else target


def view_targets(view: PricingView, index: pd.DatetimeIndex) -> np.ndarray:
    months = index.year.to_numpy() * 12 + index.month.to_numpy() - 1 + view.months_ahead
    if view.roll_day is not None:
        months = months + (index.day.to_numpy() > view.roll_day)
    return months.astype(int)


def view_switch_dates(view: PricingView, start: date, end: date) -> list[date]:
    """start 到 end 之間計價月份切換的日子（那天起用下一個合約）。"""
    out, day, prev = [], start, view_target(view, start)
    while day <= end:
        target = view_target(view, day)
        if target != prev:
            out.append(day)
        prev = target
        day += timedelta(days=1)
    return out


def view_months_needed(cfg: Config, view: PricingView) -> list[int]:
    """計價視角通常會用到曲線上的哪幾個月份（用來列依賴、判斷缺數據）。"""
    curve = cfg.curves[view.curve]
    # 當月 M 的某天，M1 至少是 M + months_before 那個合約（它的最後交易日在 M 月）；計價月份最多是 M + months_ahead (+1)
    top = view.months_ahead + (view.roll_day is not None) - roll_of(curve).months_before + 1
    return [n for n in curve.months if n <= max(top, 1)]


def view_series(cfg: Config, view: PricingView, get, anchor: pd.Timestamp | None = None) -> tuple[pd.Series, pd.Series]:
    """計價視角每天的價格，以及每天用的合約序號。get(code) 取原始序列。

    給了 anchor：固定用 anchor 那天的計價合約（算同一合約的漲跌時用，月份切換不算進變化）。
    """
    curve = cfg.curves[view.curve]
    raw = {n: get(f"{curve.code}_M{n}") for n in curve.months}
    index = pd.DatetimeIndex([])
    for values in raw.values():
        index = index.union(values.index)
    empty = pd.Series(dtype=float, index=pd.DatetimeIndex([]), name=view.code)
    if len(index) == 0:
        return empty, pd.Series(dtype=int, index=pd.DatetimeIndex([]))
    front = front_ordinals(roll_of(curve), index)
    targets = (np.full(len(index), view_target(view, anchor.date())) if anchor is not None
               else view_targets(view, index))
    need = targets - front + 1
    values = np.full(len(index), np.nan)
    for n, series in raw.items():
        hit = need == n
        if hit.any():
            values[hit] = series.reindex(index[hit]).to_numpy(dtype=float)
    expired = need < 1
    if expired.any():                                   # 計價合約已到期：用它作為 M1 的最後一個價格
        m1 = raw.get(1, pd.Series(dtype=float)).reindex(index).to_numpy(dtype=float)
        final: dict[int, tuple[pd.Timestamp, float]] = {}
        for ts, o, v in zip(index, front, m1, strict=True):
            if not np.isnan(v):
                final[int(o)] = (ts, float(v))          # index 遞增，留下的是這個合約當 M1 的最後一天
        for i in np.flatnonzero(expired):
            got = final.get(int(targets[i]))
            if got is not None and got[0] <= index[i]:
                values[i] = got[1]
    out = pd.Series(values, index=index, name=view.code)
    keep = ~np.isnan(values)
    return out[keep], pd.Series(targets[keep], index=index[keep])


# ------------------------------ 換月調整收益 ------------------------------

def chained_returns(cfg: Config, code: str, get) -> tuple[pd.Series, int]:
    """code 的逐日對數收益，換月日用同一合約的價格算。返回（收益, 退回原始收益的換月次數）。"""
    prices = get(code).dropna()
    prices = prices[prices > 0]
    raw = np.log(prices).diff().iloc[1:]
    m = member(cfg, code)
    if m is None or len(prices) < 2:
        return raw, 0
    fronts = front_ordinals(roll_of(m.curve), prices.index)
    k = np.diff(fronts)                                   # 前一天到今天換了幾次月
    out, fallback = raw.copy(), 0
    rolled = np.nonzero(k > 0)[0]
    if len(rolled):
        curve = {n: get(f"{m.curve.code}_M{n}") for n in m.curve.months}
        for i in rolled:
            prev_day = prices.index[i]
            n_prev = m.n + int(k[i])
            prev = curve.get(n_prev)
            p = prev.get(prev_day) if prev is not None else None
            if p is not None and np.isfinite(p) and p > 0:
                out.iloc[i] = float(np.log(prices.iloc[i + 1] / p))
            else:
                fallback += 1
    return out, fallback


def adjusted_prices(cfg: Config, code: str, get) -> tuple[pd.Series, int]:
    """比例調整的連續價格：最新一天等於原始價格，往前按換月調整後的收益倒推。"""
    prices = get(code).dropna()
    prices = prices[prices > 0]
    if member(cfg, code) is None or len(prices) < 2:
        return prices, 0
    returns, fallback = chained_returns(cfg, code, get)
    back = returns[::-1].cumsum()[::-1]                   # 從每一天到最新一天的累計收益
    level = np.exp(np.log(prices.iloc[-1]) - back.reindex(prices.index).shift(-1).fillna(0.0))
    level.iloc[-1] = prices.iloc[-1]
    return level.rename(code), fallback
