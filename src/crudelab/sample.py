"""生成演示用的假數據 Excel。數字是隨機遊走，不是真實行情，只用來試導入流程。

故意做得不規整（多行表頭、RIC 代碼、空行、橫排版式、一列認不出來的列），
用來模擬「專人整理但不完全按模板」的實際情況。
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook


def _walk(rng: random.Random, start: float, step: float, n: int) -> list[float]:
    out, x = [], start
    for _ in range(n):
        x += rng.gauss(0, step)
        out.append(round(x, 2))
    return out


def write_sample_workbook(path: Path, days: int = 60, seed: int = 7) -> None:
    rng = random.Random(seed)
    dates = []
    d = date(2026, 8, 3)
    while len(dates) < days:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)

    brent = _walk(rng, 70, 0.9, days)
    cols = {  # 表頭兩行：名稱 / RIC
        ("Brent M1", "LCOc1"): brent,
        ("Brent M2", "LCOc2"): [round(v - 0.45 + rng.gauss(0, 0.05), 2) for v in brent],
        ("Brent M3", "LCOc3"): [round(v - 0.85 + rng.gauss(0, 0.08), 2) for v in brent],
        ("WTI M1", "CLc1"): [round(v - 3.6 + rng.gauss(0, 0.3), 2) for v in brent],
        ("WTI M3", "CLc3"): [round(v - 4.3 + rng.gauss(0, 0.3), 2) for v in brent],
        ("迪拜掉期M1", ""): [round(v - 1.8 + rng.gauss(0, 0.25), 2) for v in brent],
        ("迪拜掉期M3", ""): [round(v - 2.5 + rng.gauss(0, 0.25), 2) for v in brent],
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

    ws4 = wb.create_sheet("說明")
    ws4.append(["這一頁只有文字，系統會標記為無法識別並跳過。"])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
