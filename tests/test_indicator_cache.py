"""跨請求共用的指標計算結果：數據、參數、配置任何一樣變了都要重算；情境、歷史時點、固定合約不能混進來。"""
import threading
from datetime import date, datetime

import pytest

from crudelab import indicators, parameters
from crudelab.config import load_config
from crudelab.indicators import IndicatorEngine, Scenario
from crudelab.ingest import Obs, write_observations
from crudelab.settings import PROJECT_ROOT


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(indicators, "_shared", None)


def last(engine, code):
    return engine.compute(code).iloc[-1]


def test_second_request_reuses_results(loaded, cfg):
    first = IndicatorEngine(loaded, cfg)
    spread = first.compute("SPR_BRENT_WTI")
    summary = first.summary("SPR_BRENT_WTI")
    second = IndicatorEngine(loaded, cfg)
    assert second.compute("SPR_BRENT_WTI") is spread                    # 沒有重算
    assert second.summary("SPR_BRENT_WTI") == summary
    second.summary("SPR_BRENT_WTI")["last_value"] = -1                 # 拿到的是副本，改了不影響共用的
    assert IndicatorEngine(loaded, cfg).summary("SPR_BRENT_WTI") == summary


def test_any_data_write_invalidates(loaded, cfg):
    engine = IndicatorEngine(loaded, cfg)
    day = engine.compute("SPR_BRENT_WTI").index[-1].date()
    before = last(engine, "SPR_BRENT_WTI")
    brent = engine.series_values("BRENT_M1")[str(day)]
    write_observations(loaded, [Obs("BRENT_M1", day, float(brent) + 5)], "manual")
    loaded.commit()
    after = IndicatorEngine(loaded, cfg)
    assert last(after, "SPR_BRENT_WTI") == pytest.approx(before + 5)
    assert after.summary("SPR_BRENT_WTI")["last_value"] == pytest.approx(before + 5)


def test_parameter_change_and_config_reload_invalidate(loaded, cfg):
    engine = IndicatorEngine(loaded, cfg)
    before = last(engine, "CRK_US_RBOB")                               # RBOB × 每桶加侖數 − WTI
    parameters.set_value(loaded, "GAL_PER_BBL", 43, valid_from=date(1900, 1, 1))
    loaded.commit()
    changed = IndicatorEngine(loaded, cfg)
    rbob = changed.series_values("RBOB_M1").iloc[-1]
    assert last(changed, "CRK_US_RBOB") == pytest.approx(before + rbob)
    reloaded = load_config(PROJECT_ROOT / "config")                     # 配置重新載入：新的對象，不用舊結果
    assert IndicatorEngine(loaded, reloaded)._computed is not changed._computed


def test_scenarios_history_and_pinned_views_do_not_mix_in(loaded, cfg):
    plain = IndicatorEngine(loaded, cfg)
    base = last(plain, "SPR_BRENT_WTI")
    shifted = IndicatorEngine(loaded, cfg, scenario=Scenario(shifts={"BRENT_M1": 10.0}))
    assert last(shifted, "SPR_BRENT_WTI") == pytest.approx(base + 10)
    past = IndicatorEngine(loaded, cfg, as_of=datetime(2000, 1, 1))
    assert past.compute("SPR_BRENT_WTI").empty
    anchor = plain.compute("TS_BRENT_M1_M2").index[-30]                 # 固定在一個月前那組合約上算一遍
    plain.pinned(anchor).summary("TS_BRENT_M1_M2")
    again = IndicatorEngine(loaded, cfg)
    assert last(again, "SPR_BRENT_WTI") == pytest.approx(base)
    assert again.summary("TS_BRENT_M1_M2") == IndicatorEngine(loaded, cfg)._summary("TS_BRENT_M1_M2")


def test_concurrent_requests_see_complete_results(loaded, settings, cfg):
    """幾個請求同時開始算：共用的原始數據分組不能被讀到分了一半。"""
    from crudelab import db
    expected = {code: IndicatorEngine(loaded, cfg).summary(code) for code in ("SPR_BRENT_WTI", "CRK_US_321")}
    for _ in range(3):
        indicators._shared = None
        results, errors = [], []

        def work(results=results, errors=errors):
            session = db.get_session()
            try:
                engine = IndicatorEngine(session, cfg)
                results.append({code: engine.summary(code) for code in expected})
            except Exception as exc:            # pragma: no cover - 失敗時看得到原因
                errors.append(exc)
            finally:
                session.close()
        threads = [threading.Thread(target=work) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors and all(r == expected for r in results)
