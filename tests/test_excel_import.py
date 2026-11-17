from datetime import date

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
                       "EIA周度": "wide", "STEO月度": "wide", "說明": "unrecognized"}
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
    with pytest.raises(ImportFlowError, match="不支援"):
        preview_file(session, settings, b"x", "old.xls", "reuters_xls")
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
