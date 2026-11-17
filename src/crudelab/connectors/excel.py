"""Excel / CSV 導入：自動識別版式，不要求固定模板。

能識別三種常見版式：
  wide             一列日期 + 多列數據（最常見；表頭可以有多行，例如名稱一行、RIC 一行）
  wide_transposed  日期橫排在一行，每條序列佔一行
  long             長表：日期、名稱、數值 各一列
識別不了的工作表會標成 unrecognized 並跳過，不會猜。

流程分兩步：先 parse 出預覽（每一列對應到哪條序列、有多少個值），人確認後再帶 mapping 重新 parse 出數據點。
"""
from __future__ import annotations

import csv
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from ..ingest import Obs
from ..periods import parse_period

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
        text = value.strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1]
        try:
            number = float(text)
        except ValueError:
            return None
        return number if number == number and abs(number) != float("inf") else None
    return None


def read_grids(path: Path) -> tuple[dict[str, list[list]], dict[str, str]]:
    """把文件讀成 {工作表名: 二維格子}，以及 {工作表名: 截斷說明}（只含被截斷的表）。"""
    truncated: dict[str, str] = {}
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            grids = {}
            for ws in wb.worksheets:
                rows = []
                width = 0
                for row in ws.iter_rows(values_only=True):       # 不指定 max_col：只讀實際有內容的寬度
                    if len(rows) >= MAX_ROWS:
                        break
                    width = max(width, len(row))
                    rows.append(list(row[:MAX_COLS]))
                grids[ws.title] = rows
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
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
        rows = [[c if c != "" else None for c in row] for row in csv.reader(text.splitlines(), dialect)]
        if len(rows) > MAX_ROWS:
            truncated["CSV"] = f"文件有 {len(rows)} 行，超過讀取上限 {MAX_ROWS} 行，超出的部分沒有讀入"
        return {"CSV": rows[:MAX_ROWS]}, truncated
    if suffix == ".xls":
        raise ValueError("不支援舊版 .xls，請在 Excel 裡另存為 .xlsx")
    raise ValueError(f"不支援的文件類型：{suffix or '（無副檔名）'}")


def _pad(grid: list[list]) -> list[list]:
    width = max((len(r) for r in grid), default=0)
    return [list(r) + [None] * (width - len(r)) for r in grid]


def _header_text(value) -> str | None:
    if isinstance(value, str) and value.strip() and parse_number(value) is None and parse_date(value) is None:
        return value.strip()
    return None


def _match(candidates: list[str], label: str, resolver: Resolver) -> tuple[str | None, str | None]:
    hits = {code for code in (resolver(c) for c in [*candidates, label]) if code}
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
                           date_ref=line_ref(jd))
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

    if is_long:
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


def parse_workbook(path: Path, resolver: Resolver,
                   mapping: dict[str, str | None] | None = None) -> tuple[list[SheetPreview], list[Obs]]:
    """mapping 為 None 時只做預覽；給了 mapping 時，回傳按 mapping（缺省用自動識別結果）取出的數據點。"""
    previews, observations = [], []
    grids, truncated = read_grids(path)
    for name, grid in grids.items():
        preview, obs = _parse_sheet(name, grid, resolver, mapping)
        if name in truncated:
            preview.note = "；".join(filter(None, [preview.note, truncated[name]]))
        previews.append(preview)
        observations.extend(obs)
    return previews, observations
