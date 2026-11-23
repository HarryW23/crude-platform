from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from crudelab.connectors.excel import parse_date, parse_number
from crudelab.imports import ImportFlowError, commit_batch, preview_file
from crudelab.ingest import load_points, to_series
from crudelab.models import Observation


@pytest.fixture()
def sample_bytes(sample_path):
    return sample_path.read_bytes()


def _cols(preview, sheet):
    return {c["label"]: c for s in preview["sheets"] if s["name"] == sheet for c in s["columns"]}


def test_messy_workbook_layouts_are_detected(session, settings, sample_bytes):
    preview = preview_file(session, settings, sample_bytes, "sample.xlsx", "demo")
    layouts = {s["name"]: s["layout"] for s in preview["sheets"]}
    assert layouts == {"期貨": "wide", "新加坡成品油": "wide_transposed", "升貼水": "long", "中國": "wide",
                       "EIA周度": "wide", "STEO月度": "wide", "遠月合約": "wide", "持倉": "wide",
                       "說明": "unrecognized"}
    held = _cols(preview, "持倉")
    assert {c["series_code"] for c in held.values()} == {"CFTC_WTI_MM_LONG", "CFTC_WTI_MM_SHORT", "CFTC_WTI_OI",
                                                        "ICE_BRENT_MM_LONG", "ICE_BRENT_MM_SHORT", "ICE_BRENT_OI"}
    back = _cols(preview, "遠月合約")
    assert back["Brent M12 | LCOc12"]["series_code"] == "BRENT_M12"
    assert back["迪拜掉期M2"]["series_code"] == "DUBAI_SWAP_M2"
    assert all(c["series_code"] for c in back.values())
    steo = _cols(preview, "STEO月度")["OPEC液體產量"]                   # 表頭是「2026-09」這種期間寫法
    assert steo["series_code"] == "STEO_OPEC_SUPPLY" and steo["n_values"] == 96
    assert (steo["first_date"], steo["last_date"]) == ("2020-01-01", "2027-12-01")
    assert _cols(preview, "中國")["美元人民幣"]["series_code"] == "USDCNY"
    assert _cols(preview, "中國")["SC M1"]["n_values"] == 55            # 國慶假期 5 個交易日沒有數據

    futures = _cols(preview, "期貨")
    assert futures["Brent M1 | LCOc1"]["series_code"] == "BRENT_M1"
    assert futures["迪拜掉期M1"]["series_code"] == "DUBAI_SWAP_M1"
    assert futures["我自己的備註列"]["series_code"] is None
    assert futures["WTI M1 | CLc1"]["n_values"] == 59 and futures["WTI M1 | CLc1"]["n_skipped"] == 1
    assert _cols(preview, "新加坡成品油")["新加坡柴油"]["series_code"] == "SG_GASOIL_10PPM"
    assert _cols(preview, "升貼水")["ESPO Diff"]["n_values"] == 60


def test_commit_writes_points_with_cell_refs_and_is_idempotent(session, settings, sample_bytes):
    preview = preview_file(session, settings, sample_bytes, "sample.xlsx", "demo")
    result = commit_batch(session, preview["batch_id"])
    session.commit()
    assert result["updated"] == 0 and result["unmapped_columns"] == 1
    assert result["inserted"] == session.scalar(select(func.count()).select_from(Observation))

    df = load_points(session, ["BRENT_M1", "SG_JET_KERO", "URALS_DIFF"])
    first = {r.series_code: r for r in df.sort_values("obs_date").drop_duplicates("series_code").itertuples()}
    assert first["BRENT_M1"].obs_date == date(2026, 8, 3) and first["BRENT_M1"].source_ref == "期貨!B5"
    assert first["SG_JET_KERO"].source_ref == "新加坡成品油!B4"
    assert first["URALS_DIFF"].source_ref == "升貼水!C3"

    again = preview_file(session, settings, sample_bytes, "sample.xlsx", "demo")
    assert any("已導入過" in w for w in again["warnings"])
    result2 = commit_batch(session, again["batch_id"])
    assert result2["inserted"] == 0 and result2["updated"] == 0
    with pytest.raises(ImportFlowError, match="已經入庫"):
        commit_batch(session, again["batch_id"])


def test_manual_mapping_is_learned_for_next_time(session, settings, sample_bytes):
    preview = preview_file(session, settings, sample_bytes, "sample.xlsx", "demo")
    key = _cols(preview, "期貨")["我自己的備註列"]["key"]
    skip = _cols(preview, "期貨")["Brent M2 | LCOc2"]["key"]
    result = commit_batch(session, preview["batch_id"], {key: "TD3C_WS", skip: None})
    session.commit()
    assert result["learned_aliases"] == 1
    points = load_points(session, ["TD3C_WS", "BRENT_M2"])
    assert len(to_series(points, "TD3C_WS")) == 60 and to_series(points, "BRENT_M2").empty   # 設為 None 的列不入庫
    again = preview_file(session, settings, sample_bytes, "sample.xlsx", "demo")
    assert _cols(again, "期貨")["我自己的備註列"]["series_code"] == "TD3C_WS"

    with pytest.raises(ImportFlowError, match="未知的序列代碼"):
        commit_batch(session, again["batch_id"], {key: "NOPE"})


