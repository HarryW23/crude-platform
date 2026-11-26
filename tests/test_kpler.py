"""Kpler 終端下載的表：原樣導入；只收要的拆分方式；這一期不讀；沒有序列的國家不列出；帶 Kpler 公式的拒收。"""
import io
from datetime import date

import pytest
from openpyxl import Workbook

from crudelab.connectors.kpler import adapt
from crudelab.imports import ImportFlowError, commit_batch, preview_file
from crudelab.ingest import load_points, to_series
from crudelab.periods import parse_period

TODAY = date.today()
THIS_MONTH = f"{TODAY:%Y-%m}"


def kpler_file(title: str, header: list, rows: list[list]) -> bytes:
    """照 Kpler 下載的樣子：一張工作表，名字是圖的標題（截到 31 個字），第一列 date。"""
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    ws.append(["date", *header])
    for row in rows:
        ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


EXPORTS = kpler_file("World monthly exports (Crude-Co by origin country)",
                     ["Saudi Arabia", "Russian Federation", "Norway"],
                     [["2026-07", 4562.0, 5708.0, 1290.0], ["2026-08", 2448.2, 5606.1, 1300.0],
                      ["2026-09", 6700.4, 5529.0, 1350.0],
                      [THIS_MONTH, 5054.6, 5115.1, 1434.7]])
EXPORTS_NAME = "World_monthly_exports_Crude_Co_by_origin_country___2026-11-26_00h28.xlsx"


def values(session, code):
    return {str(d.date()): v for d, v in to_series(load_points(session, [code]), code).items()}


def test_exports_download_imports_as_is_without_the_unfinished_month(session, settings):
    preview = preview_file(session, settings, EXPORTS, EXPORTS_NAME, "kpler_xls")
    sheet = preview["sheets"][0]
    assert {c["series_code"]: c["n_values"] for c in sheet["columns"]} == {"KPLER_EXP_SAUDI": 3, "KPLER_EXP_RUSSIA": 3}
    assert "另有 1 列（國家 / 分類）平台裡沒有對應的序列，跳過" in sheet["note"]          # Norway 不一一列出
    assert any(f"{THIS_MONTH} 是這個月，還沒結束" in w for w in preview["warnings"])
    commit_batch(session, preview["batch_id"])
    assert values(session, "KPLER_EXP_SAUDI") == {"2026-07-01": 4562.0, "2026-08-01": 2448.2, "2026-09-01": 6700.4}
    assert values(session, "CRUDE_PROD_SAUDI") == {}                           # 不會對到 OPEC 月報的沙特產量


def test_weekly_inventories_use_iso_weeks(session, settings):
    week = f"{TODAY.isocalendar()[0]}-W{TODAY.isocalendar()[1]:02d}"
    content = kpler_file("World weekly inventories including commodities-on-water",
                         ["Level (Kb)", "Onshore", "Oil in Transit on Ships", "Floating Storage", "Revisit Rate"],
                         [["2026-W46", 4615842.5, 3375302.7, 1146813.5, 93726.3, 0.67],
                          ["2026-W47", 4605316.5, 3399249.8, 1109027.1, 97039.6, 0.71],
                          [week, 4553562.3, 3393204.3, 1067677.3, 92680.7, 0.22]])
    name = "World_weekly_inventories_including_commodities-on-water_by_onshore_offshore_status.xlsx"
    preview = preview_file(session, settings, content, name, "kpler_xls")
    commit_batch(session, preview["batch_id"])
    assert values(session, "KPLER_FLOATING_WORLD") == {"2026-11-09": 93726.3, "2026-11-16": 97039.6}   # 那一周的周一
    assert parse_period("2026-W48") == date(2026, 11, 23) and parse_period("2026-W54") is None


