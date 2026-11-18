"""回測框架。"""
import math
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from crudelab import backtest, signals
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations
from crudelab.models import BacktestRun


def business_days(n, end=date(2026, 10, 23)):
    return list(pd.bdate_range(end=end, periods=n).date)


def put(session, code, days, values, recorded_on_date=False):
    """寫入一條序列。recorded_on_date=True 時，每個值的入庫時間就是它的數據日期（模擬系統當天就收到）。"""
    if recorded_on_date:
        for d, v in zip(days, values, strict=True):
            write_observations(session, [Obs(code, d, float(v))], "manual",
                               now=datetime.combine(d, time(18, 0)))
    else:
        write_observations(session, [Obs(code, d, float(v)) for d, v in zip(days, values, strict=True)], "manual")
    session.commit()


# ------------------------------ 基本組件 ------------------------------

def test_forward_returns_use_the_anchors_own_trading_days():
    days = pd.DatetimeIndex(business_days(30))
    prices = pd.Series(np.exp(np.arange(30) * 0.01), index=days)
    horizons = backtest.DEFAULT_HORIZONS
    fwd = backtest.forward_returns(prices, days, horizons)
    assert fwd["W1"].iloc[0] == pytest.approx(0.05) and fwd["M1"].iloc[0] == pytest.approx(0.21)
    assert fwd["W1"].iloc[-5:].isna().all() and fwd["W1"].iloc[-6] == pytest.approx(0.05)   # 之後不夠 5 天就沒有
    assert fwd["M3"].isna().all()


def test_publication_lag_removes_a_perfect_foresight_leak():
    """一條周度序列的值恰好等於「之後一周的收益」。不扣發佈延遲就會顯得料事如神。"""
    rng = np.random.default_rng(3)
    days = pd.DatetimeIndex(business_days(800))
    prices = pd.Series(70 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days)))), index=days)
    fwd = backtest.forward_returns(prices, days, backtest.DEFAULT_HORIZONS[:1])["W1"]
    fridays = days[days.day_of_week == 4]
    leaky = fwd.reindex(fridays).dropna()                                  # 「周五的數據」= 之後 5 個交易日的收益
    frame = pd.DataFrame({"score": np.sign(leaky)}, index=leaky.index)
    cheat = backtest.as_available(frame, fridays, lag_days=0)["score"]
    honest = backtest.as_available(frame, fridays, lag_days=5)["score"]
    hit = lambda s: (np.sign(s) == np.sign(fwd.reindex(fridays))).loc[s.notna()].mean()  # noqa: E731
    assert hit(cheat) > 0.99 and abs(hit(honest) - 0.5) < 0.1


def test_evaluate_counts_effective_samples_and_significance():
    rng = np.random.default_rng(1)
    idx = pd.RangeIndex(1000)
    scores = pd.Series(rng.uniform(-1, 1, 1000), index=idx)
    noise = backtest.evaluate(scores, pd.Series(rng.normal(0, 1, 1000), index=idx), 21, 1, 0.15, 30)
    assert noise["n"] == 1000 and noise["n_eff"] == pytest.approx(1000 / 21, abs=0.1)
    assert noise["significant"] is False and abs(noise["ic"]) < 0.1
    linked = backtest.evaluate(scores, scores * 0.5 + pd.Series(rng.normal(0, 0.2, 1000), index=idx), 21, 1, 0.15, 30)
    assert linked["significant"] and linked["hit_rate"] > 0.8
    assert linked["hit_low"] < linked["hit_rate"] < linked["hit_high"]
    few = backtest.evaluate(scores.iloc[:100], scores.iloc[:100], 63, 1, 0.15, 30)
    assert few["status"] == "insufficient" and few["significant"] is False and few["ic"] == pytest.approx(1)


# ------------------------------ 和實際信號完全一致 ------------------------------