def test_csv_and_bad_files(session, settings):
    csv = b"Date,LCOc1,CLc1\n2026-09-01,70.1,66.5\n2026-09-02,\"1,070.5\",n/a\n"
    preview = preview_file(session, settings, csv, "x.csv", "reuters_xls")
    cols = _cols(preview, "CSV")
    assert cols["LCOc1"]["series_code"] == "BRENT_M1" and cols["LCOc1"]["last_value"] == 1070.5
    assert cols["CLc1"]["n_values"] == 1 and cols["CLc1"]["n_skipped"] == 1
    with pytest.raises(ImportFlowError, match="無法打開"):                 # 壞掉的 .xls：給說明
        preview_file(session, settings, b"x", "old.xls", "reuters_xls")
    with pytest.raises(ImportFlowError, match="不支援"):
        preview_file(session, settings, b"x", "notes.docx", "reuters_xls")
    with pytest.raises(ImportFlowError, match="無法打開"):                 # 損壞的文件：給說明，不崩潰
        preview_file(session, settings, b"not really a workbook", "broken.xlsx", "reuters_xls")
    with pytest.raises(ImportFlowError, match="不能用於文件導入"):
        preview_file(session, settings, csv, "x.csv", "kpler")


def test_cell_parsers():
    assert parse_date("03-Aug-2026") == date(2026, 8, 3)
    assert parse_date("2026/8/3 00:00:00") == date(2026, 8, 3)
    assert parse_date(20260803) == date(2026, 8, 3)
    assert parse_date(70.5) is None and parse_date("Brent") is None and parse_date("12345") is None
    assert parse_date("2026-09") == date(2026, 9, 1) and parse_date("Sep-26") == date(2026, 9, 1)
    assert parse_date("2026Q3") == date(2026, 7, 1) and parse_date("3Q26") == date(2026, 7, 1)
    assert parse_date("2026年9月") == date(2026, 9, 1) and parse_date("Q5 2026") is None
    assert parse_date("2026.09") is None and parse_date("May") is None           # 像數字或不完整的不算
    assert parse_number("1,234.5") == 1234.5 and parse_number("N/A") is None and parse_number(True) is None


def test_wide_transposed_sheets_are_not_silently_truncated(session, settings, tmp_path, monkeypatch):
    from openpyxl import Workbook

    from crudelab.connectors import excel
    wb = Workbook()
    ws = wb.active
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(900)]           # 橫排 900 個日期
    ws.append(["品種", *days])
    ws.append(["新加坡柴油", *[90 + i * 0.01 for i in range(900)]])
    path = tmp_path / "wide.xlsx"
    wb.save(path)
    preview = preview_file(session, settings, path.read_bytes(), "wide.xlsx", "demo")
    column = preview["sheets"][0]["columns"][0]
    assert column["n_values"] == 900 and column["last_date"] == days[-1].isoformat() and preview["warnings"] == []

    monkeypatch.setattr(excel, "MAX_COLS", 100)                                  # 真的超過上限時：明說，不靜默
    cut = preview_file(session, settings, path.read_bytes(), "wide.xlsx", "demo")
    assert cut["sheets"][0]["columns"][0]["n_values"] == 99
    assert any("沒有讀入" in w for w in cut["warnings"])



# ------------------------------ 合併儲存格、舊版 .xls（#45） ------------------------------

def _merged_rows():
    """「Brent」「WTI」各橫跨三列（合併儲存格），下一行才是 M1、M2、M3。"""
    return [["日期", "Brent", None, None, "WTI", None, None],
            [None, "M1", "M2", "M3", "M1", "M2", "M3"],
            [date(2026, 9, 1), 70.1, 69.5, 69.0, 66.5, 66.1, 65.8],
            [date(2026, 9, 2), 70.4, 69.8, 69.2, 66.9, 66.4, 66.0]]


