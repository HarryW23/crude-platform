"""OPEC 月報（MOMR）PDF：取出「二手來源」產量表（#14）。

OPEC 官網有防爬蟲，平台不能自己去下載；在瀏覽器下載月報 PDF 後，原樣在後台導入頁上傳即可。
只讀標題含「crude oil production based on secondary sources」的表（新版是一張 DoC 表，舊版分 OPEC 和
非 OPEC DoC 兩張），不讀「direct communication」（各國自報）那張：兩張表的行名一樣，讀錯了會悄悄混進去。

PDF 抽出的文字是一行一行的（「Saudi Arabia 8,980 9,450 … 30」），用 excel.split_text_table 拆成格子，
之後和其他表格一樣識別（年度、季度、變化列自動跳過）。
讀出來以後核對合計：各國加起來要等於 Total OPEC、Total Non-OPEC DoC，兩個合計加起來等於 Total DoC
（月報各數四捨五入到千桶/日，允許每行 0.5 的誤差）。對不上說明拆錯了列，整個文件不導，不猜。
"""
from __future__ import annotations

import re
from pathlib import Path

CAPTION = re.compile(r"crude\s+oil\s+production\s+based\s+on\s+secondary\s+sources", re.I)
END = re.compile(r"^(notes?|sources?)\b|^table\s+\d", re.I)       # 表後面的註釋、下一張表
GRAND_TOTAL = "total doc"


class MomrError(ValueError):
    pass


def _number(text) -> float | None:
    from .excel import parse_number
    return parse_number(text)


def check_totals(rows: list[tuple[str, list]]) -> None:
    """各國之和 = 合計（每一列都查）。rows：(在哪張表, 拆好的數據行 [名稱, 值…])，按表裡的順序。"""
    group: list[list] = []
    totals: list[list] = []
    for where, row in rows:
        label = str(row[0]).strip().lower()
        if not label.startswith("total"):
            group.append(row)
            continue
        parts = totals if label == GRAND_TOTAL and totals else group
        for j in range(1, len(row)):
            values = [_number(r[j]) for r in parts]
            stated = _number(row[j])
            if stated is None or not values or any(v is None for v in values):
                continue
            expected = sum(v for v in values if v is not None)
            if abs(expected - stated) > 0.5 * len(parts) + 0.5:
                raise MomrError(f"{where}：{row[0]} 第 {j} 列是 {stated:g}，上面各行加起來是 {expected:g}，對不上。"
                                "多半是 PDF 裡的表格式變了、拆錯了列；這個文件沒有導入，請改用導入模板貼上")
        if label != GRAND_TOTAL:
            totals.append(row)
        group = []


def read_momr(path: Path) -> dict[str, list[list]]:
    """{工作表名: 格子}：每張二手來源產量表一個。找不到、拆不開、合計對不上都報錯。"""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    from .excel import split_text_table
    try:
        pages = [page.extract_text() or "" for page in PdfReader(path).pages]
    except (PdfReadError, OSError, ValueError) as exc:
        raise MomrError(f"PDF 打不開：{exc}") from None
    out: dict[str, list[list]] = {}
    rows: list[tuple[str, list]] = []                  # (在哪張表, 數據行)：舊版分兩張表，Total DoC 在第二張
    for p, text in enumerate(pages, 1):
        lines = [line.strip() for line in text.splitlines()]
        for i, line in enumerate(lines):
            if not CAPTION.search(line) or re.search(r"direct\s+communication", line, re.I):
                continue
            block = [line]
            for later in lines[i + 1:]:
                if END.match(later):
                    break
                block.append(later)
            grid = [[x] if x else [] for x in block]
            split = split_text_table(grid)
            where = f"第 {p} 頁的表「{line[:80]}」"
            if split is grid:
                raise MomrError(f"{where} 讀不出：各行的數字個數對不齊，或找不到月份表頭。"
                                "這個文件沒有導入，請改用導入模板貼上")
            filled = [row for row in split if any(v is not None for v in row)]
            rows += [(where, row) for row in filled[1:]]           # 第一行是表頭
            out[f"第{p}頁"] = split
    check_totals(rows)
    if not out:
        raise MomrError("這個 PDF 裡沒有找到 OPEC 月報的二手來源產量表"
                        "（標題含 crude oil production based on secondary sources）。目前 PDF 只支援 OPEC 月報")
    return out
