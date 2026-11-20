"""報告導出 Word（#40）：內容和 Markdown 下載一致，表格、標題、粗體、解讀、模板都對。"""
import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt

from crudelab import reports
from crudelab.report_docx import DEFAULT_CJK_FONT, to_docx


def read(data: bytes) -> Document:
    return Document(io.BytesIO(data))


def all_text(doc: Document) -> str:
    parts = [p.text for p in doc.paragraphs]
    parts += [c.text for t in doc.tables for row in t.rows for c in row.cells]
    return "\n".join(parts)


def test_real_report_round_trips(loaded, cfg):
    report = reports.generate(loaded, cfg, "daily")
    text = reports.full_markdown(report)
    doc = read(to_docx(text, title=report.title))
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith(("Heading", "Title"))]
    expected = [re.sub(r"\*\*(.+?)\*\*", r"\1", m) for m in re.findall(r"^#{1,4} (.*)$", text, re.M)]
    assert headings == expected and headings[0].startswith("原油市場")
    assert len(doc.tables) == text.count("\n|---") + text.startswith("|---")         # 每個 Markdown 表格都在
    body = all_text(doc)
    for cell in re.findall(r"^\| (.+?) \|", text, re.M):                              # 每行第一格都在 Word 裡
        assert cell.replace("**", "") in body
    assert "**" not in body                                                          # 粗體標記沒有漏成文字
    first = doc.tables[0]
    assert all(r.bold for r in first.rows[0].cells[0].paragraphs[0].runs)            # 表頭加粗
    assert first.rows[0]._tr.trPr.find(qn("w:tblHeader")) is not None                # 跨頁重複表頭
    assert all(row._tr.trPr.find(qn("w:cantSplit")) is not None for row in first.rows)  # 行不被分頁切開
    widths = [c.width for c in doc.tables[2].rows[0].cells]                          # 指標名那列比數字列寬
    assert widths[0] > widths[2]
    bold = [r.text for p in doc.paragraphs for r in p.runs if r.bold]
    assert any(b and b in text for b in bold)                                        # **綜合判斷** 變成粗體
    assert doc.core_properties.title == report.title
    fonts = doc.styles["Normal"].element.rPr.find(qn("w:rFonts"))
    assert fonts.get(qn("w:eastAsia")) == DEFAULT_CJK_FONT


def test_numbers_right_aligned_and_failed_check_is_red():
    md = ("# 標題\n\n| 指標 | 最新值 | 變化 |\n|---|---|---|\n| Brent | 1,234.50 | +2.10 |\n| WTI | — | −0.5 |\n\n"
          "## 解讀\n\n由大模型撰寫。其中這些數字在正文裡找不到，請勿採信：88.5。\n\n第一段。\n\n第二段 **重點**。\n")
    doc = read(to_docx(md, title="t"))
    table = doc.tables[0]
    assert table.rows[1].cells[0].paragraphs[0].alignment != WD_ALIGN_PARAGRAPH.RIGHT      # 名稱列不右對齊
    assert all(table.rows[r].cells[c].paragraphs[0].alignment == WD_ALIGN_PARAGRAPH.RIGHT
               for r in (1, 2) for c in (1, 2))
    warning = next(p for p in doc.paragraphs if "請勿採信" in p.text)
    assert all(r.font.color.rgb is not None for r in warning.runs)
    assert [p.text for p in doc.paragraphs if p.text in ("第一段。", "第二段 重點。")] == ["第一段。", "第二段 重點。"]
    footer = doc.sections[0].footer.paragraphs[0]._p.xml
    assert "PAGE" in footer and "crudelab" in footer


def test_template_styles_are_used(tmp_path):
    template = Document()
    template.styles["Normal"].font.size = Pt(14)
    template.sections[0].header.paragraphs[0].text = "某公司研究部"
    path = tmp_path / "template.docx"
    template.save(path)
    doc = read(to_docx("# 標題\n\n正文\n", title="t", template=path))
    assert doc.styles["Normal"].font.size == Pt(14)
    assert doc.sections[0].header.paragraphs[0].text == "某公司研究部"                   # 公司抬頭來自模板
    assert "crudelab 生成" not in doc.sections[0].footer.paragraphs[0].text             # 用模板時不加內置頁腳
    assert read(to_docx("正文\n", title="t", template=tmp_path / "missing.docx")).paragraphs  # 模板不存在：用內置


def test_download_api(loaded, cfg, client):
    made = client.post("/api/reports", json={"kind": "daily"}).json()
    r = client.get(f"/api/reports/{made['id']}/docx")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert f'filename="report-{made["id"]}.docx"' in r.headers["content-disposition"]
    doc = read(r.content)
    md = client.get(f"/api/reports/{made['id']}/markdown").text
    assert doc.paragraphs[0].text == md.splitlines()[0].lstrip("# ")                  # 和 Markdown 同一份全文
    assert client.get("/api/reports/999/docx").status_code == 404