def test_wrong_split_or_renamed_file_is_rejected(session, settings):
    """進口按來源國拆分：「United States」是從美國運出的量，不是美國的進口，整個文件拒收並說明怎麼下載。"""
    content = kpler_file("World monthly imports (Crude-Co by origin country)", ["United States", "China"],
                         [["2026-09", 4224.8, 10.0]])
    with pytest.raises(ImportFlowError, match="拆分方式不對.*Destination"):
        preview_file(session, settings, content, "World_monthly_imports_Crude_Co_by_origin_country.xlsx", "kpler_xls")
    with pytest.raises(ImportFlowError, match="文件名"):                        # 文件名改過：認不出
        preview_file(session, settings, EXPORTS, "kpler.xlsx", "kpler_xls")
    imports = kpler_file("World monthly imports (Crude-Co by destination country)", ["China", "South Korea"],
                         [["2026-08", 11000.0, 2800.0], ["2026-09", 11200.0, 2900.0]])
    preview = preview_file(session, settings, imports, "World_monthly_imports_Crude_Co_by_destination_country.xlsx",
                           "kpler_xls")
    assert {c["series_code"] for c in preview["sheets"][0]["columns"]} == {"KPLER_IMP_CHINA", "KPLER_IMP_KOREA"}


def test_other_workbooks_are_left_alone(tmp_path):
    grids = {"價格": [["Date", "Saudi Arabia"], ["2026-09", 9950]]}
    assert adapt(tmp_path / "x.xlsx", grids) == {} and grids["價格"][0] == ["Date", "Saudi Arabia"]


def test_files_still_holding_kpler_formulas_are_rejected(session, settings):
    wb = Workbook()
    ws = wb.active
    ws.append(["Date", "BRENT_M1"])
    ws.append([date(2026, 9, 1), 70.0])
    ws["B3"] = '=Kpler.getFlows("Crude","Export")'
    out = io.BytesIO()
    wb.save(out)
    with pytest.raises(ImportFlowError, match=r"Kpler公式.*B3.*貼上「值」"):
        preview_file(session, settings, out.getvalue(), "k.xlsx", "kpler_xls")


def test_wrong_units_are_flagged_against_existing_data(session, settings):
    """單位錯了（給的是桶/日而不是千桶/日）：和庫裡已有的數據差 1000 倍，預覽提醒。"""
    commit_batch(session, preview_file(session, settings, EXPORTS, EXPORTS_NAME, "kpler_xls")["batch_id"])
    wrong = kpler_file("World monthly exports (Crude-Co by origin country)", ["Saudi Arabia"],
                       [["2026-08", 2_448_200.0], ["2026-09", 6_700_400.0], ["2026-07", 4_562_000.0]])
    warnings = preview_file(session, settings, wrong, EXPORTS_NAME, "kpler_xls")["warnings"]
    assert any(w.startswith("KPLER_EXP_SAUDI") and "單位是 kb/d" in w for w in warnings)


def test_long_history_is_not_mistaken_for_a_unit_change(session, settings):
    """運費一年裡漲十幾倍：十年歷史（中位數 55）和最近的水平（1000 上下）差很多，不是單位問題，不提醒。"""
    from datetime import timedelta
    days = [date(2016, 1, 4) + timedelta(days=7 * i) for i in range(560)]
    vals = [55.0] * 530 + [1000.0] * 30
    lines = ["Date,TD3C_WS"] + [f"{d},{v}" for d, v in zip(days[-40:], vals[-40:], strict=True)]
    first = preview_file(session, settings, "\n".join(lines).encode(), "r1.csv", "reuters_xls")
    commit_batch(session, first["batch_id"])
    full = ["Date,TD3C_WS"] + [f"{d},{v}" for d, v in zip(days, vals, strict=True)]
    warnings = preview_file(session, settings, "\n".join(full).encode(), "r2.csv", "reuters_xls")["warnings"]
    assert not [w for w in warnings if "單位" in w]


def test_kpler_download_dropped_into_inbox_is_recorded_as_kpler(session, settings, cfg):
    import os
    import time

    from sqlalchemy import select

    from crudelab import jobs
    from crudelab.models import Observation
    inbox = settings.data_dir / "inbox"
    inbox.mkdir(parents=True)
    path = inbox / EXPORTS_NAME
    path.write_bytes(EXPORTS)
    old = time.time() - 120
    os.utime(path, (old, old))
    assert jobs.run_now(cfg, settings, "IMPORT_INBOX")["status"] == "ok"
    sources = set(session.scalars(select(Observation.source_code).where(Observation.series_code == "KPLER_EXP_SAUDI")))
    assert sources == {"kpler_xls"}
