"""重複報價檢查：同一個數連續出現很多天，多半是數據商沒有更新、在沿用舊值，而不是市場真的沒動。

典型例子是路透的升貼水和化工現貨：Bonny Light 升貼水曾經 71 天都是 1.55。這種數據每天都「有」，
斷更檢查看不出來，但用它算的日變化、波動率、價差信號都會失真。

規則在 config/freshness.yaml 的 repeats（見 schema.RepeatRule）：只看日度序列，預設只看現貨（spot）
和升貼水（diff），期貨結算價、匯率在假期或冷門月份不變是正常的，不檢查。
用在三個地方：
  - 斷更檢查（freshness.check）：最新的值已經連續 max_run 天沒變，算斷更，走斷更通知
  - 導入預覽（imports.preview_file）：文件裡有很長的重複段時提醒
  - 序列頁：斷更提示裡寫明是「沿用舊值」
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

TOLERANCE = 1e-9


@dataclass(frozen=True)
class Run:
    """一段連續相同的值：從 start 到 end 共 n 個數據點都是 value。"""
    start: date
    end: date
    n: int
    value: float


def watched(cfg: Config, code: str) -> bool:
    """這條序列要不要做重複報價檢查。"""
    rule = cfg.freshness.repeats
    s = cfg.series.get(code)
    if s is None or code in rule.ignore or s.frequency != "daily":
        return False
    return s.kind in rule.kinds or code in rule.series


def runs(points: Iterable[tuple[date, float]], min_n: int) -> list[Run]:
    """按日期排好的數據點裡，所有長度 ≥ min_n 的重複段。"""
    out: list[Run] = []
    current: list[tuple[date, float]] = []
    for d, v in sorted(points, key=lambda p: p[0]):
        if current and abs(v - current[-1][1]) <= TOLERANCE:
            current.append((d, v))
            continue
        if len(current) >= min_n:
            out.append(Run(current[0][0], current[-1][0], len(current), current[0][1]))
        current = [(d, v)]
    if len(current) >= min_n:
        out.append(Run(current[0][0], current[-1][0], len(current), current[0][1]))
    return out


def trailing(points: Sequence[tuple[date, float]]) -> Run | None:
    """最新的值已經連續出現了多少次（不管多短都回傳）。points 要按日期排好。"""
    if not points:
        return None
    last_day, last_value = points[-1]
    n, start = 0, last_day
    for d, v in reversed(points):
        if abs(v - last_value) > TOLERANCE:
            break
        n, start = n + 1, d
    return Run(start, last_day, n, last_value)


def describe(run: Run) -> str:
    return f"從 {run.start} 起連續 {run.n} 個數據點都是 {run.value:g}"


def import_warnings(cfg: Config, by_series: dict[str, list[tuple[date, float]]]) -> list[str]:
    """導入預覽的提醒：文件裡某條要檢查的序列有連續 max_run 次以上相同的值。"""
    rule = cfg.freshness.repeats
    out = []
    for code in sorted(by_series):
        if not watched(cfg, code):
            continue
        found = runs(by_series[code], rule.max_run)
        if not found:
            continue
        longest = max(found, key=lambda r: r.n)
        out.append(f"{code}（{cfg.series[code].name_zh}）有 {len(found)} 段連續 {rule.max_run} 次以上相同的值，"
                   f"最長一段{describe(longest)}。多半是數據商在沿用舊值，不是新報價；"
                   f"日變化、波動率會偏小，最新一段如果還在延續，斷更檢查會報出來")
    return out
