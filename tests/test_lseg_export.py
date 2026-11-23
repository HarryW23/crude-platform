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


def load_script(listening=(9000,)):
    """載入腳本；listening 是假裝在聽的本機端口（測試機上沒有 Workspace）。"""
    spec = importlib.util.spec_from_file_location("lseg_export", ROOT / "scripts" / "lseg_export.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.port_open = lambda port: port in listening
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
        if universe == "NODATA":
            return pd.DataFrame()
        base = 80.0 if universe.startswith("LCO") else 60.0
        return pd.DataFrame({"TRDPRC_1": [base + 9] * 5, "SETTLE": [base + i for i in range(5)]}, index=days)

    def search(query, top, select):
        calls.append(("search", query))
        if query == "broken":
            raise RuntimeError(f"search failed for {KEY}")
        hits = {"Oman crude oil futures": [("OQDc1", "DME Oman Crude Oil Futures"), ("NODATA", "Old Oman contract")]}
        return pd.DataFrame(hits.get(query, []), columns=["RIC", "DocumentTitle"])

    data.open_session, data.get_history, data.close_session = open_session, get_history, lambda: None
    data.discovery = types.SimpleNamespace(search=search)
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


def test_candidates_cover_exactly_the_series_without_ric():
    """lseg_candidates.txt 要和 lseg_rics.txt 裡「還不知道 RIC」的清單一致：RIC 寫進配置後，候選裡那一行就該刪掉。"""
    import re
    script = load_script()
    codes = [code for code, _, _ in script.read_candidates(ROOT / "scripts" / "lseg_candidates.txt", None)]
    unknown = re.findall(r"^# (\w+)\s+\?", (ROOT / "scripts" / "lseg_rics.txt").read_text(encoding="utf-8"), re.M)
    assert len(codes) == len(set(codes)) and set(codes) == set(unknown)


def test_find_tries_guesses_and_search_hits(fake_lseg, tmp_path, capsys):
    script = load_script()
    cands = tmp_path / "cands.txt"
    cands.write_text("# 註釋\nOMAN_M1 | Oman crude oil futures | DMOc1 OQDc1\nFU_M1 | broken | BADc1\n"
                     "PTA_CN | | ZTAc1\n", encoding="utf-8")
    out = tmp_path / "find.txt"
    assert script.main(["find", "--candidates", str(cands), "--out", str(out), "--only", "OMAN_M1,FU_M1"]) == 0
    printed = capsys.readouterr().out
    assert KEY not in printed and KEY not in out.read_text(encoding="utf-8")       # 錯誤裡的 key 被遮住
    assert ("search", "Oman crude oil futures") in fake_lseg and ("history", "ZTAc1") not in fake_lseg   # --only
    rows = [line.split("\t") for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0][:3] == ["序列代碼", "RIC", "結果"]
    got = {(r[0], r[1]): r for r in rows[1:]}
    # 候選在前、搜索結果在後；同一個 RIC 只試一次，標題用候選標記
    assert [r[1] for r in rows[1:] if r[0] == "OMAN_M1"] == ["DMOc1", "OQDc1", "NODATA"]
    assert got[("OMAN_M1", "OQDc1")][2:6] == ["ok", "SETTLE", "2026-11-17", "64"]
    assert got[("OMAN_M1", "NODATA")][2] == "none"
    assert got[("FU_M1", "BADc1")][2] == "none" and "***" in got[("FU_M1", "BADc1")][7]
    assert "2 條序列裡，1 條找到了" in printed and "搜索「broken」失敗" in printed

    with pytest.raises(script.Fail, match="格式不對"):
        cands.write_text("ONLY | \n", encoding="utf-8")
        script.read_candidates(cands, None)
    cands.write_text("OMAN_M1 | x | y\n", encoding="utf-8")
    assert script.main(["find", "--candidates", str(cands), "--only", "NOPE"]) == 1


def test_no_proxy_or_closed_session_gives_restart_steps(fake_lseg, tmp_path, monkeypatch, capsys):
    script = load_script(listening=())
    rics = tmp_path / "rics.txt"
    rics.write_text("BRENT_M1 LCOc1\n", encoding="utf-8")
    assert script.main(["export", "--rics", str(rics), "--out", str(tmp_path / "o.csv")]) == 1
    assert "托盤完全退出" in capsys.readouterr().out and not any(c[0] == "open" for c in fake_lseg)   # 沒開會話就停下

    # 舊的 .portInUse 指向沒在聽的端口：probe 說明那是舊記錄
    appdata = tmp_path / "appdata"
    (appdata / "Refinitiv" / "Data API Proxy").mkdir(parents=True)
    (appdata / "Refinitiv" / "Data API Proxy" / ".portInUse").write_text("9123", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    assert script.main(["probe"]) == 1
    printed = capsys.readouterr().out
    assert "數據通道：沒有在聽" in printed and "記著端口 9123" in printed and "舊記錄" in printed

    # 端口在聽，但庫返回一個沒打開的會話（它不拋錯）：不能誤報「已打開」
    script = load_script()
    closed = types.SimpleNamespace(open_state=types.SimpleNamespace(name="Closed"))
    monkeypatch.setattr(sys.modules["lseg.data"], "open_session", lambda app_key=None: closed)
    assert script.main(["probe"]) == 1
    printed = capsys.readouterr().out
    assert "數據通道：端口 9000 在聽" in printed and "會話沒有打開（狀態：Closed）" in printed
    assert "已用" not in printed
