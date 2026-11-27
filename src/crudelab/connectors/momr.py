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
from datetime import date
from pathlib import Path

from .excel import join_broken_lines, parse_number, split_text_table, text_row, tokens

CAPTION = re.compile(r"crude\s+oil\s+production\s+based\s+on\s+secondary\s+sources", re.I)
END = re.compile(r"^(notes?|sources?)\s*:|^table\s+\d", re.I)    # 表後面的註釋（Notes: / Source:）、表的標題
GRAND_TOTAL = "total doc"


class MomrError(ValueError):
    pass


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
            values = [parse_number(r[j]) for r in parts]
            stated = parse_number(row[j])
            if stated is None or not values or any(v is None for v in values):
                continue
            expected = sum(v for v in values if v is not None)
            if abs(expected - stated) > 0.5 * len(parts) + 0.5:
                raise MomrError(f"{where}：{row[0]} 第 {j} 列是 {stated:g}，上面各行加起來是 {expected:g}，對不上。"
                                "多半是 PDF 裡的表格式變了、拆錯了列；這個文件沒有導入，請改用導入模板貼上")
        if label != GRAND_TOTAL:
            totals.append(row)
        group = []


def _tables(lines: list[str]) -> list[list[str]]:
    """一頁文字裡的二手來源產量表：每張表回傳「表頭區 + 數據行」那幾行，交給 split_text_table 拆。

    新版月報兩張表的標題在前、表身在後（PDF 的文字順序），不能按標題找表身；按表身找：
    連續的「名稱 + 一串數字」行是一張表，往上到前一個註釋（Notes / Source）或標題為止是它的表頭區。
    表頭區裡要有 secondary（表頭格寫著 Secondary sources），有 direct communication 的不要。"""
    def is_row(text: str) -> bool:
        return text_row(tokens(text)) is not None

    found, i = [], 0
    while i < len(lines):
        if not is_row(lines[i]):
            i += 1
            continue
        end = i
        while end + 1 < len(lines) and (is_row(lines[end + 1]) or not lines[end + 1].strip()):
            end += 1
        start = i
        while start > 0 and not END.match(lines[start - 1]) and not CAPTION.search(lines[start - 1]):
            start -= 1
        zone = " ".join(lines[start:i]).lower()
        if "secondary" in zone and "direct" not in zone:
            found.append(lines[start:end + 1])
        i = end + 1
    return found


def read_momr(path: Path) -> dict[str, list[list]]:
    """{工作表名: 格子}：每張二手來源產量表一個。找不到、拆不開、合計對不上都報錯。"""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        pages = [page.extract_text() or "" for page in PdfReader(path).pages]
    except (PdfReadError, OSError, ValueError) as exc:
        raise MomrError(f"PDF 打不開：{exc}") from None
    return read_pages(pages)


def read_pages(pages: list[str]) -> dict[str, list[list]]:
    """每頁抽出的文字 → {工作表名: 格子}（read_momr 的後半；測試直接用真實月報抽出的文字）。"""
    out: dict[str, list[list]] = {}
    rows: list[tuple[str, list]] = []                  # (在哪張表, 數據行)：舊版分兩張表，Total DoC 在第二張
    missed: list[str] = []
    for p, text in enumerate(pages, 1):
        captions = [line.strip() for line in text.splitlines() if CAPTION.search(line)]
        if not captions:
            continue
        where = f"第 {p} 頁的表「{captions[0][:80]}」"
        tables = _tables(join_broken_lines([line.strip() for line in text.splitlines()]))
        if not tables:                  # 只提到標題的頁（例如表目錄），表不在這頁
            missed.append(where)
            continue
        for n, block in enumerate(tables, 1):
            grid = [[x] if x else [] for x in block]
            split = split_text_table(grid)
            if split is grid:
                raise MomrError(f"{where} 讀不出：各行的數字個數對不齊，或找不到月份表頭。"
                                "這個文件沒有導入，請改用導入模板貼上")
            filled = [row for row in split if any(v is not None for v in row)]
            rows += [(where, row) for row in filled[1:]]           # 第一行是表頭
            out[f"第{p}頁" + (f"（{n}）" if len(tables) > 1 else "")] = split
    if out and not any(str(row[0]).strip().lower().startswith("total") for _, row in rows):
        raise MomrError(f"{rows[0][0] if rows else '這個 PDF'}：表裡沒有任何合計行（Total OPEC / Total DoC），"
                        "國別數字無法用月報的合計核對，這個文件沒有導入。請確認是完整的月報 PDF")
    check_totals(rows)
    for p, text in enumerate(pages, 1):
        balance = _balance(text, p)
        if balance is not None:
            out[f"第{p}頁 供需平衡"] = balance
            break
    if not out and missed:
        raise MomrError(f"{missed[0]} 讀不出：找不到表頭寫著 Secondary sources 的表身。"
                        "這個文件沒有導入，請改用導入模板貼上")
    if not out:
        raise MomrError("這個 PDF 裡沒有找到 OPEC 月報的二手來源產量表"
                        "（標題含 crude oil production based on secondary sources）。目前 PDF 只支援 OPEC 月報")
    return out


