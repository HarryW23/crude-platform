"""Kpler（Excel 插件）：模板從配置生成；填好、貼上為數值的模板能導入；還帶著 Kpler 公式的拒收。"""
import io
from datetime import date

import pytest
from openpyxl import load_workbook

from crudelab.imports import ImportFlowError, commit_batch, preview_file
from crudelab.ingest import load_points, to_series
from crudelab.templates import kpler


def filled(cfg, monthly: dict[str, list], weekly: dict[str, list], formula: str | None = None) -> bytes:
    """照說明填模板：每列一條序列，月份 / 日期在第一列（第 3 行起）。"""
    wb = load_workbook(io.BytesIO(kpler(cfg)))
    for title, values, days in (("月度流量", monthly, [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]),
                                ("周度浮倉", weekly, [date(2026, 11, 6), date(2026, 11, 13), date(2026, 11, 20)])):
        sheet = wb[title]
        columns = {sheet.cell(1, j).value: j for j in range(2, sheet.max_column + 1)}
        for i, day in enumerate(days, 3):
            sheet.cell(i, 1, day)
            for header, numbers in values.items():
                sheet.cell(i, columns[header], numbers[i - 3])
    if formula:
        wb["月度流量"]["B5"] = formula
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_template_columns_come_from_config(cfg, admin):
    wb = load_workbook(io.BytesIO(kpler(cfg)))
    assert wb.sheetnames == ["說明", "月度流量", "周度浮倉"]
    monthly = [c.value for c in wb["月度流量"][1]][1:]
    expected = [s.aliases[0] for s in cfg.series.values() if s.provider == "kpler" and s.frequency == "monthly"]
    assert monthly == expected and "Kpler Saudi Arabia crude exports" in monthly
    assert [c.value for c in wb["周度浮倉"][1]][1:] == ["Kpler crude floating storage", "Kpler crude oil on water"]
    response = admin.get("/api/imports/templates/kpler.xlsx")
    assert response.status_code == 200 and response.content[:2] == b"PK"


def test_filled_template_imports_into_kpler_series_not_opec_production(session, settings, cfg):
    content = filled(cfg, {"Kpler Saudi Arabia crude exports": [6800, 6900, 7100],
                           "Kpler China crude imports": [10900, 11200, 11500]},
                     {"Kpler crude floating storage": [62000, 61500, 60800]})
    preview = preview_file(session, settings, content, "kpler.xlsx", "kpler_xls")
    found = {c["series_code"]: c["n_values"] for s in preview["sheets"] for c in s["columns"] if c["n_values"]}
    assert found == {"KPLER_EXP_SAUDI": 3, "KPLER_IMP_CHINA": 3, "KPLER_FLOATING_WORLD": 3}
    commit_batch(session, preview["batch_id"])
    saudi = to_series(load_points(session, ["KPLER_EXP_SAUDI"]), "KPLER_EXP_SAUDI")
    assert list(saudi) == [6800, 6900, 7100] and to_series(load_points(session, ["CRUDE_PROD_SAUDI"]),
                                                           "CRUDE_PROD_SAUDI").empty


def test_files_still_holding_kpler_formulas_are_rejected(session, settings, cfg):
    for formula in ('=Kpler.getFlows("Crude","Export")', "=_xldudf_KPLER_GETTRADES(A1)"):
        content = filled(cfg, {"Kpler Saudi Arabia crude exports": [6800, 6900, 7100]}, {}, formula)
        with pytest.raises(ImportFlowError, match=r"Kpler公式.*「月度流量」B5.*貼上「值」"):
            preview_file(session, settings, content, "kpler.xlsx", "kpler_xls")


def test_wrong_units_are_flagged_against_existing_data(session, settings, cfg):
    """單位錯了（插件給的是桶/日而不是千桶/日）：和庫裡已有的數據差 1000 倍，預覽提醒；升貼水這類不查。"""
    first = filled(cfg, {"Kpler Saudi Arabia crude exports": [6800, 6900, 7100]}, {})
    commit_batch(session, preview_file(session, settings, first, "k1.xlsx", "kpler_xls")["batch_id"])
    wrong = filled(cfg, {"Kpler Saudi Arabia crude exports": [6_810_000, 6_905_000, 7_120_000]}, {})
    warnings = preview_file(session, settings, wrong, "k2.xlsx", "kpler_xls")["warnings"]
    assert any(w.startswith("KPLER_EXP_SAUDI（沙特原油出口（Kpler））這批數據的中位數是 6,905,000") and "1,001 倍" in w
               and "單位是 kb/d" in w for w in warnings)
    right = filled(cfg, {"Kpler Saudi Arabia crude exports": [6810, 6905, 7120]}, {})
    assert not [w for w in preview_file(session, settings, right, "k3.xlsx", "kpler_xls")["warnings"] if "單位" in w]
