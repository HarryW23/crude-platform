"""Excel / CSV 導入：自動識別版式，不要求固定模板。

文件類型：.xlsx / .xlsm（openpyxl）、舊版 .xls（xlrd）、.csv / .txt，以及 OPEC 月報的 PDF（momr.py）。
合併儲存格按 Excel 顯示的樣子展開：合併區域裡每一格都當作有左上角那個值（例如「Brent」橫跨 M1–M12 的表頭）。

能識別三種常見版式：
  wide             一列日期 + 多列數據（最常見；表頭可以有多行，例如名稱一行、RIC 一行）
  wide_transposed  日期橫排在一行，每條序列佔一行
  long             長表：日期、名稱、數值 各一列
識別不了的工作表會標成 unrecognized 並跳過，不會猜。
從 PDF 複製、整行擠在一格的表格（「Saudi Arabia 9,950 9,980」）先拆成格子再識別（split_text_table）。

流程分兩步：先 parse 出預覽（每一列對應到哪條序列、有多少個值），人確認後再帶 mapping 重新 parse 出數據點。
"""
from __future__ import annotations

import csv
import re
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from struct import error as struct_error

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException

from ..ingest import Obs
from ..periods import is_quarter, parse_period

# 讀取上限。超過時不會靜默丟棄：預覽裡會標明這張表被截斷了。
MAX_ROWS, MAX_COLS, HEADER_DEPTH = 200_000, 16_384, 12
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%d-%b-%Y", "%d %b %Y", "%d-%b-%y",
                 "%b %d, %Y", "%Y年%m月%d日", "%d/%m/%Y")
_TIME_TAIL = re.compile(r"[ T]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?$")

Resolver = Callable[[str], str | None]   # 列名 → 序列代碼（找不到回 None）


@dataclass
class ColumnPreview:
    key: str                     # 提交 mapping 時用的鍵
    ref: str                     # 在原文件中的位置，例如 "C 列"
    label: str                   # 表頭文字（多行表頭用 " | " 連起來）
    candidates: list[str]
    series_code: str | None      # 自動識別結果
    n_values: int = 0
    n_skipped: int = 0           # 有日期但值不是數字的格子數
    first_date: str | None = None
    last_date: str | None = None
    last_value: float | None = None
    note: str | None = None


@dataclass
class SheetPreview:
    name: str
    layout: str
    date_ref: str | None = None
    columns: list[ColumnPreview] = field(default_factory=list)
    note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and 19000101 <= value <= 21001231:
        value = str(value)
    if not isinstance(value, str):
        return None
    text = _TIME_TAIL.sub("", value.strip())
    if not 4 <= len(text) <= 14:
        return None
    if (period := parse_period(text)) is not None:      # 2026-09、Sep-26、2026Q3 → 期間第一天
        return period
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        return parsed if 1980 <= parsed.year <= 2100 else None
    return None


def parse_number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("\u2212", "-")     # 排版用的負號 − 也認
        if text.endswith("%"):
            text = text[:-1]
        try:
            number = float(text)
        except ValueError:
            return None
        return number if number == number and abs(number) != float("inf") else None
    return None


# 路透 Workspace / Eikon 的 Excel 插件函數。存檔時可能帶 _xll. 前綴。
REUTERS_FORMULA = re.compile(r"(?<![\w.])(?:_xll\.)?(?:TR|RDP\.\w+|RHistory|RtGet|RData|RSearch)\s*\(", re.IGNORECASE)
BROKEN_ADDIN = "#NAME?"          # 插件函數在沒裝插件的電腦上重算後的錯誤值


