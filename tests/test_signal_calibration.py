"""信號改進（#19）：去趨勢、權重是參數、按回測校準權重（逐步前推檢驗，沒變好不讓採用）。"""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from crudelab import backtest, signals
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations
from crudelab.models import BacktestRun, ParameterVersion

DAYS = pd.bdate_range("2023-01-02", periods=700)


def test_detrend_measures_distance_from_the_trend_line():
    rng = np.random.default_rng(1)
    values = pd.Series(np.arange(len(DAYS)) * 0.1 + rng.normal(0, 1, len(DAYS)), index=DAYS)   # 穩定上升 + 噪聲
    plain = signals.rolling_scores(values, 365, 30, 1, "none").iloc[-1]
    linear = signals.rolling_scores(values, 365, 30, 1, "linear").iloc[-1]
    assert plain["z"] > 1.3                                     # 和均值比：只因為在上升就長期偏多
    assert abs(linear["z"]) < 2.5 and abs(linear["z"]) < plain["z"]
    assert linear["trend_t"] > 10                               # 斜率非常顯著
    flat = pd.Series(rng.normal(0, 1, len(DAYS)), index=DAYS)
    assert abs(signals.rolling_scores(flat, 365, 30, 1, "linear").iloc[-1]["trend_t"]) < 3
    # 窗口內只有一個點時沒有 z
    assert signals.rolling_scores(values.iloc[:1], 365, 30, 1, "linear")["z"].isna().all()


def test_live_signal_equals_the_backtest_score(session, cfg):
    rng = np.random.default_rng(2)
    brent = 70 + np.cumsum(rng.normal(0, 1, len(DAYS)))
    spread = np.cumsum(rng.normal(0, 0.1, len(DAYS)))
    write_observations(session, [Obs(c, d.date(), float(v)) for d, b, m in zip(DAYS, brent, spread, strict=True)
                                 for c, v in (("BRENT_M1", b), ("BRENT_M3", b - m))], "manual")
    session.commit()
    engine = IndicatorEngine(session, cfg)
    today = DAYS[-1]
    live = signals.data_signal(engine, "SIG_BRENT_STRUCTURE", today)
    s = cfg.signals["SIG_BRENT_STRUCTURE"]
    frame = backtest.rolling_scores(engine.values(s.input), s.lookback_days, s.min_points, s.direction, s.detrend)
    assert live["status"] == "ok" and live["score"] == pytest.approx(frame["score"].iloc[-1])
    assert live["weight"] == 1.0 and live["weight_parameter"] == "W_SIG_BRENT_STRUCTURE"


def test_signal_weights_are_versioned_parameters(session, cfg):
    from crudelab import parameters
    assert cfg.parameters["W_SIG_US_321"].group == "信號權重" and cfg.parameters["W_SIG_US_321"].value == 1.0
    parameters.set_value(session, "W_SIG_US_321", 0.0, date(2024, 6, 3), "測試", "t")
    session.commit()
    engine = IndicatorEngine(session, cfg)
    w = backtest.signal_weights(cfg, engine, ["W_SIG_US_321"[2:], "SIG_SG_MARGIN"], DAYS)
    assert w.loc["2024-05-31", "SIG_US_321"] == 1.0 and w.loc["2024-06-03", "SIG_US_321"] == 0.0   # 按生效日期
    part = pd.DataFrame({"SIG_US_321": 0.8, "SIG_SG_MARGIN": -0.2}, index=DAYS)
    module = backtest.weighted_mean(part, w)
    assert module["2024-05-31"] == pytest.approx(0.3) and module["2024-06-03"] == pytest.approx(-0.2)


def test_proposed_weight_rules():
    ok = {"status": "ok"}
    assert backtest.proposed_weight({**ok, "ic": 0.15, "ic_t": 3.0}, 1.0)[0] == 1.5
    assert backtest.proposed_weight({**ok, "ic": 0.9, "ic_t": 9.0}, 1.0)[0] == backtest.WEIGHT_CAP
    assert backtest.proposed_weight({**ok, "ic": 0.01, "ic_t": 2.1}, 1.0)[0] == backtest.MIN_WEIGHT
    weight, _, verdict = backtest.proposed_weight({**ok, "ic": -0.2, "ic_t": -3.0}, 1.0)
    assert (weight, verdict) == (0.0, "reversed")
    assert backtest.proposed_weight({**ok, "ic": 0.05, "ic_t": 1.0}, 1.0)[2] == "weak"
    assert backtest.proposed_weight({"status": "insufficient", "ic": 0.5, "ic_t": 5}, 0.7) == \
        (0.7, "樣本不足：保留現在的權重", "insufficient")


