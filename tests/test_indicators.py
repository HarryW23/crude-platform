from datetime import date, datetime

import pandas as pd
import pytest

from crudelab.indicators import IndicatorEngine, Scenario
from crudelab.ingest import Obs, write_observations
from crudelab.models import utcnow
from crudelab.parameters import set_value

D = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)]


def load(session, data: dict[str, list], source="manual", dates=D, **kwargs):
    obs = [Obs(code, d, v) for code, series in data.items() for d, v in zip(dates, series) if v is not None]
    write_observations(session, obs, source, **kwargs)
    session.commit()


def test_spread_and_crack_values(session, cfg):
    load(session, {"BRENT_M1": [70, 71, 72, 73], "WTI_M1": [66, 66.5, None, 69],
                   "RBOB_M1": [2.0, 2.1, 2.2, 2.3], "HO_M1": [2.4, 2.4, 2.4, 2.4], "GASOIL_ICE_M1": [745] * 4})
    engine = IndicatorEngine(session, cfg)
    spread = engine.compute("SPR_BRENT_WTI")
    assert [d.date() for d in spread.index] == [D[0], D[1], D[3]]      # 缺 WTI 的那天不出值
    assert list(spread) == [4.0, 4.5, 4.0]
    assert engine.compute("CRK_US_321").iloc[0] == pytest.approx((2 * 2.0 + 2.4) * 42 / 3 - 66)
    assert engine.compute("CRK_ICE_GASOIL").iloc[0] == pytest.approx(745 / 7.45 - 70)

    summary = engine.summary("SPR_BRENT_WTI")
    assert summary["status"] == "ok" and summary["last_value"] == 4.0 and summary["chg_1d"] == -0.5
    assert summary["chg_5d"] is None and summary["percentile_1y"] is None       # 歷史太短，不硬算
    missing = engine.summary("SPR_BRENT_DUBAI")
    assert missing["status"] == "missing_data" and missing["missing_series"] == ["DUBAI_SWAP_M1"]
    assert missing["last_value"] is None


def test_missing_parameter_blocks_indicator_until_provided(session, cfg):
    load(session, {"TD3C_WS": [60, 62, 64, 66]})
    summary = IndicatorEngine(session, cfg).summary("FRT_TD3C_BBL")
    assert summary["status"] == "missing_parameter" and summary["missing_parameters"] == ["FLAT_TD3C"]
    set_value(session, "FLAT_TD3C", 22.0, valid_from=date(2026, 1, 1))
    session.commit()
    freight = IndicatorEngine(session, cfg).compute("FRT_TD3C_BBL").iloc[0]
    assert freight == pytest.approx(60 / 100 * 22 / 7.31)                # 中東→中國按沙特輕質折桶（#28）


def test_parameters_apply_by_effective_date(session, cfg):
    dates = [date(2018, 4, 27), date(2018, 5, 2), date(2019, 4, 2)]
    load(session, {"SC_M1": [500, 500, 500]}, dates=dates)
    cost = IndicatorEngine(session, cfg).compute("CN_CRUDE_SC_CNY_T")
    assert list(cost) == pytest.approx([500 * 7.33 * rate + 40 for rate in (1.17, 1.16, 1.13)])

    # 給一個新的生效日期：只影響那天之後；歷史不變
    set_value(session, "CN_PORT_MISC", 60, valid_from=date(2019, 1, 1))
    session.commit()
    engine = IndicatorEngine(session, cfg)
    assert list(engine.compute("CN_CRUDE_SC_CNY_T")) == pytest.approx(
        [500 * 7.33 * 1.17 + 40, 500 * 7.33 * 1.16 + 40, 500 * 7.33 * 1.13 + 60])
    node = engine.trace("CN_PORT_MISC", dates[1])
    assert node["value"] == 40 and node["valid_from"] == "1900-01-01"
    assert engine.trace("CN_PORT_MISC", dates[2])["valid_from"] == "2019-01-01"


def test_parameter_correction_and_as_of(session, cfg):
    load(session, {"BRENT_M1": [70] * 4, "GASOIL_ICE_M1": [750] * 4}, now=datetime(2026, 9, 5))
    before = utcnow()
    set_value(session, "BBL_PER_T_GASOIL", 7.5)            # 不給生效日期 = 更正
    session.commit()
    assert IndicatorEngine(session, cfg).compute("CRK_ICE_GASOIL").iloc[0] == pytest.approx(30.0)
    earlier = IndicatorEngine(session, cfg, as_of=before)
    assert earlier.compute("CRK_ICE_GASOIL").iloc[0] == pytest.approx(750 / 7.45 - 70)