BALANCE_CAPTION = re.compile(r"world\s+oil\s+demand\s+and\s+production\s+balance,", re.I)   # 表 11-1，不是 11-2（變化）
BALANCE_ROWS = (                        # 要的行：月報裡的寫法 → 別名（config/master/series/balance.yaml）
    (re.compile(r"^\(a\)\s*total world demand$", re.I), "momr:balance:(a) Total world demand"),
    (re.compile(r"^\(b\)\s*total non-doc liquids production and doc ngls$", re.I),
     "momr:balance:(b) Total Non-DoC liquids production and DoC NGLs"),
    (re.compile(r"^\(a\)-\(b\)$", re.I), "momr:balance:(a) - (b)"),
)


def _balance(text: str, page: int) -> list[list] | None:
    """附錄表 11-1（World oil demand and production balance, mb/d）裡的全球需求、(b)、(a) − (b) 三行。
    列是年度和季度（2025、1Q26 …）：季度記在季度第一天，年度列不讀（一般表格識別本來就不讀年度）。
    表頭和這三行的數字個數必須一樣，對不齊就報錯，不猜。這頁不是表 11-1 時回 None。"""
    # 「(a) - (b)」的「-」會被當成「沒有數」：先接成一個詞
    lines = [re.sub(r"\(a\)\s*-\s*\(b\)", "(a)-(b)", line.strip()) for line in text.splitlines()]
    if not any(BALANCE_CAPTION.search(line) for line in lines):
        return None
    where = f"第 {page} 頁的表「World oil demand and production balance」"
    header = None
    for line in lines:
        toks = tokens(line)
        periods = [t for t in toks if re.fullmatch(r"\d{4}|[1-4]Q\d{2}", t)]
        if len(periods) >= 6 and toks[-len(periods):] == periods:
            header = periods
            break
    if header is None:
        raise MomrError(f"{where}：找不到年度 / 季度的表頭（2025、1Q26 …）。這個文件沒有導入，請截圖給負責平台的人")
    grid: list[list] = [[None, *header]]
    pending = ""                                     # 「(b) Total Non-DoC liquids」在 PDF 裡斷成兩行
    for line in lines:
        row = text_row(tokens(f"{pending} {line}".strip()))
        label = row[0] if row else f"{pending} {line}".strip()
        for pattern, alias in BALANCE_ROWS:
            if pattern.match(label):
                if row is None:
                    pending = label
                    break
                if len(row[1]) != len(header):
                    raise MomrError(f"{where}：「{label}」有 {len(row[1])} 個數，表頭有 {len(header)} 列，對不上。"
                                    "這個文件沒有導入")
                grid.append([alias, *row[1]])
                pending = ""
                break
        else:
            pending = ""
            if row is None and re.match(r"^\(b\)", line):
                pending = line
    if len(grid) != len(BALANCE_ROWS) + 1:
        found = {r[0] for r in grid[1:]}
        lost = [a.split(":", 2)[2] for _, a in BALANCE_ROWS if a not in found]
        raise MomrError(f"{where}：沒找到 {'、'.join(lost)} 這幾行。這個文件沒有導入，請截圖給負責平台的人")
    return grid


_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                "november", "december")
_COVER_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(_MONTH_NAMES) + r")\s+(\d{4})\b", re.I)


def publication_date(path: Path) -> date | None:
    """封面上的發佈日期（第一頁「11 November 2026」），作為報告期。讀不到回 None。"""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        text = PdfReader(path).pages[0].extract_text() or ""
    except (PdfReadError, OSError, ValueError, IndexError):
        return None
    m = _COVER_DATE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), _MONTH_NAMES.index(m.group(2).lower()) + 1, int(m.group(1)))
    except ValueError:
        return None
