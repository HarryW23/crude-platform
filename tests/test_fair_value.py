"""基本面公允價值模型。"""
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from crudelab import backtest, fair_value, projection
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations
from crudelab.parameters import set_value
from crudelab.schema import FairValueModel

N = 900


def business_days(n, end=date(2026, 10, 23)):
    return list(pd.bdate_range(end=end, periods=n).date)


def put(session, code, days, values):
    write_observations(session, [Obs(code, d, float(v)) for d, v in zip(days, values, strict=True)], "manual")
    session.commit()


def synthetic(session, phi=0.95, slope=0.04, seed=7):
    """log(Brent) = 4.2 + slope × 月差 + 偏離，偏離是 AR(1)：每天保留 phi。月差是慢慢擺動的基本面。"""
    rng = np.random.default_rng(seed)
    days = business_days(N)
    t = np.arange(N)
    structure = 1.5 * np.sin(2 * np.pi * t / 250) + rng.normal(0, 0.1, N)
    dev = np.zeros(N)
    for i in range(1, N):
        dev[i] = phi * dev[i - 1] + rng.normal(0, 0.01)
    brent = np.exp(4.2 + slope * structure + dev)
    put(session, "BRENT_M1", days, brent)
    put(session, "BRENT_M3", days, brent - structure)                     # 讓 TS_BRENT_M1_M3 正好等於 structure
    return days, structure, dev


def one_factor(**kwargs) -> FairValueModel:
    return FairValueModel(**{"code": "FV", "name_zh": "測試", "target": "BRENT_M1", "window_days": 1095,
                             "min_points": 120, "factors": [{"input": "TS_BRENT_M1_M3"}], **kwargs})


def test_recovers_a_known_relationship(session, cfg):
    days, _, dev = synthetic(session)
    model = one_factor()
    engine = IndicatorEngine(session, cfg)
    frame = fair_value.aligned_frame(engine, cfg, model)
    fit = fair_value.fit_at(frame, frame.index[-1], model)
    # 偏離的方差約 0.032²，基本面部分約 0.042²，所以 R² 理論上約 0.6
    assert fit.status == "ok" and 0.45 < fit.r2 < 0.8
    # 偏離持續性強時有效樣本少，係數的估計誤差本來就在一兩成
    assert fit.coefficients["TS_BRENT_M1_M3"] == pytest.approx(0.04, rel=0.25)
    assert fit.phi == pytest.approx(0.95, abs=0.03)
    assert fit.half_life == pytest.approx(math.log(0.5) / math.log(0.95), rel=0.3)
    assert fit.deviation == pytest.approx(dev[-1], abs=0.01)               # 偏離就是造出來的那個偏離
    assert fit.fair == pytest.approx(fit.price / math.exp(fit.deviation))
    assert fit.deviation_pct == pytest.approx(math.exp(fit.deviation) - 1)
    # 拆解：公允價值 = 窗口均值水平 + 各因子貢獻
    window = frame[frame.index >= frame.index[-1] - pd.Timedelta(days=1095)]
    mean_level = fit.intercept + fit.coefficients["TS_BRENT_M1_M3"] * window["TS_BRENT_M1_M3"].mean()
    assert math.log(fit.fair) == pytest.approx(mean_level + fit.contributions["TS_BRENT_M1_M3"])
    # 漂移：偏高就往下，期限越長消退越多
    d5, d63 = fair_value.drift_log(fit, 5), fair_value.drift_log(fit, 63)
    assert d5 == pytest.approx(-(1 - fit.phi ** 5) * fit.deviation) and abs(d63) > abs(d5)
    assert np.sign(d5) == -np.sign(fit.deviation)


def test_no_lookahead_past_fits_ignore_future_data(session, cfg):
    days, _, _ = synthetic(session)
    model = one_factor()
    frame = fair_value.aligned_frame(IndicatorEngine(session, cfg), cfg, model)
    t = frame.index[600]
    before = fair_value.fit_at(frame, t, model)
    changed = frame.copy()
    changed.loc[changed.index > t, ["y", "TS_BRENT_M1_M3"]] *= 3            # 把之後的數據改得面目全非
    after = fair_value.fit_at(changed, t, model)
    assert before.as_dict() == after.as_dict()