class LiveCells:
    """記錄還依賴路透插件的格子。這種文件一離開那台電腦，數字就會變成 #NAME? 或空白，
    導入時只會悄悄少掉一部分數據，所以整個文件拒收，並告訴用戶先貼上為數值。"""

    def __init__(self) -> None:
        self.formulas: list[tuple[str, str, str]] = []     # (工作表, 格子, 公式)
        self.broken: list[tuple[str, str]] = []             # (工作表, 格子)

    def cell(self, sheet: str, ref: str | None, formula: str | None, error: str | None) -> None:
        if formula and REUTERS_FORMULA.search(formula):
            self.formulas.append((sheet, ref or "?", formula))
        elif error == BROKEN_ADDIN:
            self.broken.append((sheet, ref or "?"))

    def check(self) -> None:
        fix = "請在裝了路透的那台電腦上打開原文件：全選 → 複製 → 選擇性貼上「值」，存檔後再導入。"
        if self.formulas:
            sheet, ref, formula = self.formulas[0]
            shown = formula if len(formula) <= 60 else formula[:57] + "…"
            raise ValueError(f"文件裡還有路透公式（例：工作表「{sheet}」{ref}：={shown}），"
                             f"離開裝了路透的電腦就會變成 #NAME? 或空白。{fix}")
        if self.broken:
            sheet, ref = self.broken[0]
            raise ValueError(f"文件裡有 {len(self.broken)} 個 {BROKEN_ADDIN} 錯誤（例：工作表「{sheet}」{ref}），"
                             f"通常是路透公式在沒裝插件的電腦上重算過，這些數據已經丟了。{fix}")


def read_grids(path: Path) -> tuple[dict[str, list[list]], dict[str, str]]:
    """把文件讀成 {工作表名: 二維格子}，以及 {工作表名: 截斷說明}（只含被截斷的表）。"""
    truncated: dict[str, str] = {}
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
        except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError):
            # 文件損壞、其實不是 Excel（只是副檔名對）、或被密碼保護
            raise ValueError("文件無法打開：可能已損壞、不是真正的 Excel 文件，或設了打開密碼") from None
        try:
            live = LiveCells()
            merges = _xlsx_merges(path, live)
            live.check()
            grids = {}
            for ws in wb.worksheets:
                rows: list[list] = []
                width = 0
                for row in ws.iter_rows(values_only=True):       # 不指定 max_col：只讀實際有內容的寬度
                    if len(rows) >= MAX_ROWS:
                        break
                    width = max(width, len(row))
                    rows.append(list(row[:MAX_COLS]))
                grids[ws.title] = _fill_merges(rows, merges.get(ws.title, []))
                if (ws.max_row or len(rows)) > MAX_ROWS or width > MAX_COLS:
                    truncated[ws.title] = (f"這張表有 {ws.max_row or len(rows)} 行 × {width} 列，超過讀取上限"
                                           f"（{MAX_ROWS} 行 × {MAX_COLS} 列），超出的部分沒有讀入")
            return grids, truncated
        finally:
            wb.close()
    if suffix in (".csv", ".txt"):
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "gb18030", "big5"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("無法識別 CSV 的文字編碼")
        as_lines = [[line] if line.strip() else [] for line in text.splitlines()[:MAX_ROWS]]
        if split_text_table(as_lines) is not as_lines:     # 從 PDF 複製的表（數字裡的千分位逗號不是分隔符）
            return {"CSV": as_lines}, truncated
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
        except csv.Error:                     # 只有一列、或沒有分隔符：按普通逗號分隔讀
            dialect = csv.excel
        rows = [[c if c != "" else None for c in row] for row in csv.reader(text.splitlines(), dialect)]
        live = LiveCells()
        for r, row in enumerate(rows[:MAX_ROWS]):
            for c, value in enumerate(row):
                if value is not None and value.strip() == BROKEN_ADDIN:
                    live.cell("CSV", f"{get_column_letter(c + 1)}{r + 1}", None, BROKEN_ADDIN)
        live.check()
        if len(rows) > MAX_ROWS:
            truncated["CSV"] = f"文件有 {len(rows)} 行，超過讀取上限 {MAX_ROWS} 行，超出的部分沒有讀入"
        return {"CSV": rows[:MAX_ROWS]}, truncated
    if suffix == ".xls":
        return _read_xls(path)
    if suffix == ".pdf":                         # 只支援 OPEC 月報的產量表（connectors/momr.py）
        from .momr import read_momr
        return read_momr(path), truncated
    raise ValueError(f"不支援的文件類型：{suffix or '（無副檔名）'}")


