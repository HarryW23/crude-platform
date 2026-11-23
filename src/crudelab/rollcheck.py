"""換月規則核對：用路透連續合約每天對應的合約，找出實際換月日，和 config 裡曲線的 roll 規則比較。

換月日錯了會悄悄影響很多東西：日變化、波動率、回測收益都按「同一個合約」算，換月那天的跳動要剔除；
規則差一天，就會有一天把換月跳動當成行情。規則裡的 verified: false 就是還沒核對過。

數據來自 scripts/lseg_export.py export 順便寫出的 reuters_日期_contracts.csv：
每行是 日期、序列代碼、RIC、到期日（EXPIR_DATE）、合約月（CRT_MNTH）。
連續合約 M1 的到期日（沒有就用合約月）變了的那天，就是數據商實際的換月日。

用法：crudelab verify-rolls reuters_20261124_contracts.csv
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import contracts
from .config import Config

NEAR_DAYS = 10            # 實際換月日和規則換月日相差多少個日曆天以內，算同一次換月（差幾天）


@dataclass
class CurveCheck:
    curve: str
    series: str
    first: date
    last: date
    expected: list[date]                      # 規則給的換月日（已對到有數據的那天）
    actual: list[date]                        # 數據裡的換月日
    shifted: list[tuple[date, date, int]] = field(default_factory=list)   # (規則, 實際, 差幾個交易日)
    unmatched_rule: list[date] = field(default_factory=list)              # 規則說換月、附近數據裡沒換
    unmatched_data: list[date] = field(default_factory=list)              # 數據裡換了、附近規則沒換
    overrides: dict[str, date] = field(default_factory=dict)              # 建議寫進 roll.overrides 的
    note: str | None = None
    verified: bool = False                    # 配置裡已經標了 roll.verified

    @property
    def consistent(self) -> bool:
        return not (self.shifted or self.unmatched_rule or self.unmatched_data)


def read_contracts(path: Path) -> dict[str, list[tuple[date, str, str]]]:
    """讀 reuters_*_contracts.csv：{序列代碼: [(日期, 到期日, 合約月)]}，按日期排好。"""
    out: dict[str, list[tuple[date, str, str]]] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or len(header) < 5:
            raise ValueError(f"{path.name} 不是合約對照文件（應有 日期、序列代碼、RIC、到期日、合約月 五列）")
        for n, row in enumerate(reader, 2):
            if not row:
                continue
            try:
                day = date.fromisoformat(row[0][:10])
            except ValueError:
                raise ValueError(f"{path.name} 第 {n} 行的日期看不懂：{row[0]}") from None
            out.setdefault(row[1], []).append((day, row[3].strip(), row[4].strip()))
    for rows in out.values():
        rows.sort()
    return out


def _parse_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def actual_rolls(rows: list[tuple[date, str, str]]) -> list[date]:
    """M1 對應的合約變了的日子（到期日優先，沒有就看合約月）。第一天不算。"""
    out = []
    previous = None
    for day, expiry, month in rows:
        key = expiry or month
        if not key:
            continue
        if previous is not None and key != previous:
            out.append(day)
        previous = key
    return out


def check_curve(cfg: Config, code: str, rows: list[tuple[date, str, str]]) -> CurveCheck | None:
    curve = cfg.curves[code]
    if curve.roll is None or not rows:
        return None
    rule = curve.roll
    days = [r[0] for r in rows]
    first, last = days[0], days[-1]
    actual = actual_rolls(rows)

    def on_data(d: date) -> date | None:                 # 規則換月日落在假期時，對到之後第一個有數據的日子
        return next((x for x in days if x >= d), None)

    # 從數據第一天算起（第一天當天的換月看不出來，去掉）：從第二天算會漏掉正好在第二天的那次
    expected = sorted({x for d in contracts.roll_dates(rule, first, last)
                       if (x := on_data(d)) is not None and x > first})
    result = CurveCheck(code, f"{code}_M1", first, last, expected, actual, verified=rule.verified)
    position = {d: i for i, d in enumerate(days)}
    exact = set(expected) & set(actual)
    spare_actual = [d for d in actual if d not in exact]
    for d in [d for d in expected if d not in exact]:
        near = [a for a in spare_actual if abs((a - d).days) <= NEAR_DAYS]
        if near:
            a = min(near, key=lambda x: abs((x - d).days))
            spare_actual.remove(a)
            result.shifted.append((d, a, position[a] - position[d]))
        else:
            result.unmatched_rule.append(d)
    result.unmatched_data = spare_actual

    if curve.lseg and curve.lseg.offset:
        m1 = curve.lseg.ric.replace("{n}", str(1 + curve.lseg.offset))
        result.note = (f"這條曲線的 RIC 有月序偏移（M1 = {m1}），"
                       "數據商的到期日不是這裡 M1 的最後交易日，只比較換月日，不給 overrides 建議")
        return result
    # 建議的 overrides：實際換月前一天的到期日，就是換掉的那個合約的實際最後交易日
    by_day = {r[0]: r for r in rows}
    for _rule_day, actual_day, _ in result.shifted:
        before = days[position[actual_day] - 1]
        expiry = _parse_day(by_day[before][1])
        if expiry is None or abs((expiry - before).days) > NEAR_DAYS:
            continue
        # 換掉的是哪個月份：規則裡最後交易日離這個到期日最近的那個（規則早換或晚換時，當天的 M1 可能已經不是它）
        o0 = contracts.front_ordinal(rule, before)
        o = min((o0 - 1, o0, o0 + 1), key=lambda x: abs((contracts.last_trading_day(rule, x) - expiry).days))
        if expiry != contracts.last_trading_day(rule, o):
            result.overrides[contracts.contract_label(o)] = expiry
    return result


def verify(cfg: Config, data: dict[str, list[tuple[date, str, str]]]) -> list[CurveCheck]:
    out = []
    for code in cfg.curves:
        check = check_curve(cfg, code, data.get(f"{code}_M1", []))
        if check is not None:
            out.append(check)
    return out


def report(checks: list[CurveCheck]) -> list[str]:
    lines = []
    for c in checks:
        head = (f"{c.curve}（{c.first} ~ {c.last}）：規則換月 {len(c.expected)} 次，數據裡換月 {len(c.actual)} 次，"
                f"{'完全一致' if c.consistent else '有出入'}")
        lines.append(head)
        lines += [f"  規則 {r}，實際 {a}（{'晚' if k > 0 else '早'} {abs(k)} 個交易日）" for r, a, k in c.shifted]
        lines += [f"  規則 {d} 換月，數據附近沒換" for d in c.unmatched_rule]
        lines += [f"  數據 {d} 換月，規則附近沒換" for d in c.unmatched_data]
        if c.overrides:
            body = ", ".join(f'"{k}": {v}' for k, v in sorted(c.overrides.items()))
            lines.append(f"  建議寫進 roll.overrides：{{{body}}}")
        if c.note:
            lines.append(f"  注：{c.note}")
        if c.consistent and len(c.expected) >= 3 and not c.verified:
            lines.append("  可以把這條曲線的 roll.verified 改成 true")
        if not c.consistent and c.verified:
            lines.append("  這條曲線標了 verified: true，但和這份數據對不上：先查清楚再決定是改規則還是加 overrides")
    return lines or ["文件裡沒有任何有換月規則的曲線的 M1（序列代碼像 BRENT_M1）"]
