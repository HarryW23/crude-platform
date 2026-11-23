"""單日錯價檢查：某一天突然跳很多、第二天又回到原位，而同一天其他序列都沒有類似的跳動，多半是錯價。

例：Eurobob 2026-08-05 是 1059.3，前後兩天是 924.3 和 925.3。
行情劇烈的日子（例如 2026-03-09 原油和成品油一起大起大落）很多序列同時「跳一下」，那是市場，不是錯價：
同一天另有 MIN_PEERS 條以上序列也出現跳動、或者同一品種的另一條序列也在跳（例如日本和新加坡石腦油
2026-06-03 一起跳又一起回來），不提醒。

判斷（只看日度序列，至少 MIN_POINTS 個數據點）：
  日變化的穩健標準差 σ = 1.4826 × 中位數絕對偏差（MAD）
  第 t 天：前一天到 t 的變化 d1、t 到後一天的變化 d2 方向相反，兩者都大於 K × σ，
  而且前一天和後一天很接近（|x(t+1) − x(t−1)| < 一半的 min(|d1|, |d2|)）——跳上去又回來。
只在導入預覽時提醒，不改數據：確實是錯價的話，在路透核對後用手動錄入改正（會留下修訂記錄）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import numpy as np

K = 8.0                   # 算「可疑」的跳動：日變化穩健標準差的倍數
PEER_K = 5.0              # 判斷其他序列同一天是不是也在跳的門檻（寬一點，市場大動時各序列幅度不同）
MIN_PEERS = 2             # 同一天至少這麼多條其他序列也在跳，就當作市場行情
MIN_POINTS = 30           # 數據點太少，估不出正常的日變化幅度
KINDS = ("futures", "swap", "spot", "diff", "osp", "freight", "fx")   # 價格類序列；庫存、流量、比率不查


@dataclass(frozen=True)
class Spike:
    series: str
    day: date
    before: float
    value: float
    after: float


def spikes(points: list[tuple[date, float]], k: float) -> list[Spike]:
    """一條序列裡所有「跳上去又回來」的日子（series 欄位留空，由調用方填）。"""
    ordered = sorted(points)
    if len(ordered) < MIN_POINTS:
        return []
    days = [p[0] for p in ordered]
    x = np.array([p[1] for p in ordered], dtype=float)
    d = np.diff(x)
    sigma = 1.4826 * float(np.median(np.abs(d - np.median(d))))
    if sigma <= 0:
        return []
    out = []
    for t in range(1, len(x) - 1):
        d1, d2 = x[t] - x[t - 1], x[t + 1] - x[t]
        small = min(abs(d1), abs(d2))
        if np.sign(d1) != np.sign(d2) and small > k * sigma and abs(x[t + 1] - x[t - 1]) < 0.5 * small:
            out.append(Spike("", days[t], float(x[t - 1]), float(x[t]), float(x[t + 1])))
    return out


def suspicious(by_series: Mapping[str, list[tuple[date, float]]],
               commodity: Mapping[str, str] | None = None) -> list[Spike]:
    """所有序列裡的可疑錯價：自己跳得夠大，而且同一天其他序列沒有一起跳。commodity：序列 → 品種。"""
    commodity = commodity or {}
    loud = {code: spikes(points, K) for code, points in by_series.items()}
    loose = {code: {s.day for s in spikes(points, PEER_K)} for code, points in by_series.items()}
    out = []
    for code, found in loud.items():
        for s in found:
            peers = [other for other, days in loose.items() if other != code and s.day in days]
            same = [p for p in peers if commodity.get(p) and commodity.get(p) == commodity.get(code)]
            if len(peers) < MIN_PEERS and not same:
                out.append(Spike(code, s.day, s.before, s.value, s.after))
    return sorted(out, key=lambda s: (s.series, s.day))


def import_warnings(kinds: Mapping[str, str], names: Mapping[str, str],
                    by_series: Mapping[str, list[tuple[date, float]]],
                    commodity: Mapping[str, str] | None = None) -> list[str]:
    """導入預覽的提醒。kinds / names / commodity：序列代碼 → 類型 / 中文名 / 品種（只查價格類的日度序列）。"""
    priced = {c: p for c, p in by_series.items() if kinds.get(c) in KINDS}
    found = suspicious(priced, commodity)
    if not found:
        return []
    by_code: dict[str, list[Spike]] = {}
    for s in found:
        by_code.setdefault(s.series, []).append(s)
    out = []
    for code, items in by_code.items():
        shown = "；".join(f"{s.day} 是 {s.value:g}（前後兩天 {s.before:g}、{s.after:g}）" for s in items[:3])
        more = f" 等 {len(items)} 處" if len(items) > 3 else ""
        out.append(f"{code}（{names.get(code, code)}）可能有錯價：{shown}{more}。單日跳動後又回到原位，"
                   "同一天其他序列沒有類似波動；請在路透上核對，確實錯了就在序列頁手動改正")
    return out