def test_backtest_scores_match_live_signals_on_each_date(session, cfg):
    """回測裡某一天的分數，必須等於那一天在信號頁上會看到的分數。兩種模式都要。"""
    rng = np.random.default_rng(5)
    days = business_days(120)
    m1 = 70 + np.cumsum(rng.normal(0, 0.5, 120))
    put(session, "BRENT_M1", days, m1, recorded_on_date=True)
    put(session, "BRENT_M3", days, m1 - 1 + rng.normal(0, 0.3, 120), recorded_on_date=True)
    put(session, "WTI_M1", days, m1 - 4, recorded_on_date=True)
    put(session, "WTI_M3", days, m1 - 5 + rng.normal(0, 0.3, 120), recorded_on_date=True)

    checks = [pd.Timestamp(d) for d in (days[40], days[80], days[-1])]
    observation = backtest.run(session, cfg, backtest.Options("observation", step=1))
    vintage = backtest.run(session, cfg, backtest.Options("vintage", start=days[30], step=1))
    obs_points = {p[0]: p[1] for p in observation["points"]["SIG_BRENT_STRUCTURE"]["W1"]["points"]}
    vin_points = {p[0]: p[1] for p in vintage["points"]["SIG_BRENT_STRUCTURE"]["W1"]["points"]}
    for t in checks:
        engine = IndicatorEngine(session, cfg, as_of=datetime.combine(t.date(), time(23, 59, 59)))
        live = signals.data_signal(engine, "SIG_BRENT_STRUCTURE", t)
        key = t.date().isoformat()
        if key in obs_points:                                              # 最後幾天沒有之後的收益，不在散點裡
            assert obs_points[key] == pytest.approx(live["score"])
            assert vin_points[key] == pytest.approx(live["score"])
    assert len(obs_points) > 50 and set(vin_points) <= set(obs_points)


def test_finds_a_real_relationship_and_reports_none_in_noise(session, cfg):
    """人工造一個「裂解價差高 → 之後油價漲」的關係，回測應該看得出來；另一個純噪音信號應該看不出來。"""
    rng = np.random.default_rng(11)
    n = 900
    days = business_days(n)
    t = np.arange(n)
    crack = 20 + 8 * np.sin(2 * np.pi * t / 120) + rng.normal(0, 0.5, n)
    put(session, "WTI_M1", days, [70.0] * n)
    put(session, "RBOB_M1", days, [2.0] * n)
    put(session, "HO_M1", days, (crack + 70) / 14 - 4)                    # 讓 CRK_US_321 正好等於 crack
    drift = 0.003 * (crack - 20) / 8
    put(session, "BRENT_M1", days, 70 * np.exp(np.cumsum(drift + rng.normal(0, 0.004, n))))
    put(session, "WTI_M3", days, 70 + rng.normal(0, 1, n))                # WTI 月差 = 純噪音，和油價無關

    result = backtest.run(session, cfg, backtest.Options())
    by_code = {s["code"]: s for s in result["signals"]}
    crack_month = by_code["SIG_US_321"]["by_horizon"]["M1"]
    assert crack_month["hit_rate"] > 0.7 and crack_month["ic"] > 0.4 and crack_month["significant"]
    noise_week = by_code["SIG_WTI_STRUCTURE"]["by_horizon"]["W1"]
    assert abs(noise_week["ic"]) < 0.25 and not noise_week["significant"]
    assert by_code["SIG_US_CRUDE_STOCKS"]["by_horizon"]["M1"]["status"] == "no_data"
    refinery = next(m for m in result["modules"] if m["module"] == "refinery")
    assert refinery["by_horizon"]["M1"]["ic"] > 0.2
    assert result["overall"]["scored"] > 0 and result["points"]["OVERALL"]["M1"]["slope"] is not None


