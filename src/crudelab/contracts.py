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

約定：最後交易日當天合約仍然是 M1，第二天才換。工作日只按周一到周五算，交易所假期用 overrides 修正。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import Config
from .schema import CurveDef, RollRule


@dataclass(frozen=True)
class CurveMember:
    curve: CurveDef
    n: int


def ordinal(year: int, month: int) -> int:
    return year * 12 + month - 1


def contract_label(o: int) -> str:
    return f"{o // 12}-{o % 12 + 1:02d}"


def _prev_business(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _minus_business(d: date, n: int) -> date:
    for _ in range(n):
        d = _prev_business(d - timedelta(days=1))
    return d


def last_trading_day(rule: RollRule, o: int) -> date:
    """合約月份序號 o 的最後交易日。"""
    key = contract_label(o)
    if key in rule.overrides:
        return rule.overrides[key]
    m = o - rule.months_before
    year, month = m // 12, m % 12 + 1
    if rule.day == "last_business":
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        anchor = _prev_business(nxt - timedelta(days=1))
    else:
        anchor = _prev_business(date(year, month, rule.day))
    return _minus_business(anchor, rule.business_days_before)


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
    out = []
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
    index = pd.DatetimeIndex(sorted(set().union(*[s.index for s in raw.values()]))) if raw else pd.DatetimeIndex([])
    if len(index) == 0:
        return get(code)
    target = front_ordinal(m.curve.roll, anchor.date()) + m.n - 1
    need = target - front_ordinals(m.curve.roll, index) + 1          # 每天需要的月序
    values = np.full(len(index), np.nan)
    for n, s in raw.items():
        hit = need == n
        if hit.any():
            values[hit] = s.reindex(index[hit]).to_numpy(dtype=float)
    return pd.Series(values, index=index, name=code).dropna()


# ------------------------------ 換月調整收益 ------------------------------

def chained_returns(cfg: Config, code: str, get) -> tuple[pd.Series, int]:
    """code 的逐日對數收益，換月日用同一合約的價格算。返回（收益, 退回原始收益的換月次數）。"""
    prices = get(code).dropna()
    prices = prices[prices > 0]
    raw = np.log(prices).diff().iloc[1:]
    m = member(cfg, code)
    if m is None or len(prices) < 2:
        return raw, 0
    fronts = front_ordinals(m.curve.roll, prices.index)
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