MAX_MERGED_CELLS = 1_000_000          # 單個合併區域最多展開這麼多格（防止異常文件把內存撐爆）


def _fill_merges(grid: list[list], ranges: list[tuple[int, int, int, int]]) -> list[list]:
    """把合併區域（0 起算的 首行, 首列, 末行, 末列，含兩端）裡的每一格都填成左上角的值。只填讀入的範圍。"""
    for r0, c0, r1, c1 in ranges:
        if r0 >= len(grid) or c0 >= MAX_COLS:
            continue
        r1, c1 = min(r1, len(grid) - 1), min(c1, MAX_COLS - 1)
        if (r1 - r0 + 1) * (c1 - c0 + 1) > MAX_MERGED_CELLS:
            continue
        top = grid[r0]
        value = top[c0] if c0 < len(top) else None
        if value is None:
            continue
        for r in range(r0, r1 + 1):
            row = grid[r]
            if len(row) <= c1:
                row.extend([None] * (c1 + 1 - len(row)))
            for c in range(c0, c1 + 1):
                if (r, c) != (r0, c0):
                    row[c] = value
    return grid


def _xlsx_merges(path: Path, live: LiveCells | None = None) -> dict[str, list[tuple[int, int, int, int]]]:
    """從 xlsx 裡讀各工作表的合併區域。openpyxl 的只讀模式拿不到合併信息，這裡直接流式讀工作表 XML 裡的 mergeCell，
    不用把整本載入內存。用 openpyxl 自己的 XML 解析入口（裝了 defusedxml 時用它）。讀不到就當沒有合併。
    同一遍順便記下還留著路透公式、或已經變成 #NAME? 的格子（傳了 live 時）。"""
    from openpyxl.utils.cell import range_boundaries
    from openpyxl.xml.functions import fromstring, iterparse
    ns_main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ns_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    out: dict[str, list[tuple[int, int, int, int]]] = {}
    try:
        with zipfile.ZipFile(path) as z:
            book = fromstring(z.read("xl/workbook.xml"))
            rels = fromstring(z.read("xl/_rels/workbook.xml.rels"))
            targets = {r.get("Id"): r.get("Target") for r in rels}
            for sheet in book.iter(f"{ns_main}sheet"):
                target = targets.get(sheet.get(f"{ns_rel}id"))
                if not target:
                    continue
                member = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
                ranges = []
                with z.open(member) as fh:
                    for _event, el in iterparse(fh):
                        if el.tag == f"{ns_main}mergeCell" and el.get("ref"):
                            c0, r0, c1, r1 = range_boundaries(el.get("ref"))
                            ranges.append((r0 - 1, c0 - 1, r1 - 1, c1 - 1))
                        elif el.tag == f"{ns_main}c" and live is not None:
                            f, v = el.find(f"{ns_main}f"), el.find(f"{ns_main}v")
                            live.cell(sheet.get("name"), el.get("r"), f.text if f is not None else None,
                                      v.text if v is not None and el.get("t") == "e" else None)
                        elif el.tag == f"{ns_main}row":
                            el.clear()                         # 數據行不需要：讀過就丟，保持內存小
                if ranges:
                    out[sheet.get("name")] = ranges
    except (KeyError, ValueError, zipfile.BadZipFile, OSError, SyntaxError):
        return {}
    return out