def synthetic(cfg):
    """一個有效的信號、一個方向相反的信號；之後 1 個月的收益跟著有效的那個走。"""
    rng = np.random.default_rng(3)
    good = pd.Series(np.clip(np.sin(np.arange(len(DAYS)) / 15) + rng.normal(0, 0.2, len(DAYS)), -1, 1), index=DAYS)
    scores = pd.DataFrame(np.nan, index=DAYS, columns=list(cfg.signals))
    scores["SIG_US_321"], scores["SIG_SG_MARGIN"] = good, -good + rng.normal(0, 0.1, len(DAYS))
    horizon = next(h for h in backtest.horizons_of(cfg) if h.code == "M1")
    fwd = pd.DataFrame({h.code: np.nan for h in backtest.horizons_of(cfg)}, index=DAYS)
    fwd[horizon.code] = good * 0.02 + rng.normal(0, 0.01, len(DAYS))
    return scores, fwd


def test_calibration_keeps_the_good_signal_and_drops_the_reversed_one(session, cfg):
    scores, fwd = synthetic(cfg)
    result = backtest.calibrate(cfg, IndicatorEngine(session, cfg), scores, fwd, backtest.horizons_of(cfg), 1)
    rows = {r["code"]: r for r in result["signals"]}
    assert rows["SIG_US_321"]["verdict"] == "effective" and rows["SIG_US_321"]["proposed"] > 1
    assert rows["SIG_SG_MARGIN"]["verdict"] == "reversed" and rows["SIG_SG_MARGIN"]["proposed"] == 0
    assert rows["SIG_BRENT_STRUCTURE"]["verdict"] == "insufficient" and rows["SIG_BRENT_STRUCTURE"]["proposed"] == 1.0
    walk = result["walk_forward"]
    assert walk["refits"] >= 20 and walk["better"] is True                # 現在等權：好壞兩個互相抵消
    assert walk["calibrated"]["ic"] > walk["current"]["ic"]


def test_walk_forward_uses_only_returns_already_realised(session, cfg):
    """把評估日 t 之後的收益改掉，t 時的逐步前推權重不能變。"""
    scores, fwd = synthetic(cfg)
    engine = IndicatorEngine(session, cfg)
    horizon = next(h for h in backtest.horizons_of(cfg) if h.code == "M1")
    cut = DAYS[400]
    before = backtest.calibrate(cfg, engine, scores.loc[:cut], fwd.loc[:cut], backtest.horizons_of(cfg), 1)
    poisoned = fwd.copy()
    later = DAYS[400 - horizon.trading_days + 1:]                         # 到 cut 時還沒實現的收益
    poisoned.loc[later, "M1"] = -poisoned.loc[later, "M1"] * 50
    after = backtest.calibrate(cfg, engine, scores.loc[:cut], poisoned.loc[:cut], backtest.horizons_of(cfg), 1)
    assert before["walk_forward"]["refits"] == after["walk_forward"]["refits"]
    assert before["walk_forward"]["last_weights"] == after["walk_forward"]["last_weights"]   # 沒用到未實現的收益
    assert before["signals"] != after["signals"]                         # 全樣本的建議值當然會變（它用了全部）


def test_adopt_only_when_walk_forward_improves(sites, session, cfg):
    admin, front = sites["admin"], sites["front"]
    scores, fwd = synthetic(cfg)
    good = backtest.calibrate(cfg, IndicatorEngine(session, cfg), scores, fwd, backtest.horizons_of(cfg), 1)
    bad = {**good, "walk_forward": {**good["walk_forward"], "better": False}}
    for cal in (bad, good):
        session.add(BacktestRun(created_by="t", status="done", mode="observation", step=1,
                                results={"calibration": backtest._clean(cal)}))
    session.commit()
    runs = [r.id for r in session.query(BacktestRun).order_by(BacktestRun.id)]
    assert admin.post(f"/api/calibration/{runs[0]}/adopt").status_code == 409            # 沒變好：不讓採用
    assert front.post(f"/api/calibration/{runs[1]}/adopt").status_code in (404, 405)    # 前台不能改參數
    latest = admin.get("/api/calibration/latest").json()
    assert latest["run_id"] == runs[1] and latest["adoptable"]
    assert {c["parameter"] for c in latest["changes"]} >= {"W_SIG_US_321", "W_SIG_SG_MARGIN"}
    r = admin.post(f"/api/calibration/{runs[1]}/adopt")
    assert r.status_code == 200
    session.expire_all()
    v = session.query(ParameterVersion).filter(ParameterVersion.key == "W_SIG_SG_MARGIN").order_by(
        ParameterVersion.id.desc()).first()
    assert v.value == 0.0 and f"回測 #{runs[1]}" in v.note and "逐步前推" in v.note
    left = {c["parameter"] for c in admin.get("/api/calibration/latest").json()["changes"]}
    assert not left & {"W_SIG_US_321", "W_SIG_SG_MARGIN"}                 # 採用後就和建議一樣了
