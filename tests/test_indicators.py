from datetime import date

import pytest

from crudelab.indicators import FormulaError, IndicatorEngine, formula_names, parse_formula
from crudelab.ingest import Obs, write_observations
from crudelab.models import Parameter

D = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)]


def _load(session, data: dict[str, list], source="manual"):
    obs = [Obs(code, d, v) for code, values in data.items() for d, v in zip(D, values) if v is not None]
    write_observations(session, obs, source)
    session.commit()


def test_formula_parsing_is_restricted():
    assert formula_names("(2 * RBOB_M1 + HO_M1) * GAL_PER_BBL / 3 - lag(WTI_M1, 1)") == \
        ["RBOB_M1", "HO_M1", "GAL_PER_BBL", "WTI_M1"]
    for bad in ("a.b", "f(x)", "x if y else z", "[1]", "'s'", "a < b", "lag(x, n=1)", "1 +"):
        with pytest.raises(FormulaError):
            parse_formula(bad)


def test_spread_and_crack_values(session, cfg):
    _load(session, {"BRENT_M1": [70, 71, 72, 73], "WTI_M1": [66, 66.5, None, 69],
                    "RBOB_M1": [2.0, 2.1, 2.2, 2.3], "HO_M1": [2.4, 2.4, 2.4, 2.4],
                    "GASOIL_ICE_M1": [745, 745, 745, 745]})
    engine = IndicatorEngine(session, cfg.indicators)
    spread = engine.compute("SPR_BRENT_WTI")
    assert [d.date() for d in spread.index] == [D[0], D[1], D[3]]      # 缺 WTI 的那天不出值
    assert list(spread) == [4.0, 4.5, 4.0]
    assert engine.compute("CRK_US_321").iloc[0] == pytest.approx((2 * 2.0 + 2.4) * 42 / 3 - 66)
    assert engine.compute("CRK_ICE_GASOIL").iloc[0] == pytest.approx(745 / 7.45 - 70)

    summary = engine.summary("SPR_BRENT_WTI")
    assert summary["status"] == "ok" and summary["last_value"] == 4.0 and summary["change"] == -0.5
    missing = engine.summary("SPR_BRENT_DUBAI")
    assert missing["status"] == "missing_data" and missing["missing_inputs"] == ["DUBAI_SWAP_M1"]
    assert missing["last_value"] is None


def test_parameter_change_flows_into_indicator(session, cfg):
    _load(session, {"BRENT_M1": [70] * 4, "GASOIL_ICE_M1": [750] * 4})
    session.get(Parameter, "BBL_PER_T_GASOIL").value = 7.5
    session.commit()
    assert IndicatorEngine(session, cfg.indicators).compute("CRK_ICE_GASOIL").iloc[-1] == pytest.approx(30.0)


def test_ffill_limit_bridges_holidays_only_within_limit(session, cfg):
    # SC 在 9/2、9/3 休市；ffill_limit=3 天內沿用，Brent 那邊不受影響
    _load(session, {"SC_M1": [500, None, None, 510], "USDCNY": [7.0, 7.0, 7.0, 7.0],
                    "BRENT_M1": [70, 71, 72, 73]})
    engine = IndicatorEngine(session, cfg.indicators)
    spread = engine.compute("SPR_SC_BRENT")
    assert spread[str(D[2])] == pytest.approx(500 / 7 - 72)
    tree = engine.trace("SPR_SC_BRENT", D[2])
    sc_usd = tree["children"][0]
    assert sc_usd["code"] == "SC_M1_USD" and sc_usd["value"] == pytest.approx(500 / 7)
    sc = sc_usd["children"][0]
    assert sc["obs_date"] == D[0].isoformat() and "沿用" in sc["note"]
    assert engine.compute("TS_SC_M1_M3").empty                         # 沒有 SC_M3


def test_trace_reaches_source_cell(session, settings, cfg, tmp_path):
    from crudelab.imports import commit_batch, preview_file
    from crudelab.sample import write_sample_workbook
    path = tmp_path / "s.xlsx"
    write_sample_workbook(path)
    preview = preview_file(session, settings, path.read_bytes(), "s.xlsx", "demo")
    commit_batch(session, preview["batch_id"])
    session.commit()
    engine = IndicatorEngine(session, cfg.indicators)
    tree = engine.trace("CRK_ICE_GASOIL", date(2026, 8, 3))
    kinds = [(c["kind"], c["code"]) for c in tree["children"]]
    assert kinds == [("series", "GASOIL_ICE_M1"), ("parameter", "BBL_PER_T_GASOIL"), ("series", "BRENT_M1")]
    leaf = tree["children"][0]
    assert leaf["source_ref"] == "期貨!K5" and leaf["filename"] == "s.xlsx" and leaf["source_code"] == "demo"
    assert tree["value"] == pytest.approx(leaf["value"] / 7.45 - tree["children"][2]["value"])