def test_price_band_coverage_is_calibrated_on_constant_volatility(session, cfg):
    rng = np.random.default_rng(2)
    days = business_days(1500)
    put(session, "BRENT_M1", days, 70 * np.exp(np.cumsum(rng.normal(0, 0.015, 1500))))
    band = backtest.run(session, cfg, backtest.Options())["band"]
    assert band["expected"] == pytest.approx(0.6827, abs=1e-4)
    for code in ("W1", "M1"):
        assert band["by_horizon"][code]["coverage"] == pytest.approx(0.683, abs=0.06)
        assert band["by_horizon"][code]["above"] + band["by_horizon"][code]["below"] == pytest.approx(
            1 - band["by_horizon"][code]["coverage"])


# ------------------------------ 防呆與局限 ------------------------------

def test_guards(session, cfg):
    with pytest.raises(backtest.BacktestError, match="沒有足夠的價格數據"):
        backtest.run(session, cfg, backtest.Options())
    days = business_days(300)
    put(session, "BRENT_M1", days, np.linspace(60, 80, 300))               # 今天才入庫
    with pytest.raises(backtest.BacktestError, match="改用「按數據日期」"):
        backtest.run(session, cfg, backtest.Options("vintage"))
    for options, message in [(backtest.Options("magic"), "回測模式"), (backtest.Options(step=0), "評估間隔"),
                             (backtest.Options(start=date(2026, 9, 1), end=date(2026, 8, 1)), "開始日期"),
                             (backtest.Options(start=date(2030, 1, 1)), "交易日太少")]:
        with pytest.raises(backtest.BacktestError, match=message):
            backtest.run(session, cfg, options)
    result = backtest.run(session, cfg, backtest.Options(step=5))
    assert result["step"] == 5 and result["dates"] == 60
    assert any("換月" in c for c in result["caveats"]) and any("地緣模組不參與" in c for c in result["caveats"])
    approx = {s["code"]: s["approx"] for s in result["signals"]}
    assert approx["SIG_GLOBAL_BALANCE"] and approx["SIG_OECD_STOCKS"] and not approx["SIG_BRENT_STRUCTURE"]


def test_vintage_respects_the_cap(session, cfg, monkeypatch):
    days = business_days(60)
    put(session, "BRENT_M1", days, np.linspace(60, 80, 60), recorded_on_date=True)
    monkeypatch.setattr(cfg.backtest, "vintage_max_dates", 10)
    with pytest.raises(backtest.BacktestError, match="超過上限"):
        backtest.run(session, cfg, backtest.Options("vintage", step=1))


# ------------------------------ 執行、記錄、接口 ------------------------------

def test_api_runs_and_stores_results(sites, session, monkeypatch):
    monkeypatch.setattr(backtest, "start_in_background", lambda cfg, run_id: backtest.execute(cfg, run_id))
    front, admin = sites["front"], sites["admin"]
    assert admin.get("/api/backtests").status_code == 404                   # 研究功能：只在前台
    assert front.post("/api/backtests", json={"step": 0}).status_code == 400
    failed = front.post("/api/backtests", json={}).json()                  # 沒有數據
    assert front.get(f"/api/backtests/{failed['id']}").json()["status"] == "failed"

    put(session, "BRENT_M1", business_days(400), 70 * np.exp(np.cumsum(np.random.default_rng(4).normal(0, 0.01, 400))))
    made = front.post("/api/backtests", json={"mode": "observation", "step": 2}).json()
    detail = front.get(f"/api/backtests/{made['id']}").json()
    assert detail["status"] == "done" and detail["results"]["step"] == 2 and detail["results"]["dates"] == 200
    listing = front.get("/api/backtests").json()
    assert listing[0]["id"] == made["id"] and listing[0]["summary"]["dates"] == 200 and "results" not in listing[0]
    assert listing[1]["status"] == "failed" and "價格數據" in listing[1]["message"]
    assert front.get("/api/backtests/999").status_code == 404


