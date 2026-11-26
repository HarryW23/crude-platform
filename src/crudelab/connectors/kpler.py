"""Kpler 終端下載的表（#25）：在 Kpler 網頁上把流量、庫存圖下載成 Excel，原樣上傳就能導入。

認法：工作表名是「World monthly exports (Crude-Co…」這種（Kpler 下載的固定寫法，按表的標題截到 31 個字），
拆分方式從文件名看（Kpler 的文件名是「World_monthly_exports_Crude_Co_by_origin_country_…」）。
表頭（國家名、庫存分類）前面加上「kpler:表的種類:」再交給一般的表頭識別，例如 kpler:exports:Saudi Arabia；
序列配置裡的別名寫的就是這種名字（config/master/series/kpler.yaml），所以國家名不會和別的序列撞上
（「Saudi Arabia」本來是 OPEC 月報產量的名字）。
平台裡沒有對應序列的國家（一百多個）不列出來，只在說明裡記一個數。

只收平台要的拆分方式：出口按裝貨國（origin country）、進口按卸貨國 / 地區（destination country / continent）、
庫存按陸上 / 海上（onshore/offshore status）。進口按來源國拆分的表，「United States」那一列是從美國運出的量，
不是美國的進口，混進來會悄悄錯，所以整個文件拒收並說明要怎麼下載。
最後一期（這個月、這一周）還沒結束，數字不完整（裝貨還沒被跟蹤到），不讀。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from ..periods import parse_period

PREFIX = "kpler:"
SHEET = re.compile(r"^world\s+(monthly|weekly)\s+(exports|imports|inventories)\b", re.I)
SPLITS = {                    # 表的種類 → 文件名裡允許的拆分方式
    "exports": ("by_origin_country",),
    "imports": ("by_destination_country", "by_destination_continent"),
    "inventories": ("by_onshore_offshore_status",),
}
HOW = {
    "exports": "Flows → Crude → Exports，按 Origin（裝貨國）的 Country 拆分",
    "imports": "Flows → Crude → Imports，按 Destination（卸貨國）的 Country 或 Continent 拆分",
    "inventories": "Inventories → Crude，勾選含海上（commodities on water），按 onshore/offshore status 拆分",
}


class KplerFileError(ValueError):
    pass


def _current(period: date, granularity: str, today: date) -> bool:
    if granularity == "monthly":
        return (period.year, period.month) >= (today.year, today.month)
    return period >= date.fromisocalendar(*today.isocalendar()[:2], 1)


def adapt(path: Path, grids: dict[str, list[list]], today: date | None = None) -> dict[str, str]:
    """把 Kpler 下載的表改成一般的寬表（就地改 grids），回傳 {工作表名: 說明}。不是 Kpler 的表不動。"""
    today = today or date.today()
    notes: dict[str, str] = {}
    name = path.name.lower()
    for title, grid in grids.items():
        m = SHEET.match(title.strip())
        if not m or not grid or not grid[0] or str(grid[0][0] or "").strip().lower() != "date":
            continue
        granularity, kind = m.group(1).lower(), m.group(2).lower()
        if kind in ("exports", "imports") and "crude" not in name:
            raise KplerFileError(f"Kpler 的表「{title}」：文件名裡看不出是原油（Crude）。平台目前只收原油的流量，"
                                 f"下載時商品選 Crude；文件名不要改")
        if not any(split in name for split in SPLITS[kind]):
            raise KplerFileError(f"Kpler 的表「{title}」的拆分方式不對（或文件名被改過，看不出拆分方式）："
                                 f"平台要的是 {HOW[kind]}。這個文件沒有導入")
        header = grid[0]
        grid[0] = [header[0]] + [f"{PREFIX}{kind}:{str(h).strip()}" if h not in (None, "") else h for h in header[1:]]
        kept = [grid[0]]
        dropped = []
        for row in grid[1:]:
            period = parse_period(str(row[0] or "")) if row else None
            if period is not None and _current(period, granularity, today):
                dropped.append(str(row[0]))
                continue
            kept.append(row)
        grid[:] = kept
        if dropped:
            what = "這個月" if granularity == "monthly" else "這一周"
            notes[title] = (f"{'、'.join(dropped)} 是{what}，還沒結束，Kpler 的數字不完整（還有裝貨沒跟蹤到），"
                            "沒有讀入；過了這一期再導入就有")
    return notes