def test_merged_header_cells_in_xlsx(session, settings, tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "價格"
    for row in _merged_rows():
        ws.append(row)
    ws.merge_cells("B1:D1")
    ws.merge_cells("E1:G1")
    path = tmp_path / "merged.xlsx"
    wb.save(path)
    cols = _cols(preview_file(session, settings, path.read_bytes(), "merged.xlsx", "reuters_xls"), "價格")
    got = {label: c["series_code"] for label, c in cols.items()}
    assert got == {"Brent | M1": "BRENT_M1", "Brent | M2": "BRENT_M2", "Brent | M3": "BRENT_M3",
                   "WTI | M1": "WTI_M1", "WTI | M2": "WTI_M2", "WTI | M3": "WTI_M3"}
    assert cols["WTI | M3"]["last_value"] == 66.0 and cols["Brent | M1"]["n_values"] == 2


def test_legacy_xls_with_dates_merges_and_errors(session, settings, tmp_path):
    xlwt = pytest.importorskip("xlwt")                       # 只用來在測試裡造 .xls
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Prices")
    day = xlwt.easyxf(num_format_str="YYYY-MM-DD")
    rows = _merged_rows()
    ws.write(0, 0, rows[0][0])
    ws.write_merge(0, 0, 1, 3, "Brent")
    ws.write_merge(0, 0, 4, 6, "WTI")
    for j, v in enumerate(rows[1][1:], start=1):
        ws.write(1, j, v)
    for i, row in enumerate(rows[2:], start=2):
        ws.write(i, 0, row[0], day)                            # 真正的日期格（不是文字）
        for j, v in enumerate(row[1:], start=1):
            ws.write(i, j, xlwt.Formula("NA()") if (i, j) == (3, 6) else v)   # 一格錯誤值 #N/A：當作沒有值
    path = tmp_path / "old.xls"
    wb.save(str(path))
    preview = preview_file(session, settings, path.read_bytes(), "old.xls", "reuters_xls")
    cols = _cols(preview, "Prices")
    assert cols["Brent | M2"]["series_code"] == "BRENT_M2" and cols["Brent | M2"]["first_date"] == "2026-09-01"
    assert cols["WTI | M3"]["n_values"] == 1 and cols["WTI | M3"]["last_value"] == 65.8


def test_header_matching_prefers_the_most_specific_tier():
    from crudelab.connectors.excel import _match
    table = {"brent": "DATED_BRENT", "brentm1": "BRENT_M1", "lcoc1": "BRENT_M1", "clc1": "WTI_M1"}
    resolve = lambda text: table.get(text.lower().replace(" ", "").replace("|", ""))   # noqa: E731
    assert _match(["Brent", "M1"], "Brent | M1", resolve) == ("BRENT_M1", None)        # 整個表頭最具體
    assert _match(["標題", "Brent", "M1"], "標題 | Brent | M1", resolve) == ("BRENT_M1", None)   # 相鄰兩行
    assert _match(["Brent M1", "LCOc1"], "Brent M1 | LCOc1", resolve) == ("BRENT_M1", None)   # 名稱 + RIC
    code, note = _match(["LCOc1", "CLc1"], "LCOc1 | CLc1", resolve)
    assert code is None and "多條序列" in note                                             # 衝突：不猜


def test_files_still_tied_to_the_reuters_addin_are_rejected(session, settings, tmp_path):
    """路透插件公式一離開那台電腦就變成 #NAME? 或空白：導入時整個文件拒收，提示先貼上為數值。"""
    from openpyxl import Workbook

    def book(cell):
        wb = Workbook()
        ws = wb.active
        ws.title = "價格"
        ws.append(["Date", "LCOc1", "CLc1"])
        ws.append([date(2026, 9, 1), 70.1, 66.5])
        ws.append([date(2026, 9, 2), 70.4, 66.9])
        ws["C3"] = cell
        path = tmp_path / "r.xlsx"
        wb.save(path)
        return path.read_bytes()

    def preview(content, name="r.xlsx"):
        return preview_file(session, settings, content, name, "reuters_xls")

    for formula in ('=TR("CLc1","TR.SETTLEMENTPRICE")', '=_xll.RDP.HistoricalPricing("CLc1")', "=RHistory(B2)"):
        with pytest.raises(ImportFlowError, match=r"路透公式.*「價格」C3.*貼上「值」"):
            preview(book(formula))
    with pytest.raises(ImportFlowError, match=r"#NAME\? 錯誤.*「價格」C3"):
        preview(book("#NAME?"))                                    # openpyxl 把它存成錯誤值格
    with pytest.raises(ImportFlowError, match=r"#NAME\? 錯誤.*「CSV」C3"):
        preview(b"Date,LCOc1,CLc1\n2026-09-01,70.1,66.5\n2026-09-02,70.4,#NAME?\n", "r.csv")

    # 普通公式、#N/A（路透導出裡常見的休市空值）、名字裡碰巧有 TR 的函數：照常導入
    for ok in ("=B3*0.95", "=TRIM(B3)", "=STR(1)", "#N/A"):
        assert _cols(preview(book(ok)), "價格")["LCOc1"]["series_code"] == "BRENT_M1"


def test_preview_warns_about_repeated_quotes(session, settings):
    """導入預覽：升貼水連續 5 天以上同一個數，提醒是數據商沿用舊值；期貨結算價不變不提醒。"""
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(10)]
    lines = ["Date,FORTIES_DIFF,BRENT_M1"] + [f"{d},{1.15 if i < 7 else 0.2 * i},80" for i, d in enumerate(days)]
    preview = preview_file(session, settings, "\n".join(lines).encode(), "r.csv", "reuters_xls")
    hits = [w for w in preview["warnings"] if "連續" in w]
    assert len(hits) == 1 and hits[0].startswith("FORTIES_DIFF（福蒂斯升貼水）有 1 段連續 5 次以上相同的值")
    assert "從 2026-09-01 起連續 7 個數據點都是 1.15" in hits[0]
