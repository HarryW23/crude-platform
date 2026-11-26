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
        if universe == "PLATTS00":                  # 路透真實的錯誤格式：原因代碼在括號裡，在很長的訊息後面
            raise RuntimeError("Error code -1 | No data to return, please check errors: "
                               "ERROR: No successful response.\n"
                               "(TS.Interday.UserNotPermission.92000, User has no permission)")
        days = pd.bdate_range(end="2026-11-17", periods=5)
        if universe == "CNY=":
            return pd.DataFrame({"BID": [7.1] * 5, "MID_PRICE": [7.11, 7.12, 7.13, 7.14, 7.15]}, index=days)
        if universe == "NODATA":
            return pd.DataFrame()
        if universe == "FLAKYc1" and calls.count(("history", "FLAKYc1")) == 1:
            raise RuntimeError("ReadTimeout: timed out")              # 臨時錯誤：第二次就好了
        base = 80.0 if universe.startswith("LCO") else 60.0
        frame = pd.DataFrame({"TRDPRC_1": [base + 9] * 5, "SETTLE": [base + i for i in range(5)]}, index=days)
        if universe == "LCOc1":                                         # 期貨連續合約：路透帶到期日和合約月
            frame["EXPIR_DATE"] = ["2026-11-14"] * 2 + ["2026-12-31"] * 3
            frame["CRT_MNTH"] = ["JAN7"] * 2 + ["FEB7"] * 3
        return frame

    def search(query, top, select):
        calls.append(("search", query))
        if query == "broken":
            raise RuntimeError(f"search failed for {KEY}")
        hits = {"Oman crude oil futures": [("OQDc1", "DME Oman Crude Oil Futures"), ("NODATA", "Old Oman contract")]}
        return pd.DataFrame(hits.get(query, []), columns=["RIC", "DocumentTitle"])

    data.open_session, data.get_history, data.close_session = open_session, get_history, lambda: None
    class Chain:
        def __init__(self, name):
            if name != "0#OQ:":
                raise RuntimeError("unknown chain")
            self.constituents = ["0#OQ:sub", "OQc1", "OQc2", "OQc3", "OQc4"]

    data.discovery = types.SimpleNamespace(search=search, Chain=Chain)
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
    cands.write_text("# 註釋\nOMAN_M1 | Oman crude oil futures | DMOc1 OQDc1\n"
                     "FU_M1 | broken; Nothing here | BADc1 PLATTS00\n"
                     "PTA_CN | | ZTAc1\nURALS_DIFF | | 0#OQ: 0#BAD: # 行尾註釋\n", encoding="utf-8")
    out = tmp_path / "find.txt"
    assert script.main(["find", "--candidates", str(cands), "--out", str(out),
                        "--only", "OMAN_M1,FU_M1,URALS_DIFF"]) == 0
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
    assert got[("FU_M1", "BADc1")][2] == "none" and "***" in got[("FU_M1", "BADc1")][8]
    # 錯誤原因從括號裡的代碼判斷，不會被截掉
    assert got[("FU_M1", "PLATTS00")][8] == ("沒權限（帳號沒訂閱這個數據，常見於 Platts / Argus）"
                                             " [TS.Interday.UserNotPermission.92000]")
    assert ("search", "Nothing here") in fake_lseg                          # ; 分開的第二個搜索詞也搜了
    # 鏈展開成合約（跳過子鏈，只取前 3 個），展不開的鏈直接略過
    assert [r[1] for r in rows[1:] if r[0] == "URALS_DIFF"] == ["0#BAD:", "OQc1", "OQc2", "OQc3"]
    assert got[("URALS_DIFF", "0#BAD:")][8].startswith("鏈展開不了")
    assert got[("URALS_DIFF", "OQc1")][7] == "鏈 0#OQ: 的成員"
    assert "3 條序列裡，2 條找到了" in printed and "搜索「broken」失敗" in printed

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


