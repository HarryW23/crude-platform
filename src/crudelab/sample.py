"""生成演示用的假數據 Excel。數字是隨機遊走，不是真實行情，只用來試導入流程。

期貨按具體合約生成再排成連續合約，所以換月日的跳動和真實行情一樣存在。
故意做得不規整（多行表頭、RIC 代碼、空行、橫排版式、一列認不出來的列），
用來模擬「專人整理但不完全按模板」的實際情況。
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

from .contracts import front_ordinal, last_trading_day
from .schema import RollRule


def _walk(rng: random.Random, start: float, step: float, n: int) -> list[float]:
    out, x = [], start
    for _ in range(n):
        x += rng.gauss(0, step)
        out.append(round(x, 2))
    return out


END = date(2026, 10, 23)          # 測試用的演示數據的最後一個交易日（固定，結果可以重現）


def last_weekday(today: date | None = None) -> date:
    """crudelab make-sample 用：演示數據結束在最近一個工作日，否則一生成就是「數據太舊」。"""
    d = today or date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _weekdays_ending(end: date, count: int) -> list[date]:
    out, d = [], end
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def write_sample_workbook(path: Path, days: int = 60, seed: int = 7, end: date = END) -> None:
    """days：日度行情的交易日數。供需的周度、月度數據固定給 6 年多，夠算五年同期。"""
    rng = random.Random(seed)
    dates = _weekdays_ending(end, days)

    brent = _walk(rng, 70, 0.9, days)
    curves = _curves(dates, brent, random.Random(seed + 1))
    cols = {  # 表頭兩行：名稱 / RIC
        ("Brent M1", "LCOc1"): curves["Brent"][1],
        ("Brent M2", "LCOc2"): curves["Brent"][2],
        ("Brent M3", "LCOc3"): curves["Brent"][3],
        ("WTI M1", "CLc1"): curves["WTI"][1],
        ("WTI M3", "CLc3"): curves["WTI"][3],
        ("迪拜掉期M1", ""): curves["Dubai"][1],
        ("迪拜掉期M3", ""): curves["Dubai"][3],
        ("RBOB M1", "RBc1"): [round((v + 14 + rng.gauss(0, 1)) / 42, 4) for v in brent],
        ("Heating Oil M1", "HOc1"): [round((v + 24 + rng.gauss(0, 1)) / 42, 4) for v in brent],
        ("ICE Gasoil M1", "LGOc1"): [round((v + 22 + rng.gauss(0, 1)) * 7.45, 2) for v in brent],
        ("我自己的備註列", ""): [round(rng.uniform(0, 1), 2) for _ in brent],
    }
    wb = Workbook()
    ws = wb.active
    ws.title = "期貨"
    ws.append(["【演示假數據，非真實行情】"])
    ws.append([])
    ws.append(["日期", *[name for name, _ in cols]])
    ws.append(["", *[ric for _, ric in cols]])
    for i, day in enumerate(dates):
        ws.append([day, *[series[i] if not (name == "WTI M1" and i == 20) else "N/A"
                          for (name, _), series in cols.items()]])

    ws2 = wb.create_sheet("新加坡成品油")   # 橫排：日期在一行
    ws2.append(["品種", *[day.strftime("%Y/%m/%d") for day in dates]])
    for name, premium in (("新加坡汽油92", 9), ("新加坡柴油", 17), ("新加坡航煤", 16), ("新加坡石腦油", -4)):
        ws2.append([name, *[round(v - 1.8 + premium + rng.gauss(0, 0.6), 2) for v in brent]])
    ws2.append(["新加坡高硫380", *[round((v - 1.8 - 6 + rng.gauss(0, 0.6)) * 6.35, 2) for v in brent]])

    ws3 = wb.create_sheet("升貼水")         # 長表
    ws3.append(["Date", "Name", "Value"])
    for day in dates:
        ws3.append([day.isoformat(), "ESPO Diff", round(4 + rng.gauss(0, 0.3), 2)])
        ws3.append([day.isoformat(), "Urals Diff", round(-12 + rng.gauss(0, 0.5), 2)])

    ws5 = wb.create_sheet("中國")           # 中國市場：國慶假期沒有數據
    ws5.append(["日期", "SC M1", "SC M3", "美元人民幣", "山東汽油92", "山東柴油0", "山東液化氣", "山東石油焦"])
    for i, day in enumerate(dates):
        if date(2026, 10, 1) <= day <= date(2026, 10, 7):
            continue
        sc = round((brent[i] - 1.0) * 7.1 + rng.gauss(0, 3), 1)
        ws5.append([day, sc, round(sc - 6 + rng.gauss(0, 1), 1), round(7.1 + rng.gauss(0, 0.01), 4),
                    round(brent[i] * 105 + 900 + rng.gauss(0, 40)), round(brent[i] * 95 + 500 + rng.gauss(0, 40)),
                    round(4800 + rng.gauss(0, 60)), round(2400 + rng.gauss(0, 50))])

    _balance_sheets(wb, rng, end)
    _back_months(wb, dates, curves)
    _positioning(wb, end, random.Random(seed + 2))
    ws4 = wb.create_sheet("說明")
    ws4.append(["這一頁只有文字，系統會標記為無法識別並跳過。"])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _balance_sheets(wb: Workbook, rng: random.Random, end: date = END) -> None:
    """供需演示數據：EIA 式的周度表（周五）和 STEO 式的月度表（含預測期，表頭用「2026-09」這種期間寫法）。"""
    fridays, d = [], end - timedelta(days=(end.weekday() - 4) % 7)          # 不晚於 end 的最後一個周五
    while d >= date(2020, 1, 3):
        fridays.append(d)
        d -= timedelta(days=7)
    fridays.reverse()
    ws = wb.create_sheet("EIA周度")
    ws.append(["Week ending", "美國商業原油庫存", "美國戰略儲備", "庫欣庫存", "美國汽油庫存", "美國餾分油庫存",
               "美國原油產量", "美國原油進口", "美國原油出口", "美國煉廠加工量", "美國成品油表需", "美國汽油表需",
               "美國餾分油表需"])
    crude, spr = 440_000.0, 400_000.0
    for i, day in enumerate(fridays):
        season = math.sin(2 * math.pi * (day.timetuple().tm_yday - 60) / 365)
        production = 12_800 + 0.6 * i + rng.gauss(0, 60)
        imports, exports = 6_300 + rng.gauss(0, 350), 4_000 + rng.gauss(0, 400)
        runs = 16_000 + 700 * season + rng.gauss(0, 200)
        unaccounted = rng.gauss(0, 250)
        crude += 7 * (production + imports - exports - runs + unaccounted - 600)
        crude += (430_000 - crude) * 0.03                      # 拉回均值，避免漂走
        ws.append([day, round(crude), round(spr), round(32_000 - 9_000 * season + rng.gauss(0, 1500)),
                   round(228_000 - 10_000 * season + rng.gauss(0, 2500)),
                   round(120_000 + 12_000 * season + rng.gauss(0, 2500)),
                   round(production), round(imports), round(exports), round(runs),
                   round(20_200 + 600 * season + rng.gauss(0, 500)), round(8_900 + 500 * season + rng.gauss(0, 200)),
                   round(3_900 - 300 * season + rng.gauss(0, 180))])

    ws2 = wb.create_sheet("STEO月度")
    ws2.append(["Month", "OPEC液體產量", "非OPEC液體產量", "OPEC原油產量", "OECD需求", "非OECD需求", "OECD商業庫存"])
    stocks = 2_800.0
    for n in range((end.year - 2020 + 2) * 12):              # 2020-01 … 明年 12 月（含預測期）
        year, month = 2020 + n // 12, n % 12 + 1
        season = math.cos(2 * math.pi * (month - 1) / 12)
        opec = 32.0 + 0.01 * n + rng.gauss(0, 0.25)
        nonopec = 68.0 + 0.04 * n + rng.gauss(0, 0.2)
        oecd = 45.6 + 0.9 * season + rng.gauss(0, 0.2)
        nonoecd = 54.5 + 0.05 * n + rng.gauss(0, 0.25)
        stocks += (opec + nonopec - oecd - nonoecd) * 4 + (2_800 - stocks) * 0.05
        ws2.append([f"{year}-{month:02d}", round(opec, 2), round(nonopec, 2), round(opec - 5.4, 2), round(oecd, 2),
                    round(nonoecd, 2), round(stocks, 1)])


# 和 config/master/series/crude.yaml 裡的 roll 規則一致
_ROLLS = {"Brent": RollRule(months_before=2), "WTI": RollRule(months_before=1, day=25, business_days_before=3),
          "Dubai": RollRule(months_before=1)}
_OFFSETS = {"Brent": 0.0, "WTI": -3.6, "Dubai": -1.8}


def _curves(dates: list[date], level: list[float], rng: random.Random) -> dict[str, dict[int, list[float]]]:
    """按具體合約生成 M1–M12：每個合約有自己的價格，連續合約 Mn 是當天第 n 個未到期的合約。

    合約價格 = 品種水平 − 斜率 × f(距到期月數) + 合約自己的小噪聲。所以同一個合約每天的變化是連續的，
    而 M1 在換月那天會從「快到期的合約」換成「下一個合約」，價格跳一下——和真實的連續合約一樣。
    斜率隨時間慢慢擺動：有時近強遠弱，有時近弱遠強。
    """
    common = {name: [rng.gauss(0, 0.2) for _ in dates] for name in _ROLLS}
    out: dict[str, dict[int, list[float]]] = {}
    for name, rule in _ROLLS.items():
        out[name] = {n: [] for n in range(1, 13)}
        shock = 0.0
        for i, day in enumerate(dates):
            shock = 0.9 * shock + common[name][i]                  # 品種自己的價差慢慢漂移
            slope = 0.25 + 0.35 * math.sin(2 * math.pi * i / 180)   # 每多一個月，價格低多少
            front = front_ordinal(rule, day)
            for n in range(1, 13):
                o = front + n - 1
                tenor = (last_trading_day(rule, o) - day).days / 30.4
                price = level[i] + _OFFSETS[name] + shock - slope * tenor * 0.95 ** tenor + rng.gauss(0, 0.03)
                out[name][n].append(round(price, 2))
    return out


def _back_months(wb: Workbook, dates: list[date], curves: dict[str, dict[int, list[float]]]) -> None:
    """遠月合約：「期貨」表只有 M1、M3（WTI、迪拜還缺 M2），這張表補齊到 M12，讓曲線圖有東西可畫。"""
    layout = {"Brent": ("LCOc{n}", "Brent M{n}", range(4, 13)),
              "WTI": ("CLc{n}", "WTI M{n}", [2, *range(4, 13)]),
              "Dubai": ("", "迪拜掉期M{n}", [2, *range(4, 13)])}
    header_names, header_rics, columns = ["日期"], [""], []
    for name, (ric, label, wanted) in layout.items():
        for n in wanted:
            header_names.append(label.replace("{n}", str(n)))
            header_rics.append(ric.replace("{n}", str(n)) if ric else "")
            columns.append(curves[name][n])
    ws = wb.create_sheet("遠月合約")
    ws.append(header_names)
    ws.append(header_rics)
    for i, day in enumerate(dates):
        ws.append([day, *[column[i] for column in columns]])


def _positioning(wb: Workbook, end: date, rng: random.Random) -> None:
    """持倉演示數據：每周二一行，三年多。淨多頭佔比在 −5% 到 25% 之間慢慢擺動。"""
    tuesdays, d = [], end - timedelta(days=(end.weekday() - 1) % 7)
    while len(tuesdays) < 170:
        tuesdays.append(d)
        d -= timedelta(days=7)
    tuesdays.reverse()
    ws = wb.create_sheet("持倉")
    ws.append(["Report date", "WTI Managed Money Long", "WTI Managed Money Short", "WTI Open Interest",
               "Brent Managed Money Long", "Brent Managed Money Short", "Brent Open Interest"])
    crowd = 0.1
    for i, day in enumerate(tuesdays):
        crowd = 0.1 + 0.85 * (crowd - 0.1) + rng.gauss(0, 0.025)
        row = [day]
        for oi_base, scale in ((1_800_000, 1.0), (2_400_000, 1.3)):
            oi = oi_base + 150_000 * math.sin(2 * math.pi * i / 52) + rng.gauss(0, 20_000)
            net = oi * (crowd * scale)
            short = 60_000 + abs(rng.gauss(0, 15_000))
            row += [round(short + net) if short + net > 0 else 0, round(short), round(oi)]
        ws.append(row)
