"""單日錯價檢查：自己跳上去又回來、同一天其他序列沒一起跳，才提醒。"""
from datetime import date, timedelta

import numpy as np

from crudelab import spikes
from crudelab.imports import preview_file


def walk(seed, n=80, start=100.0, step=0.5):
    rng = np.random.default_rng(seed)
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    return days, list(start + np.cumsum(rng.normal(0, step, n)))


def test_isolated_spike_is_flagged_but_market_wide_jump_is_not():
    days, a = walk(1)
    _, b = walk(2)
    _, c = walk(3)
    _, d = walk(4)
    a[40] += 30                                               # 只有 a：錯價
    for s in (a, b, c, d):                                    # 第 60 天大家一起跳：市場行情
        s[60] += 25
    series = {k: list(zip(days, v, strict=True)) for k, v in {"A": a, "B": b, "C": c, "D": d}.items()}
    found = spikes.suspicious(series)
    assert [(s.series, s.day) for s in found] == [("A", days[40])]
    assert found[0].value == a[40] and found[0].before == a[39] and found[0].after == a[41]


def test_partial_reversal_short_or_flat_series_are_not_flagged():
    days, a = walk(5)
    a[30] += 30
    a[31] += 15                                               # 只回來一半：可能是真行情，不提醒
    assert spikes.spikes(list(zip(days, a, strict=True)), spikes.K) == []
    assert spikes.spikes([(days[i], 1.0) for i in range(10)], spikes.K) == []      # 數據太少
    assert spikes.spikes([(days[i], 1.0) for i in range(50)], spikes.K) == []      # 完全不動：σ = 0


def test_import_preview_warns_only_for_price_series(session, settings):
    days, a = walk(6, start=900, step=4)
    a[50] += 140
    _, stocks = walk(7, start=400, step=1)
    stocks[50] += 60                                          # 庫存（周度、非價格類）不查
    rows = zip(days, a, stocks, strict=True)
    lines = ["Date,NWE_EUROBOB,EIA_CRUDE_STOCKS"] + [f"{d},{x:.2f},{y:.2f}" for d, x, y in rows]
    preview = preview_file(session, settings, "\n".join(lines).encode(), "r.csv", "reuters_xls")
    hits = [w for w in preview["warnings"] if "錯價" in w]
    assert len(hits) == 1 and hits[0].startswith(f"NWE_EUROBOB（西北歐 Eurobob 汽油）可能有錯價：{days[50]} 是")