def _read_xls(path: Path) -> tuple[dict[str, list[list]], dict[str, str]]:
    """舊版 .xls（Excel 97–2003）。日期格轉成 datetime，合併儲存格展開。"""
    import xlrd
    from xlrd.biffh import XLRDError
    try:
        try:
            book = xlrd.open_workbook(str(path), formatting_info=True, on_demand=True)   # 合併信息要 formatting_info
        except NotImplementedError:
            book = xlrd.open_workbook(str(path), on_demand=True)
    except (XLRDError, OSError, ValueError, AssertionError, IndexError, struct_error):
        raise ValueError("文件無法打開：可能已損壞、不是真正的 Excel 文件，或設了打開密碼") from None
    grids, truncated = {}, {}
    live = LiveCells()
    try:
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            rows = []
            for r in range(min(sheet.nrows, MAX_ROWS)):
                row = []
                for c in range(min(sheet.ncols, MAX_COLS)):
                    cell = sheet.cell(r, c)
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                        except (ValueError, OverflowError, xlrd.xldate.XLDateError):
                            row.append(None)
                    elif cell.ctype in (xlrd.XL_CELL_NUMBER, xlrd.XL_CELL_TEXT):
                        row.append(cell.value if cell.value != "" else None)
                    else:                                       # 空白、錯誤值（#N/A）、布林
                        error = xlrd.error_text_from_code.get(cell.value) if cell.ctype == xlrd.XL_CELL_ERROR else None
                        if error == BROKEN_ADDIN:
                            live.cell(sheet.name, f"{get_column_letter(c + 1)}{r + 1}", None, BROKEN_ADDIN)
                        row.append(None)
                rows.append(row)
            merges = [(r0, c0, r1 - 1, c1 - 1) for r0, r1, c0, c1 in getattr(sheet, "merged_cells", [])]
            grids[sheet.name] = _fill_merges(rows, merges)
            if sheet.nrows > MAX_ROWS or sheet.ncols > MAX_COLS:
                truncated[sheet.name] = (f"這張表有 {sheet.nrows} 行 × {sheet.ncols} 列，超過讀取上限"
                                         f"（{MAX_ROWS} 行 × {MAX_COLS} 列），超出的部分沒有讀入")
            book.unload_sheet(index)
    finally:
        book.release_resources()
    live.check()
    return grids, truncated


def _pad(grid: list[list]) -> list[list]:
    width = max((len(r) for r in grid), default=0)
    return [list(r) + [None] * (width - len(r)) for r in grid]


def _header_text(value) -> str | None:
    if isinstance(value, str) and value.strip() and parse_number(value) is None and parse_date(value) is None:
        return value.strip()
    return None


def _match(candidates: list[str], label: str, resolver: Resolver) -> tuple[str | None, str | None]:
    """表頭 → 序列。從具體到籠統依次試，哪一層先對上就用哪一層：
      1. 整個表頭（多行合起來，例如合併儲存格「Brent」+ 下一行「M1」= Brent M1）
      2. 相鄰兩行合起來（表頭有三行以上時）
      3. 每一行單獨（例如名稱一行、RIC 一行，各自都能對上同一條序列）
    同一層對上不止一條序列時不猜，標出來讓人選。"""
    tiers = [[label]]
    if len(candidates) > 2:
        tiers.append([f"{a} {b}" for a, b in zip(candidates, candidates[1:], strict=False)])
    tiers.append(candidates)
    for tier in tiers:
        hits = {code for code in (resolver(c) for c in tier) if code}
        if len(hits) == 1:
            return hits.pop(), None
        if len(hits) > 1:
            return None, "表頭同時對上多條序列：" + "、".join(sorted(hits))
    return None, None


