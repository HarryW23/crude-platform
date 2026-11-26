"""導入模板：後台導入頁可以下載。模板從配置生成，序列改了模板跟著變，不會和配置對不上。

OPEC 月報的產量表（#14，PDF 讀不出時的後備）。路透（lseg_export.py）和 Kpler（終端下載的表，見 connectors/kpler.py）
的文件都能直接導入，不需要模板。
"""
from __future__ import annotations

import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .config import Config, normalize_alias

# OPEC 月報（MOMR）第 5 章「DoC crude oil production based on secondary sources, tb/d」表的行，按月報的順序。
# 名稱和月報原文一樣（導入時按序列的別名認），每一行都要能認出對應的序列（測試會檢查）。
MOMR_ROWS: tuple[tuple[str, str], ...] = (
    ("Algeria", "CRUDE_PROD_ALGERIA"), ("Congo", "CRUDE_PROD_CONGO"),
    ("Equatorial Guinea", "CRUDE_PROD_EQ_GUINEA"), ("Gabon", "CRUDE_PROD_GABON"), ("IR Iran", "CRUDE_PROD_IRAN"),
    ("Iraq", "CRUDE_PROD_IRAQ"), ("Kuwait", "CRUDE_PROD_KUWAIT"), ("Libya", "CRUDE_PROD_LIBYA"),
    ("Nigeria", "CRUDE_PROD_NIGERIA"), ("Saudi Arabia", "CRUDE_PROD_SAUDI"), ("UAE", "CRUDE_PROD_UAE"),
    ("Venezuela", "CRUDE_PROD_VENEZUELA"), ("Total OPEC", "CRUDE_PROD_OPEC"),
    ("Azerbaijan", "CRUDE_PROD_AZERBAIJAN"), ("Bahrain", "CRUDE_PROD_BAHRAIN"), ("Brunei", "CRUDE_PROD_BRUNEI"),
    ("Kazakhstan", "CRUDE_PROD_KAZAKHSTAN"), ("Malaysia", "CRUDE_PROD_MALAYSIA"), ("Mexico", "CRUDE_PROD_MEXICO"),
    ("Oman", "CRUDE_PROD_OMAN"), ("Russia", "CRUDE_PROD_RUSSIA"), ("Sudan", "CRUDE_PROD_SUDAN"),
    ("South Sudan", "CRUDE_PROD_SOUTH_SUDAN"), ("Total Non-OPEC DoC", "CRUDE_PROD_NON_OPEC_DOC"),
    ("Total DoC", "CRUDE_PROD_DOC"),
)

GUIDE = (
    "OPEC 月報產量導入模板（OPEC+ 分國原油產量，千桶/日）",
    "",
    "最簡單的做法：在後台導入頁直接上傳月報 PDF，平台自己讀表。這個模板是 PDF 讀不出時（例如月報改了格式）的後備。",
    "",
    "用模板的兩種做法：",
    "",
    "做法一：複製貼上",
    "  1. 打開月報 PDF，找到第 5 章的表「DoC crude oil production based on secondary sources, tb/d」。",
    "     注意是 secondary sources（二手來源），不是後面那張 direct communication（各國自報）。",
    "  2. 從表頭那一行（2024、2025、季度、月份）選到最後一行 Total DoC，複製。",
    "  3. 切到「貼上」工作表，點 A1，貼上。整行擠在一格裡也沒關係，導入時會自動拆開。",
    "",
    "做法二：手填",
    "  在「手填」工作表裡，把第一行的月份改成月報上最近的幾個月，再填數字。",
    "",
    "然後在後台「導入」頁上傳這個文件，數據源選「OPEC 月報（二手來源）」，預覽沒問題就確認。",
    "  - 年度、季度那幾列是平均數，會自動跳過，只導月度的數；最後的變化列（例如 Oct/Sep）也不導。",
    "  - 月報每期都會修訂前兩三個月：照貼即可，每一期的數字都會留著（按報告期保存），回測按當時發佈的那一期算。",
    "  - 導入當期月報時，「報告發佈日期」不用填；補錄以前的月報，填那一期的發佈日期。",
    "  - 表裡每個國家和合計都有對應的序列；預覽裡如果出現認不出的行，多半是月報加了新國家，告訴我加上。",
    "",
    "配額和可持續產能不在月報這張表裡，是參數：OPEC+ 每次調整配額，在後台參數頁按生效日期加一個版本。",
)


def opec_production(cfg: Config) -> bytes:
    """OPEC 月報產量模板（.xlsx）：說明、貼上（空白）、手填（國家在行，月份在列）。"""
    missing = [code for _, code in MOMR_ROWS if code not in cfg.series]
    if missing:
        raise ValueError(f"模板裡的序列不在配置裡：{'、'.join(missing)}")
    wb = Workbook()
    guide = wb.active
    guide.title = "說明"
    for i, line in enumerate(GUIDE, 1):
        guide.cell(i, 1, line or None)
    guide["A1"].font = Font(bold=True, size=13)
    guide.column_dimensions["A"].width = 110

    wb.create_sheet("貼上")

    fill = wb.create_sheet("手填")
    header = ("國家（月報原名）", "Aug 26", "Sep 26", "Oct 26")    # 示例月份：改成月報上的月份
    for j, text in enumerate(header, 1):
        cell = fill.cell(1, j, text)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="EEF2F6")
    fill.cell(1, len(header) + 2, "中文名（不導入）").font = Font(bold=True, color="888888")
    for i, (label, code) in enumerate(MOMR_ROWS, 2):
        fill.cell(i, 1, label).font = Font(bold=label.startswith("Total"))
        fill.cell(i, len(header) + 2, cfg.series[code].name_zh).font = Font(color="888888")
        for j in range(2, len(header) + 1):
            fill.cell(i, j).alignment = Alignment(horizontal="right")
    fill.column_dimensions["A"].width = 22
    fill.column_dimensions["F"].width = 30
    fill.freeze_panes = "B2"

    wb.properties.creator = "crudelab"
    wb.properties.created = wb.properties.modified = datetime(2026, 1, 1)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def unresolved_rows(cfg: Config) -> list[str]:
    """MOMR_ROWS 裡按別名認不到、或認到別的序列的行（給測試和 crudelab check 用）。"""
    table: dict[str, str] = {}
    for s in cfg.series.values():
        for alias in (s.code, s.name_zh, *s.aliases):
            table.setdefault(normalize_alias(alias), s.code)
    return [f"{label} → {table.get(normalize_alias(label), '認不出')}（應為 {code}）"
            for label, code in MOMR_ROWS if table.get(normalize_alias(label)) != code]
