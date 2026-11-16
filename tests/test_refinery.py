from datetime import date

import pytest

from crudelab.indicators import IndicatorEngine, Scenario
from crudelab.ingest import Obs, write_observations
from crudelab.refinery import breakdown

DAY = date(2026, 9, 1)


def put(session, values: dict[str, float]):
    write_observations(session, [Obs(code, DAY, v) for code, v in values.items()], "manual")
    session.commit()


def test_benchmark_margin_matches_hand_calculation(session, cfg):
    put(session, {"DUBAI_SWAP_M1": 70, "SG_NAPHTHA": 66, "SG_GASOLINE_92": 80, "SG_JET_KERO": 88,
                  "SG_GASOIL_10PPM": 90, "SG_HSFO_380": 400})
    engine = IndicatorEngine(session, cfg)
    expected = 0.07 * 66 + 0.22 * 80 + 0.14 * 88 + 0.36 * 90 + 0.14 * 400 / 6.35 - 70
    assert engine.compute("RM_SG_COMPLEX_MARGIN").iloc[0] == pytest.approx(expected)
    assert "RM_SG_COMPLEX_GROSS" not in cfg.indicators                  # 沒有稅和成本項時不重複列出


def test_china_fuel_margin_matches_hand_calculation(session, cfg):
    put(session, {"SC_M1": 500, "SD_GASOLINE_92": 8200, "SD_DIESEL_0": 7000, "SD_LPG": 4800, "SD_PETCOKE": 2400})
    engine = IndicatorEngine(session, cfg)
    spec = cfg.refineries["CN_FUEL_SD"]

    feed = 500 * 7.33 * 1.0 * 1.13 + 40
    revenue = 0.30 * 8200 + 0.37 * 7000 + 0.06 * 4800 + 0.07 * 2400
    consumption_tax = 0.30 * 2109.76 + 0.37 * 1411.2
    vat = (revenue - feed) / 1.13 * 0.13
    surcharge = (consumption_tax + vat) * 0.12
    margin = revenue - feed - consumption_tax - vat - surcharge - 300

    result = breakdown(engine, spec, DAY)
    got = {c["label"]: c["value"] for c in result["components"]}
    assert got == pytest.approx({"產品總收入": revenue, "原料總成本": feed, "產品收入 − 原料成本": revenue - feed,
                                 "消費稅": consumption_tax, "增值稅": vat, "城建及教育附加": surcharge,
                                 "加工費": 300, "毛利": margin})
    signed = sum(c["sign"] * c["value"] for c in result["components"] if c["category"] not in ("subtotal", "total"))
    assert signed == pytest.approx(margin)                              # 拆解各項加總等於毛利
    gasoline = result["products"][0]
    assert gasoline["price"] == pytest.approx(8200) and gasoline["weight"] == 0.30
    assert gasoline["consumption_tax"] == 2109.76 and result["products"][2]["consumption_tax"] is None


def test_model_reports_what_is_missing(session, cfg):
    put(session, {"SC_M1": 500, "SD_GASOLINE_92": 8200})
    engine = IndicatorEngine(session, cfg)
    summary = engine.summary(cfg.refineries["CN_FUEL_SD"].margin)
    assert summary["status"] == "missing_data"
    assert summary["missing_series"] == ["SD_DIESEL_0", "SD_LPG", "SD_PETCOKE"]
    assert "RM_CN_FUEL_SD_Y_GASOLINE" in summary["unverified_parameters"]
    partial = breakdown(engine, cfg.refineries["CN_FUEL_SD"], DAY)
    assert partial["products"][0]["value"] == pytest.approx(0.30 * 8200) and partial["products"][1]["value"] is None
    assert partial["components"][-1]["value"] is None                   # 毛利不會用不完整的數據硬算


def test_yield_scenario_changes_margin(loaded, cfg):
    spec = cfg.refineries["CN_FUEL_SD"]
    base = IndicatorEngine(loaded, cfg)
    day = base.compute(spec.margin).index[-1].date()
    shifted = IndicatorEngine(loaded, cfg, scenario=Scenario({"RM_CN_FUEL_SD_Y_GASOLINE": 0.32,
                                                              "RM_CN_FUEL_SD_Y_DIESEL": 0.35}))
    b, s = breakdown(base, spec, day), breakdown(shifted, spec, day)
    price = {p["code"]: p["price"] for p in b["products"]}
    assert s["components"][0]["value"] - b["components"][0]["value"] == pytest.approx(
        0.02 * price["GASOLINE"] - 0.02 * price["DIESEL"])
    assert s["components"][-1]["value"] != b["components"][-1]["value"]