def test_explain_error_and_comment_stripping():
    script = load_script()
    explained = script.explain_error("(TS.Interday.UserRequestError.70005, The universe is not found)")
    assert explained.startswith("RIC 或請求無效")
    assert script.explain_error("ERROR: No successful response. No data to return") == "這段時間沒有數據"
    assert script.strip_comment("OMAN 0#OQ: SETTLE   # 說明") == "OMAN 0#OQ: SETTLE"
    assert script.strip_comment("# 整行註釋") == ""


def test_lseg_ref_in_config_feeds_rics_file_and_import_aliases(cfg, session):
    """配置裡的 lseg: {ric, field}（含曲線模板的月序偏移）會進 lseg_rics.txt，也會成為導入時認得的表頭。"""
    from crudelab.imports import make_resolver
    from crudelab.master import sync_to_db
    text = lseg_rics_text(cfg)
    assert "DUBAI_SWAP_M1            DCBc2            SETTLE" in text            # 迪拜掉期 M1 = DCBc2（offset 1）
    assert "DUBAI_SWAP_M12           DCBc13" in text
    assert "ESPO_DIFF                ESPO-DUB         TRDPRC_1" in text
    sync_to_db(session, cfg)
    resolve = make_resolver(session)
    assert resolve("ESPO-DUB") == "ESPO_DIFF" and resolve("DCBc2") == "DUBAI_SWAP_M1"
    from crudelab.schema import CurveDef
    with pytest.raises(ValueError):
        CurveDef(code="X", name_zh="x", commodity="X", location="X", kind="swap", unit="USD/bbl", months=[1],
                 lseg={"ric": "NOPLACEHOLDER"})                              # 曲線的 RIC 模板必須有 {n}


def test_chain_expansion_never_hangs_and_library_thread_errors_stay_quiet(fake_lseg, monkeypatch, capsys):
    """路透庫在鏈不存在時，錯誤在它自己的推送線程裡拋出，主線程可能一直等：要限時，也不能刷一屏 traceback。"""
    import threading
    import time as real_time
    script = load_script()
    client = script.Client(KEY)
    monkeypatch.setattr(script, "CHAIN_TIMEOUT", 0.2)

    class Hanging:
        def __init__(self, name):
            def boom():
                raise RuntimeError("Error code NotFound | The record could not be found")
            t = threading.Thread(target=boom, name="Msg-Proc-ThreadOMMSTREAMING_CHAINS_0.0")
            t.start()
            t.join()
            threading.Event().wait(5)                               # 然後一直不返回（time.sleep 被夾具換掉了）

    monkeypatch.setattr(sys.modules["lseg.data"].discovery, "Chain", Hanging)
    started = real_time.monotonic()
    assert client.chain("0#GX0010551S1630M:") == []
    assert real_time.monotonic() - started < 2
    assert "Traceback" not in capsys.readouterr().err


def test_proxy_rics_are_labelled_in_rics_file_and_series_api(cfg, client):
    """用替代數據的序列：lseg_rics.txt 行尾寫明（腳本讀的時候當註釋），序列接口帶出說明給後台顯示。"""
    script = load_script()
    text = lseg_rics_text(cfg)
    line = next(x for x in text.splitlines() if x.startswith("OMAN_M1 "))
    assert "# 替代：" in line and script.strip_comment(line).split() == ["OMAN_M1", "OMANCALMc1", "TRDPRC_1"]
    rows = {r["code"]: r for r in client.get("/api/series").json()}
    assert rows["OMAN_M1"]["ric"] == "OMANCALMc1" and rows["OMAN_M1"]["proxy"].startswith("阿曼掉期評估價")
    assert rows["BRENT_M1"]["ric"] == "LCOc1" and rows["BRENT_M1"]["proxy"] is None
    assert rows["JAPAN_NAPHTHA"]["ric"] == "NAF-1H-TYO" and rows["JAPAN_NAPHTHA"]["proxy"] is None


