"""LSEG 導出腳本：用假的 lseg.data 模擬 Workspace，驗證導出的 CSV 能被 crudelab 正確識別入庫，且不洩漏 App Key。"""
import importlib.util
import sys
import types
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from crudelab.data_needs import lseg_rics_text
from crudelab.imports import commit_batch, preview_file

ROOT = Path(__file__).resolve().parents[1]
KEY = "lseg-fake-appkey-5f1c0b"                 # 測試用的假 App Key  # secret-scan: ok


def load_script():
    spec = importlib.util.spec_from_file_location("lseg_export", ROOT / "scripts" / "lseg_export.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def fake_lseg(monkeypatch):
    calls = []
    lseg = types.ModuleType("lseg")
    data = types.ModuleType("lseg.data")

    def open_session(app_key=None):
        calls.append(("open", app_key))

    def get_history(universe, interval, start, end):
        calls.append(("history", universe))
        if universe == "BADc1":
            raise RuntimeError(f"no permission for key {KEY}")
        days = pd.bdate_range(end="2026-11-17", periods=5)
        if universe == "CNY=":
            return pd.DataFrame({"BID": [7.1] * 5, "MID_PRICE": [7.11, 7.12, 7.13, 7.14, 7.15]}, index=days)
        base = 80.0 if universe.startswith("LCO") else 60.0
        return pd.DataFrame({"TRDPRC_1": [base + 9] * 5, "SETTLE": [base + i for i in range(5)]}, index=days)

    data.open_session, data.get_history, data.close_session = open_session, get_history, lambda: None
    lseg.data = data
    monkeypatch.setitem(sys.modules, "lseg", lseg)
    monkeypatch.setitem(sys.modules, "lseg.data", data)
    monkeypatch.setenv("LSEG_APP_KEY", KEY)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return calls


def test_rics_file_is_generated_from_config(cfg):
    assert (ROOT / "scripts" / "lseg_rics.txt").read_text(encoding="utf-8") == lseg_rics_text(cfg), \
        "scripts/lseg_rics.txt 過期了：運行 crudelab lseg-rics --output scripts/lseg_rics.txt"


def test_export_produces_importable_csv(fake_lseg, tmp_path, capsys, session, settings, cfg):
    script = load_script()
    rics = tmp_path / "rics.txt"
    rics.write_text("# 註釋\nBRENT_M1 LCOc1\nWTI_M1 CLc1\nUSDCNY CNY=\nBRENT_M2 BADc1\n", encoding="utf-8")
    out = tmp_path / "out.csv"
    assert script.main(["export", "--days", "10", "--out", str(out), "--rics", str(rics)]) == 0
    printed = capsys.readouterr().out
    assert KEY not in printed and "***" in printed and "BADc1" in printed       # 錯誤裡的 key 被遮住
    assert ("open", KEY) in fake_lseg
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "日期,BRENT_M1,WTI_M1,USDCNY"

    preview = preview_file(session, settings, out.read_bytes(), "reuters.csv", "reuters_xls")
    mapped = {c["label"]: c["series_code"] for c in preview["sheets"][0]["columns"]}
    assert mapped == {"BRENT_M1": "BRENT_M1", "WTI_M1": "WTI_M1", "USDCNY": "USDCNY"}
    result = commit_batch(session, preview["batch_id"])
    assert result["inserted"] == 15
    from crudelab.indicators import IndicatorEngine
    brent = IndicatorEngine(session, cfg).series_values("BRENT_M1")
    assert brent.iloc[-1] == 84.0 and brent.index[-1].date() == date(2026, 11, 17)   # 用的是結算價 SETTLE


def test_bad_rics_file_and_missing_library(tmp_path, monkeypatch):
    script = load_script()
    bad = tmp_path / "rics.txt"
    bad.write_text("ONLY_ONE_COLUMN\n", encoding="utf-8")
    with pytest.raises(script.Fail, match="第 1 行"):
        script.read_rics(bad)
    for name in ("lseg", "lseg.data", "refinitiv", "refinitiv.data", "eikon"):
        monkeypatch.setitem(sys.modules, name, None)                  # 模擬都沒裝
    with pytest.raises(script.Fail, match="pip install lseg-data"):
        script.Client("k")
