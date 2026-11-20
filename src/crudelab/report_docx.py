"""報告導出為 Word（#40）。

輸入是報告的全文 Markdown（reports.full_markdown：正文 + 解讀），和頁面、Markdown 下載是同一份內容，
所以以後改報告的內容只要改 reports.py，Word 版自動跟着變。這裡只管「怎麼排」：

  # / ## / ### / ####   標題 1–4
  | … |                  表格（第二行是分隔線）；數字列右對齊，表頭加粗並在跨頁時重複
  **…**                  粗體
  其他非空行             段落（解讀部分按原文分段）

樣式：config/reports/template.docx 存在時以它為底（字體、標題樣式、頁邊距、頁眉頁腳、公司抬頭都在 Word 裡設計，
不用改代碼）；沒有時用內置的樣式，並把中文字體設為 DEFAULT_CJK_FONT。頁腳加「第 X 頁」和生成說明。
報告的格式以後會改，改的時候優先改模板文件，其次改這裡的排版規則，不要在別處另寫一套。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

DEFAULT_CJK_FONT = "Microsoft JhengHei"        # 微軟正黑體：Windows 自帶；Mac 上 Word 會自動換成近似字體
DEFAULT_LATIN_FONT = "Calibri"
TABLE_STYLE = "Table Grid"
TABLE_FONT_PT = 9                               # 表格裡的字號：研究報告常用，比正文小一號
CELL_PADDING = Pt(12)                           # 單元格左右邊距合計
_HEADING = re.compile(r"^(#{1,4}) (.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_NUMBER = re.compile(r"^[−+\-]?[\d,]+(\.\d+)?%?$|^—$")
WARNING = ("請勿採信", "數字核對未通過")


def _set_cjk_font(style, cjk: str, latin: str) -> None:
    style.font.name = latin
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    for attr, value in (("w:ascii", latin), ("w:hAnsi", latin), ("w:eastAsia", cjk)):
        fonts.set(qn(attr), value)


def _inline(paragraph, text: str, bold: bool = False) -> None:
    """把 **粗體** 拆成粗體和普通的 run。"""
    pos = 0
    for match in _BOLD.finditer(text):
        if match.start() > pos:
            paragraph.add_run(text[pos:match.start()]).bold = bold or None
        paragraph.add_run(match.group(1)).bold = True
        pos = match.end()
    if pos < len(text):
        paragraph.add_run(text[pos:]).bold = bold or None


def _row_flag(row, tag: str) -> None:
    """表格行屬性：tblHeader = 跨頁時重複這一行；cantSplit = 這一行不要被分頁切開。"""
    flag = OxmlElement(tag)
    flag.set(qn("w:val"), "true")
    row._tr.get_or_add_trPr().append(flag)


def _display_width(text: str) -> int:
    """估算一格字的顯示寬度：中文等全角字算 2，其他算 1。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text.replace("**", ""))


def _column_widths(document, rows: list[list[str]], columns: int, numeric: list[bool]) -> list[int]:
    """每列先拿到放下最長內容所需的寬度（按 TABLE_FONT_PT 估算，加單元格邊距）；有剩餘的話只分給文字列。
    放不下時才按比例壓縮。這樣日期、數字不會被擠到換行，指標名這類長文字多分一些。"""
    section = document.sections[0]
    usable = section.page_width - section.left_margin - section.right_margin
    half_em = Pt(TABLE_FONT_PT * 0.55)                  # 一個半角字的寬度（估算）
    need = [max((_display_width(r[c]) for r in rows if c < len(r)), default=0) * half_em + CELL_PADDING
            for c in range(columns)]
    cap = [min(n, usable * 0.4) for n in need]          # 一列最多佔四成：長說明文字換行，不擠掉別的列
    spare = usable - sum(cap)
    if spare < 0:
        return [int(c * usable / sum(cap)) for c in cap]
    text_cols = [c for c in range(columns) if not numeric[c]] or list(range(columns))
    weight = sum(cap[c] for c in text_cols)
    return [int(cap[c] + (spare * cap[c] / weight if c in text_cols else 0)) for c in range(columns)]


def _table(document, rows: list[list[str]]) -> None:
    header, body = rows[0], rows[2:]
    width = len(header)
    table = document.add_table(rows=1 + len(body), cols=width)
    if TABLE_STYLE in [s.name for s in document.styles]:
        table.style = TABLE_STYLE
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    numeric = [bool(body) and all(_NUMBER.match(r[i].replace(" ", "")) or not r[i]
                                  for r in body if i < len(r)) for i in range(width)]
    widths = _column_widths(document, [header, *body], width, numeric)
    for col, text in enumerate(header):
        _inline(table.rows[0].cells[col].paragraphs[0], text, bold=True)
    _row_flag(table.rows[0], "w:tblHeader")
    for r, row in enumerate(body, start=1):
        for col in range(width):
            paragraph = table.rows[r].cells[col].paragraphs[0]
            _inline(paragraph, row[col] if col < len(row) else "")
            if numeric[col]:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for row in table.rows:
        _row_flag(row, "w:cantSplit")
        for col, cell in enumerate(row.cells):           # Word 看每格的寬度，LibreOffice 看列寬：兩個都設
            cell.width = widths[col]
            for run in cell.paragraphs[0].runs:
                run.font.size = Pt(TABLE_FONT_PT)
    for col, column in enumerate(table.columns):
        column.width = widths[col]
    document.add_paragraph()


def _page_footer(document, note: str) -> None:
    paragraph = document.sections[0].footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.add_run(f"{note}　第 ")
    run = paragraph.add_run()
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if kind:
            element = OxmlElement("w:fldChar")
            element.set(qn("w:fldCharType"), kind)
        else:
            element = OxmlElement("w:instrText")
            element.set(qn("xml:space"), "preserve")
            element.text = text
        run._r.append(element)
    paragraph.add_run(" 頁")
    for r in paragraph.runs:
        r.font.size = Pt(8)


def to_docx(markdown: str, *, title: str, template: Path | None = None) -> bytes:
    """把報告全文排成 .docx，回傳文件內容。"""
    use_template = template is not None and template.is_file()
    document = Document(str(template)) if use_template else Document()
    if not use_template:
        for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3", "Heading 4"):
            if name in [s.name for s in document.styles]:
                _set_cjk_font(document.styles[name], DEFAULT_CJK_FONT, DEFAULT_LATIN_FONT)
        document.styles["Normal"].font.size = Pt(10.5)
        _page_footer(document, "crudelab 生成；數字來自生成時的數據快照")
    document.core_properties.title = title
    document.core_properties.author = "crudelab"

    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            document.add_heading(_BOLD.sub(r"\1", heading.group(2)), level=0 if level == 1 else level - 1)
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            if len(rows) >= 2:
                _table(document, rows)
            continue
        if line.strip():
            paragraph = document.add_paragraph()
            _inline(paragraph, line.strip())
            if any(w in line for w in WARNING):                  # 數字核對未通過的提示：標紅
                for run in paragraph.runs:
                    run.font.color.rgb = RGBColor(0xB0, 0x20, 0x20)
        i += 1
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()
