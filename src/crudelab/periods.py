"""期間型數據的日期約定。

月度、季度、年度的值代表整個期間，一律記在**期間的第一天**：
    2026 年 9 月 → 2026-09-01        2026 年三季度 → 2026-07-01        2026 年 → 2026-01-01
周度數據記在數據源給的日期（EIA 周報是周五）。
這個約定讓不同來源的同期數據能直接對齊。Excel 導入和各 connector 都用這裡的函數解析期間。
"""
from __future__ import annotations

import re
from datetime import date

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_YEAR_MONTH = re.compile(r"^(\d{4})\s*[-/年]\s*(\d{1,2})\s*月?$")            # 2026-09  2026/9  2026年9月
_MON_YEAR = re.compile(r"^([A-Za-z]{3})[a-z]*[-\s']?(\d{4}|\d{2})$")           # Sep-26  Sep 2026
_YEAR_Q = re.compile(r"^(\d{4})\s*-?\s*Q([1-4])$", re.I)                       # 2026Q3  2026-Q3
_Q_YEAR = re.compile(r"^([1-4])Q\s*'?(\d{4}|\d{2})$|^Q([1-4])\s*'?(\d{4}|\d{2})$", re.I)   # 3Q26  Q3 2026


def _year(text: str) -> int:
    value = int(text)
    return value + 2000 if value < 100 else value


def parse_period(text: str) -> date | None:
    """把「2026-09」「Sep-26」「2026Q3」「3Q26」這類期間寫法轉成期間第一天。認不出回 None。"""
    text = text.strip()
    if m := _YEAR_MONTH.match(text):
        year, month = int(m.group(1)), int(m.group(2))
    elif (m := _MON_YEAR.match(text)) and m.group(1).lower() in _MONTHS:
        year, month = _year(m.group(2)), _MONTHS[m.group(1).lower()]
    elif m := _YEAR_Q.match(text):
        year, month = int(m.group(1)), 3 * int(m.group(2)) - 2
    elif m := _Q_YEAR.match(text):
        quarter, yy = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        year, month = _year(yy), 3 * int(quarter) - 2
    else:
        return None
    return date(year, month, 1) if 1 <= month <= 12 and 1980 <= year <= 2100 else None