def test_interrupted_runs_are_marked_failed(session):
    row = BacktestRun(created_by="x", status="running", mode="observation", step=1)
    session.add(row)
    session.commit()
    row.created_at -= timedelta(hours=3)
    session.commit()
    backtest.mark_stale(session)
    assert row.status == "failed" and "中斷" in row.message


def test_cli(settings, monkeypatch, capsys, sample_path):
    from crudelab.cli import main
    monkeypatch.setenv("CRUDELAB_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    assert main(["backtest"]) == 1 and "價格數據" in capsys.readouterr().err
    assert main(["import", str(sample_path), "--source", "demo"]) == 0
    capsys.readouterr()
    assert main(["backtest", "--step", "5"]) == 0
    out = capsys.readouterr().out
    assert "［綜合］" in out and "價格區間覆蓋率" in out and math.isfinite(len(out))


# ------------------------------ 信號的歷史表現 ------------------------------

def test_verdicts():
    assert backtest.verdict(None) == "none"
    assert backtest.verdict({"status": "no_data"}) == "none"
    assert backtest.verdict({"status": "insufficient", "significant": False, "ic": 0.5}) == "insufficient"
    assert backtest.verdict({"status": "ok", "significant": True, "ic": 0.2}) == "effective"
    assert backtest.verdict({"status": "ok", "significant": True, "ic": -0.2}) == "reversed"
    assert backtest.verdict({"status": "ok", "significant": False, "ic": 0.4}) == "weak"


def test_track_record_from_latest_run(session, cfg):
    from datetime import datetime as dt

    from crudelab.models import BacktestRun
    assert backtest.track_record(session, cfg)["run"] is None
    good = {"status": "ok", "significant": True, "ic": 0.3, "ic_t": 2.5, "hit_rate": 0.6, "up_share": 0.5,
            "n_eff": 60, "calls": 80}
    by = {"W1": good, "M1": {**good, "ic": -0.3}, "M3": {"status": "insufficient"}}
    sig = cfg.signals["SIG_BRENT_STRUCTURE"]
    results = {"start": "2024-01-01", "end": "2026-10-01", "neutral_band": 0.15,
               "signals": [{"code": sig.code, "input": sig.input, "direction": sig.direction, "by_horizon": by},
                           {"code": "SIG_WTI_STRUCTURE", "input": "SOMETHING_ELSE", "direction": 1, "by_horizon": by}],
               "modules": [{"module": "spread", "by_horizon": by}], "overall": {"by_horizon": by}}
    session.add(BacktestRun(created_by="t", status="done", mode="observation", step=1, results=results,
                            created_at=dt(2026, 9, 1), finished_at=dt(2026, 9, 1)))
    session.add(BacktestRun(created_by="t", status="failed", mode="observation", step=1, results=None))   # 失敗的不算
    session.commit()
    t = backtest.track_record(session, cfg, now=dt(2026, 11, 18))
    assert t["run"]["age_days"] == 78 and any("78 天前" in n for n in t["notes"])
    brent = t["signals"]["SIG_BRENT_STRUCTURE"]
    assert (brent["W1"]["verdict"], brent["M1"]["verdict"], brent["M3"]["verdict"]) == \
        ("effective", "reversed", "insufficient")
    assert brent["W1"]["hit_rate"] == 0.6 and brent["W1"]["verdict_name"] == "有效"
    assert t["signals"]["SIG_WTI_STRUCTURE"]["M1"]["verdict"] == "changed"        # 回測之後改過輸入
    assert t["signals"]["SIG_DUBAI_STRUCTURE"]["M1"]["verdict"] == "none"         # 回測裡沒有
    assert t["modules"]["spread"]["W1"]["verdict"] == "effective" and t["overall"]["M1"]["verdict"] == "reversed"


def test_track_record_api(client, loaded):
    body = client.get("/api/signals/track-record").json()
    assert body["run"] is None and [h["code"] for h in body["horizons"]] == ["W1", "M1", "M3"]