def _parse_sheet(name: str, grid: list[list], resolver: Resolver,
                 mapping: dict[str, str | None] | None) -> tuple[SheetPreview, list[Obs]]:
    grid = _pad(grid)
    if not grid or not grid[0]:
        return SheetPreview(name, "unrecognized", note="空白工作表"), []

    def date_counts(g):
        cols = [sum(parse_date(row[j]) is not None for row in g) for j in range(len(g[0]))]
        rows = [sum(parse_date(c) is not None for c in row) for row in g]
        return cols, rows

    col_counts, row_counts = date_counts(grid)
    transposed = max(row_counts) > max(col_counts)
    if transposed:
        grid = [list(col) for col in zip(*grid)]
        col_counts, _ = date_counts(grid)
    if max(col_counts) < 2:
        return SheetPreview(name, "unrecognized", note="找不到日期列（至少要有兩個日期）"), []

    jd = col_counts.index(max(col_counts))
    dates = [parse_date(row[jd]) for row in grid]
    r0 = next(i for i, d in enumerate(dates) if d is not None)
    data_rows = [i for i in range(r0, len(grid)) if dates[i] is not None]
    header_rows = range(max(0, r0 - HEADER_DEPTH), r0)
    # 月度和季度混在一起（例如 OPEC 月報的表：2025、3Q25、Sep25、Oct25）：季度那幾格是匯總（平均），
    # 記在季度第一天會和那個月的數撞在一起、或者被當成那個月的數，不讀；整張都是季度的照常讀
    quarters = [i for i in data_rows if isinstance(grid[i][jd], str) and is_quarter(grid[i][jd])]
    summary_note = None
    if quarters and len(quarters) < len(data_rows):
        data_rows = [i for i in data_rows if i not in set(quarters)]
        labels = "、".join(str(grid[i][jd]).strip() for i in quarters[:4]) + (" 等" if len(quarters) > 4 else "")
        summary_note = f"{labels} 是季度匯總，和月度數據混在一起，沒有讀入"

    def cell_ref(i: int, j: int) -> str:          # 回到原文件的座標
        r, c = (j, i) if transposed else (i, j)
        return f"{name}!{get_column_letter(c + 1)}{r + 1}"

    def line_ref(j: int) -> str:
        return f"第 {j + 1} 行" if transposed else f"{get_column_letter(j + 1)} 列"

    others = [j for j in range(len(grid[0])) if j != jd]
    stats = {}
    for j in others:
        numeric = sum(parse_number(grid[i][j]) is not None for i in data_rows)
        text = sum(_header_text(grid[i][j]) is not None for i in data_rows)
        stats[j] = (numeric, text)

    # ---- 長表：日期重複出現，且有一列主要是文字（名稱列）----
    name_col = next((j for j in others if stats[j][1] >= 0.8 * len(data_rows)), None)
    value_col = next((j for j in others if stats[j][0] >= 0.8 * len(data_rows)), None)
    is_long = (name_col is not None and value_col is not None
               and len({dates[i] for i in data_rows}) < 0.7 * len(data_rows))

    preview = SheetPreview(name, "long" if is_long else ("wide_transposed" if transposed else "wide"),
                           date_ref=line_ref(jd), note=summary_note)
    observations: list[Obs] = []

    def finish(col: ColumnPreview, points: list[tuple[date, float, str]]):
        col.n_values = len(points)
        if points:
            ordered = sorted(points, key=lambda p: p[0])
            col.first_date, col.last_date = ordered[0][0].isoformat(), ordered[-1][0].isoformat()
            col.last_value = ordered[-1][1]
        preview.columns.append(col)
        target = mapping.get(col.key, col.series_code) if mapping is not None else None
        if target:
            observations.extend(Obs(target, d, v, ref) for d, v, ref in points)

    if is_long and name_col is not None and value_col is not None:
        groups: dict[str, list] = {}
        skipped: dict[str, int] = {}
        for i in data_rows:
            label = _header_text(grid[i][name_col])
            if label is None:
                continue
            value = parse_number(grid[i][value_col])
            if value is None:
                skipped[label] = skipped.get(label, 0) + 1
                continue
            groups.setdefault(label, []).append((dates[i], value, cell_ref(i, value_col)))
        for label, points in groups.items():
            code, note = _match([label], label, resolver)
            finish(ColumnPreview(key=f"{name}|name:{label}", ref=f"{line_ref(name_col)}「{label}」", label=label,
                                 candidates=[label], series_code=code, n_skipped=skipped.get(label, 0), note=note),
                   points)
        return preview, observations

    for j in others:
        if stats[j][0] == 0:
            continue
        candidates = [t for t in (_header_text(grid[i][j]) for i in header_rows) if t]
        label = " | ".join(candidates) if candidates else f"（無表頭）{line_ref(j)}"
        code, note = _match(candidates, label, resolver)
        points, skipped_n = [], 0
        for i in data_rows:
            raw = grid[i][j]
            value = parse_number(raw)
            if value is None:
                skipped_n += raw is not None and str(raw).strip() != ""
                continue
            points.append((dates[i], value, cell_ref(i, j)))
        finish(ColumnPreview(key=f"{name}|{j}", ref=line_ref(j), label=label, candidates=candidates,
                             series_code=code, n_skipped=skipped_n, note=note), points)
    return preview, observations