def test_export_writes_contract_sidecar_and_retries_transient_errors(fake_lseg, tmp_path, capsys):
    script = load_script()
    rics = tmp_path / "rics.txt"
    rics.write_text("BRENT_M1 LCOc1\nWTI_M1 FLAKYc1\nUSDCNY CNY=\n", encoding="utf-8")
    out = tmp_path / "reuters_x.csv"
    assert script.main(["export", "--days", "10", "--out", str(out), "--rics", str(rics)]) == 0
    assert fake_lseg.count(("history", "FLAKYc1")) == 2                  # 超時重試了一次
    assert out.read_text(encoding="utf-8").splitlines()[0] == "日期,BRENT_M1,WTI_M1,USDCNY"
    side = tmp_path / "reuters_x_contracts.csv"
    rows = side.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "日期,序列代碼,RIC,到期日,合約月" and len(rows) == 6          # 只有帶合約字段的序列
    assert rows[1].endswith(",BRENT_M1,LCOc1,2026-11-14,JAN7") and rows[-1].endswith(",2026-12-31,FEB7")
    assert "crudelab verify-rolls" in capsys.readouterr().out

    # 合約對照文件可以直接交給平台核對
    from crudelab import rollcheck
    data = rollcheck.read_contracts(side)
    assert rollcheck.actual_rolls(data["BRENT_M1"]) == [date(2026, 11, 13)]

    # 沒權限不重試
    rics.write_text("FU_M1 PLATTS00\n", encoding="utf-8")
    assert script.main(["export", "--out", str(tmp_path / "o.csv"), "--rics", str(rics)]) == 1
    assert fake_lseg.count(("history", "PLATTS00")) == 1


def test_find_stops_trying_chains_when_none_expand(fake_lseg, tmp_path, capsys, monkeypatch):
    script = load_script()
    monkeypatch.setattr(script, "CHAIN_TIMEOUT", 0.1)
    cands = tmp_path / "c.txt"
    cands.write_text("A | | 0#X1: 0#X2: 0#X3: 0#X4: 0#X5:\n", encoding="utf-8")
    out = tmp_path / "f.txt"
    assert script.main(["find", "--candidates", str(cands), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "這次不再試鏈" in printed
    errors = [line.split("\t")[8] for line in out.read_text(encoding="utf-8").splitlines()[1:]]
    assert errors.count("沒試：這台機器上鏈都展不開") == 2 and len(errors) == 5


def test_find_candidates_can_name_the_field(fake_lseg, tmp_path):
    """國債 RIC 同時有價格和收益率：候選寫 RIC@字段 取指定的那個；沒有那個字段時說清楚有哪些。"""
    script = load_script()
    cands = tmp_path / "c.txt"
    cands.write_text("MACRO_UST10Y | | LCOc1@TRDPRC_1 LCOc1@MID_YLD_1\n", encoding="utf-8")
    out = tmp_path / "f.txt"
    assert script.main(["find", "--candidates", str(cands), "--out", str(out)]) == 0
    rows = [line.split("\t") for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0][6] == f"近 {script.FIND_DAYS} 天點數"
    assert rows[1][1:4] == ["LCOc1", "ok", "TRDPRC_1"] and rows[1][5] == "89"      # 不是預設的 SETTLE
    assert rows[2][2] == "none" and rows[2][8].startswith("沒有字段 MID_YLD_1（有：TRDPRC_1、SETTLE")


def test_search_lists_rics_and_titles_without_fetching(fake_lseg, tmp_path, capsys):
    """search 只列出搜索結果，不取數；結果文件只有搜索詞、RIC、標題。"""
    script = load_script()
    out = tmp_path / "search.txt"
    assert script.main(["search", "Oman crude oil futures", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "OQDc1" in printed and "DME Oman Crude Oil Futures" in printed
    assert not [c for c in fake_lseg if c[0] == "history"]                         # 沒有取數
    rows = [line.split("\t") for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0] == ["搜索詞", "RIC", "標題"]
    assert rows[1] == ["Oman crude oil futures", "OQDc1", "DME Oman Crude Oil Futures"]
    assert script.main(["search", "nothing", "--out", str(out)]) == 1               # 沒有結果：退出碼 1
