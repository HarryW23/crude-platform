"""OPEC 月報 PDF：只讀二手來源產量表，合計對不上不導入。"""
from datetime import date

import pytest

from crudelab.connectors.momr import MomrError, check_totals
from crudelab.imports import ImportFlowError, commit_batch, preview_file
from crudelab.ingest import load_points, to_series

HEADER = "Secondary sources 2024 2025 3Q26 Aug 26 Sep 26 Oct 26 Oct/Sep"
OPEC = ["Algeria 905 940 958 961 965 970 5", "IR Iran 3,252 3,280 3,270 3,281 3,265 3,240 -25",
        "Saudi Arabia 8,980 9,450 9,700 9,650 9,950 9,980 30",
        "Total OPEC 13,137 13,670 13,928 13,892 14,180 14,190 10"]
DOC = ["Kazakhstan 1,600 1,750 1,800 1,790 1,810 1,820 10", "Russia 9,000 9,100 9,200 9,180 9,250 9,300 50",
       "Total Non-OPEC DoC 10,600 10,850 11,000 10,970 11,060 11,120 60",
       "Total DoC 23,737 24,520 24,928 24,862 25,240 25,310 70"]


def pdf(pages: list[list[str]]) -> bytes:
    """最小的 PDF：每頁幾行文字（Helvetica），抽出文字時一行一行。"""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        body = "BT /F1 9 Tf 40 800 Td 12 TL " + " ".join(
            "(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") ' " for line in lines) + "ET"
        objects.append(f"<< /Length {len(body.encode('latin-1'))} >>\nstream\n{body}\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {len(objects)} 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


REPORT = pdf([
    ["Monthly Oil Market Report", "Supply"],
    ["Table 5 - 7: DoC crude oil production based on secondary sources, tb/d", HEADER, *OPEC, *DOC,
     "Notes: Totals may not add up due to independent rounding.",
     "Table 5 - 8: DoC crude oil production based on direct communication, tb/d", HEADER,
     "Saudi Arabia 8,990 9,460 9,710 9,660 9,960 9,990 30"],
])


def saudi(session):
    points = to_series(load_points(session, ["CRUDE_PROD_SAUDI"]), "CRUDE_PROD_SAUDI")
    return {str(d.date()): v for d, v in points.items()}


def test_secondary_sources_table_is_read_and_direct_communication_is_not(session, settings):
    preview = preview_file(session, settings, REPORT, "momr-november-2026.pdf", "opec_momr", vintage=date(2026, 11, 12))
    found = {c["series_code"]: c["n_values"] for s in preview["sheets"] for c in s["columns"] if c["series_code"]}
    countries = ("ALGERIA", "IRAN", "SAUDI", "OPEC", "KAZAKHSTAN", "RUSSIA", "NON_OPEC_DOC", "DOC")
    assert found == {f"CRUDE_PROD_{c}": 3 for c in countries}
    assert [s["name"] for s in preview["sheets"]] == ["第2頁"]
    commit_batch(session, preview["batch_id"])
    assert saudi(session) == {"2026-08-01": 9650.0, "2026-09-01": 9950.0, "2026-10-01": 9980.0}   # 不是自報的 9,990


def test_totals_that_do_not_add_up_block_the_import(session, settings):
    shifted = list(OPEC)
    shifted[2] = "Saudi Arabia 8,980 9,450 9,700 9,650 9,950 19,980 30"          # 讀錯一個數：合計對不上
    bad = pdf([["Table 5 - 7: DoC crude oil production based on secondary sources, tb/d", HEADER, *shifted, *DOC]])
    with pytest.raises(ImportFlowError, match="Total OPEC 第 6 列是 14190，上面各行加起來是 24190，對不上"):
        preview_file(session, settings, bad, "momr.pdf", "opec_momr")
    with pytest.raises(ImportFlowError, match="沒有找到 OPEC 月報的二手來源產量表"):
        preview_file(session, settings, pdf([["Some other report", "nothing here"]]), "other.pdf", "opec_momr")
    with pytest.raises(ImportFlowError, match="PDF 打不開"):
        preview_file(session, settings, b"%PDF-1.4 broken", "broken.pdf", "opec_momr")


def test_old_reports_with_two_tables_and_rounding():
    """舊版月報分兩張表，Total DoC 在第二張；四捨五入的誤差（每行 0.5）不算對不上。"""
    rows = [("表一", ["A", "1"]), ("表一", ["B", "2"]), ("表一", ["Total OPEC", "4"]),
            ("表二", ["C", "10"]), ("表二", ["Total Non-OPEC DoC", "10"]), ("表二", ["Total DoC", "14"])]
    check_totals(rows)
    with pytest.raises(MomrError, match="Total DoC"):
        check_totals([*rows[:-1], ("表二", ["Total DoC", "16"])])