_NO_VALUE = {"-", "–", "—", "n.a.", "na", "n/a", "..."}                 # PDF 表格裡「沒有數」的寫法
_MONTH_TOKEN = re.compile(r"^[A-Za-z]{3}$")


def _tokens(text: str) -> list[str]:
    """按空白切開；「Sep 25」這種被空格拆開的月份接回去。"""
    out: list[str] = []
    for tok in text.split():
        if out and _MONTH_TOKEN.match(out[-1]) and re.fullmatch(r"'?\d{2}(\d{2})?", tok):
            out[-1] = f"{out[-1]} {tok}"
        else:
            out.append(tok)
    return out


def _is_value(tok: str) -> bool:
    return parse_number(tok) is not None or tok.lower() in _NO_VALUE


def split_text_table(grid: list[list]) -> list[list]:
    """從 PDF 複製的表格，貼進 Excel 或存成文字文件後，常常整行擠在一格：「Saudi Arabia 9,950 9,980 30」。
    整張表每行都只有一格文字、而且能看出是「名稱 + 一串數字」的表時，拆成格子；否則原樣返回。

    名稱 = 第一個數字之前的部分（可以有空格，例如 Equatorial Guinea）。各數據行的數字個數必須一樣，
    表頭取數據行之前、最後那幾個詞（個數和數字一樣）裡有能認出的期間（2025、3Q25、Sep25）的那一行。
    對不齊就不拆，不猜。"""
    lines = []
    for row in grid:
        cells = [c for c in row if c not in (None, "")]
        if len(cells) > 1 or (cells and not isinstance(cells[0], str)):
            return grid
        lines.append(_tokens(cells[0]) if cells else [])
    data = []
    for i, toks in enumerate(lines):
        k = next((n for n, t in enumerate(toks) if _is_value(t)), len(toks))
        if k > 0 and len(toks) - k >= 2 and all(_is_value(t) for t in toks[k:]):
            data.append((i, " ".join(toks[:k]), toks[k:]))
    if len(data) < 3 or len({len(v) for _, _, v in data}) != 1:
        return grid
    width = len(data[0][2])
    header = next((i for i in range(data[0][0] - 1, -1, -1) if len(lines[i]) >= width
                   and any(parse_period(t) or re.fullmatch(r"\d{4}", t) for t in lines[i][-width:])), None)
    if header is None:
        return grid
    out: list[list] = [[None] * (width + 1) for _ in grid]      # 行號和原文件一致（數據點記的位置對得上）
    out[header] = [" ".join(lines[header][:-width]) or None, *lines[header][-width:]]   # 表頭前面可以有字
    for i, label, values in data:
        out[i] = [label, *[None if v.lower() in _NO_VALUE else v for v in values]]
    return out


def parse_workbook(path: Path, resolver: Resolver,
                   mapping: dict[str, str | None] | None = None) -> tuple[list[SheetPreview], list[Obs]]:
    """mapping 為 None 時只做預覽；給了 mapping 時，回傳按 mapping（缺省用自動識別結果）取出的數據點。"""
    previews, observations = [], []
    grids, truncated = read_grids(path)
    for name, grid in grids.items():
        preview, obs = _parse_sheet(name, split_text_table(grid), resolver, mapping)
        if name in truncated:
            preview.note = "；".join(filter(None, [preview.note, truncated[name]]))
        previews.append(preview)
        observations.extend(obs)
    return previews, observations