def test_says_no_when_it_should(session, cfg):
    days = business_days(400)
    rng = np.random.default_rng(1)
    brent = 70 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))                  # 隨機遊走
    put(session, "BRENT_M1", days, brent)
    put(session, "BRENT_M3", days, brent - rng.normal(1, 0.5, 400))           # 月差 = 純噪音，和價格無關
    engine = IndicatorEngine(session, cfg)
    model = one_factor()
    fit = fair_value.current(engine, cfg, "BRENT_FV")
    assert fit.status in ("missing", "insufficient")                      # 預設模型還要庫存偏離：沒有數據
    assert any("沒有因子的數據" in r or "樣本" in r for r in fit.reasons)
    frame = fair_value.aligned_frame(engine, cfg, model)
    weak = fair_value.fit_at(frame, frame.index[-1], model)
    assert weak.status == "weak" and any("R²" in r for r in weak.reasons) and fair_value.drift_log(weak, 21) is None
    short = fair_value.fit_at(frame, frame.index[50], model)
    assert short.status == "insufficient" and "至少需要 120" in short.reasons[0]
    with pytest.raises(ValueError, match="min_points"):
        one_factor(min_points=10)
    with pytest.raises(ValueError, match="重複"):
        one_factor(factors=[{"input": "TS_BRENT_M1_M3"}, {"input": "TS_BRENT_M1_M3"}])


def test_backtest_shows_the_drift_helps_when_the_relationship_is_real(session, cfg, monkeypatch):
    synthetic(session, phi=0.95)
    monkeypatch.setitem(cfg.fair_values, "BRENT_FV", one_factor(code="BRENT_FV"))
    result = backtest.run(session, cfg, backtest.Options(step=1))
    fv = result["fair_values"][0]
    assert fv["coverage"]["ok"] > 600
    week, month = fv["drift"]["W1"], fv["drift"]["M1"]
    assert week["improvement"] > 0.05 and month["improvement"] > 0.1 and month["significant"]
    assert month["hit_rate"] > 0.6
    assert fv["by_horizon"]["M1"]["ic"] > 0.3                              # 偏高 → 之後跌：分數取了負號，所以 IC 為正
    assert result["points"]["FV:BRENT_FV"]["M1"]["points"]


def test_backtest_shows_no_benefit_on_noise(session, cfg, monkeypatch):
    days = business_days(N)
    rng = np.random.default_rng(9)
    brent = 70 * np.exp(np.cumsum(rng.normal(0, 0.01, N)))
    put(session, "BRENT_M1", days, brent)
    put(session, "BRENT_M3", days, brent - rng.normal(1, 0.5, N))             # 月差 = 純噪音
    monkeypatch.setitem(cfg.fair_values, "BRENT_FV", one_factor(code="BRENT_FV"))
    fv = backtest.run(session, cfg, backtest.Options(step=1))["fair_values"][0]
    assert fv["coverage"]["ok"] < 0.2 * N                                  # 絕大多數日子判為解釋力弱，不給漂移
    assert not any(d["significant"] for d in fv["drift"].values())