def test_ffill_bridges_holidays_only_within_limit(session, cfg):
    # SC 在 9/2、9/3 休市；指標的 ffill_limit=3 天內沿用
    load(session, {"SC_M1": [500, None, None, 510], "USDCNY": [7.0] * 4, "BRENT_M1": [70, 71, 72, 73]})
    engine = IndicatorEngine(session, cfg)
    assert engine.compute("SPR_SC_BRENT")[str(D[2])] == pytest.approx(500 / 7 - 72)
    sc = engine.trace("SPR_SC_BRENT", D[2])["children"][0]["children"][0]
    assert sc["code"] == "SC_M1" and sc["obs_date"] == D[0].isoformat() and "沿用" in sc["note"]
    assert engine.compute("TS_SC_M1_M3").empty             # 沒有 SC_M3


def test_osp_priced_on_loading_month_average_and_never_leaks_into_next_month(session, cfg):
    """沙特官價：本月裝船 = 本月阿曼/迪拜均價（月初至今）+ 本月官價。9 月的官價不會用到 10 月。"""
    days = [date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 30), date(2026, 10, 2), date(2026, 10, 20)]
    load(session, {"OMAN_M1": [70, 71, 72, 80, 81], "DUBAI_CASH": [70, 71, 72, 80, 81]}, dates=days)
    load(session, {"ARAB_LIGHT_OSP_ASIA": [2.5]}, dates=[date(2026, 9, 1)])
    engine = IndicatorEngine(session, cfg)
    out = engine.compute("OUT_ARAB_LIGHT")
    assert list(out.index.date) == days[:3]                # 10 月還沒有官價：沒有值，不沿用 9 月的
    assert list(out) == [72.5, 73.0, 73.5]                 # 9/30 = 9 月均價 71 + 2.5 = 實際結算價
    load(session, {"ARAB_LIGHT_OSP_ASIA": [-1.0]}, dates=[date(2026, 10, 1)])
    out = IndicatorEngine(session, cfg).compute("OUT_ARAB_LIGHT")
    assert out[pd.Timestamp(2026, 10, 20)] == pytest.approx(80.5 - 1.0)    # 10 月重新從 1 日起算均價
    tree = IndicatorEngine(session, cfg).trace("OUT_ARAB_LIGHT", date(2026, 10, 20))
    osp = next(c for c in tree["children"] if c["code"] == "ARAB_LIGHT_OSP_ASIA")
    assert osp["value"] == -1.0 and osp["obs_date"] == "2026-10-01"


def test_osp_must_be_dated_on_its_loading_month(session, cfg):
    from crudelab.ingest import IngestError, Obs, write_observations
    with pytest.raises(IngestError, match="裝船月"):
        write_observations(session, [Obs("ARAB_LIGHT_OSP_ASIA", date(2026, 11, 5), 2.5)], "demo")   # 公布日
    assert write_observations(session, [Obs("ARAB_LIGHT_OSP_ASIA", date(2026, 12, 1), 2.5)], "demo")["inserted"] == 1


def test_scenario_overrides_without_saving(session, cfg):
    load(session, {"BRENT_M1": [70] * 4, "GASOIL_ICE_M1": [745] * 4})
    scenario = IndicatorEngine(session, cfg, scenario=Scenario({"BBL_PER_T_GASOIL": 7.0}, {"BRENT_M1": 5.0}))
    assert scenario.compute("CRK_ICE_GASOIL").iloc[0] == pytest.approx(745 / 7.0 - 75)
    assert IndicatorEngine(session, cfg).compute("CRK_ICE_GASOIL").iloc[0] == pytest.approx(30.0)
    on_indicator = IndicatorEngine(session, cfg, scenario=Scenario(shifts={"CRK_ICE_GASOIL": -2.0}))
    assert on_indicator.compute("CRK_ICE_GASOIL").iloc[0] == pytest.approx(28.0)
    with pytest.raises(KeyError):
        IndicatorEngine(session, cfg, scenario=Scenario({"NOPE": 1}))


def test_trace_reaches_source_cell(loaded, cfg):
    engine = IndicatorEngine(loaded, cfg)
    tree = engine.trace("CRK_ICE_GASOIL", date(2026, 8, 3))
    assert [(c["kind"], c["code"]) for c in tree["children"]] == \
        [("series", "GASOIL_ICE_M1"), ("parameter", "BBL_PER_T_GASOIL"), ("series", "BRENT_M1")]
    leaf = tree["children"][0]
    assert leaf["source_ref"] == "期貨!K5" and leaf["filename"] == "sample.xlsx" and leaf["source_code"] == "demo"
    assert tree["children"][1]["verified"] is False
    assert tree["value"] == pytest.approx(leaf["value"] / 7.45 - tree["children"][2]["value"])
