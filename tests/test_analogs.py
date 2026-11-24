"""歷史事件類比庫（#29）：只記事實和出處；市場反應用平台裡的價格現算。"""
from datetime import date

import pandas as pd
import pytest

from crudelab import analogs
from crudelab.schema import AnalogDef

DAYS = pd.bdate_range("2019-08-01", periods=120)


def test_library_is_facts_with_sources(cfg):
    assert len(cfg.analogs) >= 10
    for a in cfg.analogs.values():
        assert isinstance(a.date, date) and a.sources and all(u.startswith("https://") for u in a.sources)
        assert a.supply_loss_mbd is None or a.supply_note                 # 寫了數字就要寫口徑
    assert cfg.analogs["ABQAIQ_2019"].supply_loss_mbd == 5.7 and cfg.analogs["ABQAIQ_2019"].date == date(2019, 9, 14)
    with pytest.raises(ValueError, match="口徑"):
        AnalogDef(code="X", name_zh="x", date=date(2020, 1, 1), category="other", description="d",
                  sources=["https://a.example"], supply_loss_mbd=1.0)
    with pytest.raises(ValueError, match="https"):
        AnalogDef(code="X", name_zh="x", date=date(2020, 1, 1), category="other", description="d",
                  sources=["www.example.com"])


def test_reaction_uses_the_close_before_the_event():
    prices = pd.Series(60.0, index=DAYS)
    after = DAYS > pd.Timestamp("2019-09-13")
    prices[after] = 66.0                                                   # 周六事件，周一起漲 10%
    prices[DAYS == DAYS[after][3]] = 69.0                                  # 第 4 個交易日衝高
    spread = pd.Series(0.5, index=DAYS)
    spread[after] = 1.5
    prices = prices.iloc[:60]                                              # 只到事件後兩個多月：3 個月那格要留空
    r = analogs.reaction(prices, spread, date(2019, 9, 14))
    assert r["base_date"] == "2019-09-13" and r["base_price"] == 60.0     # 周六的事件：基準是周五收盤
    assert r["moves"]["d1"] == pytest.approx(0.10) and r["moves"]["m1"] == pytest.approx(0.10)
    assert r["peak"] == pytest.approx(0.15) and r["trough"] == pytest.approx(0.10)
    assert r["spread"]["d1"] == pytest.approx(1.0)
    assert r["status"] == "partial" and r["moves"]["m3"] is None          # 3 個月後超出了數據：留空不估算


def test_reaction_without_data():
    prices = pd.Series(60.0, index=DAYS)
    assert analogs.reaction(prices, pd.Series(dtype=float), date(1990, 8, 2))["status"] == "no_data"
    assert analogs.reaction(prices, pd.Series(dtype=float), date(2030, 1, 1))["status"] == "no_data"


def test_api_filters_by_category_and_computes_from_imported_prices(client, loaded):
    body = client.get("/api/analogs?category=conflict").json()
    assert body["analogs"] and {a["category"] for a in body["analogs"]} == {"conflict"}
    assert [a["date"] for a in body["analogs"]] == sorted((a["date"] for a in body["analogs"]), reverse=True)
    everything = client.get("/api/analogs").json()["analogs"]
    assert len(everything) >= 10
    old = next(a for a in everything if a["code"] == "KUWAIT_1990")
    assert old["status"] == "no_data" and old["moves"] == {}              # 演示數據沒有 1990 年：不給數字


def test_base_price_is_the_real_close_not_the_roll_adjusted_level():
    """漲跌用換月調整後的價格算，顯示的基準價要是當天的實際收盤。
    （2026-11 用三年路透數據體檢時發現：長期貼水下，2025-06-12 的 Brent 實際 69.36，調整後只有 46.9。）"""
    raw = pd.Series(69.36, index=DAYS)
    adjusted = raw * 0.676                                                 # 倒推出來的歷史水平低很多
    adjusted[DAYS > pd.Timestamp("2019-09-13")] *= 1.07
    r = analogs.reaction(adjusted, pd.Series(dtype=float), date(2019, 9, 14), raw)
    assert r["base_price"] == 69.36 and r["moves"]["d1"] == pytest.approx(0.07)
    without_raw = analogs.reaction(adjusted, pd.Series(dtype=float), date(2019, 9, 14))     # 沒給原始價格：照舊
    assert without_raw["base_price"] == pytest.approx(69.36 * 0.676)