def test_projection_uses_drift_only_when_chosen_and_available(session, cfg, monkeypatch):
    synthetic(session)
    monkeypatch.setitem(cfg.fair_values, "BRENT_FV", one_factor(code="BRENT_FV"))
    set_value(session, "PROJ_PRICE_PER_STOCK", 5.0)
    plain = projection.create(session, "不加", cfg=cfg)
    with_drift = projection.create(session, "加", drift_model="BRENT_FV", cfg=cfg)
    session.commit()
    base = projection.compute(session, cfg, plain.id)
    drifted = projection.compute(session, cfg, with_drift.id)
    fit = fair_value.current(IndicatorEngine(session, cfg), cfg, "BRENT_FV")
    price = base["anchor"]["price"]
    assert base["drift"] is None and base["cases"][0]["horizons"]["M1"]["central"] == pytest.approx(price)
    expected = price * (math.exp(-(1 - fit.phi ** 21) * fit.deviation) - 1)
    cell = drifted["cases"][0]["horizons"]["M1"]
    assert cell["drift_effect"] == pytest.approx(expected) and cell["central"] == pytest.approx(price + expected)
    assert drifted["drift"]["status"] == "ok" and drifted["drift"]["effects"]["M1"] == pytest.approx(expected)

    projection.set_drift_model(session, cfg, plain.id, "BRENT_FV")       # 之後也能打開 / 關掉
    assert projection.compute(session, cfg, plain.id)["drift"]["status"] == "ok"
    with pytest.raises(projection.ProjectionError, match="沒有名為"):
        projection.set_drift_model(session, cfg, plain.id, "NOPE")

    monkeypatch.setitem(cfg.fair_values, "BRENT_FV", one_factor(code="BRENT_FV", min_r2=0.99))   # 模型變得不可用
    unavailable = projection.compute(session, cfg, with_drift.id)
    assert unavailable["cases"][0]["horizons"]["M1"]["drift_effect"] == 0
    assert any("不給漂移" in p for p in unavailable["problems"])


def test_vintage_and_observation_agree_on_fits(session, cfg, monkeypatch):
    from datetime import datetime, time
    days = business_days(200)
    rng = np.random.default_rng(3)
    for i, d in enumerate(days):                                           # 每個值在它的日期當天入庫
        s = math.sin(i / 20)
        write_observations(session, [Obs("BRENT_M1", d, float(70 * math.exp(0.04 * s + rng.normal(0, 0.005)))),
                                     Obs("BRENT_M3", d, float(70 * math.exp(0.04 * s) - s))], "manual",
                           now=datetime.combine(d, time(18)))
    session.commit()
    monkeypatch.setitem(cfg.fair_values, "BRENT_FV", one_factor(code="BRENT_FV", min_points=60))
    start = days[150]
    obs = backtest.run(session, cfg, backtest.Options("observation", start=start, step=5))["fair_values"][0]
    vin = backtest.run(session, cfg, backtest.Options("vintage", start=start, step=5))["fair_values"][0]
    assert obs["coverage"] == vin["coverage"]
    assert obs["by_horizon"]["W1"]["ic"] == pytest.approx(vin["by_horizon"]["W1"]["ic"])


def test_api_report_and_chat(sites, session, cfg, monkeypatch):
    synthetic(session)
    monkeypatch.setitem(cfg.fair_values, "BRENT_FV", one_factor(code="BRENT_FV"))
    front = sites["front"]
    front.app.state.crudelab._cfg = cfg
    listing = front.get("/api/fair-value").json()
    assert listing[0]["code"] == "BRENT_FV" and listing[0]["status"] == "ok"
    assert listing[0]["factors"][0]["name_zh"] == "Brent M1 − M3" and listing[0]["drift"]["M1"]["effect"] is not None
    detail = front.get("/api/fair-value/BRENT_FV?days=100").json()
    assert len(detail["history"]) == 100 and detail["history"][-1]["fair"] == pytest.approx(listing[0]["fair"])
    assert front.get("/api/fair-value/NOPE").status_code == 404
    made = front.post("/api/projections", json={"name_zh": "x", "drift_model": "BRENT_FV"}).json()
    assert made["drift_model"] == "BRENT_FV"
    assert front.post("/api/projections", json={"name_zh": "y", "drift_model": "NOPE"}).status_code == 400
    assert front.patch(f"/api/projections/{made['id']}", json={"drift_model": None}).json()["drift_model"] is None
    report = front.post("/api/reports", json={"kind": "daily"}).json()
    text = front.get(f"/api/reports/{report['id']}").json()["markdown"]
    assert "## 基本面公允價值" in text and "公允價值" in text and "半衰期" in text
    from crudelab.llm import chat
    ctx = chat._Ctx(session, cfg, IndicatorEngine(session, cfg))
    assert chat._r(chat.tool_get_fair_value(ctx))["models"][0]["status"] == "ok"
