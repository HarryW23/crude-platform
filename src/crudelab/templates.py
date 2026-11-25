"""導入模板：後台導入頁可以下載。模板從配置生成，序列改了模板跟著變，不會和配置對不上。

OPEC 月報的產量表（#14，PDF 讀不出時的後備）和 Kpler 取數表（#25）。
路透能導出的數據不需要模板（lseg_export.py 直接寫出能導入的文件）。
"""
from __future__ import annotations

import io
from datetime import date, datetime

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


KPLER_GUIDE = (
    "Kpler 取數模板（原油出口、進口、浮倉）",
    "",
    "在裝了 Kpler Excel 插件（V2）的電腦上做。每頁第一行是表頭，不要改；第一列是日期。",
    "",
    "「月度流量」頁：每一列是一個國家的原油出口或進口。",
    "  在 Kpler 插件裡選 Flows → 商品 Crude → 方向 Export（或 Import）→ 國家 → 粒度 Monthly → 單位 kbd（千桶/日），",
    "  把得到的月份和數字放到對應的列下面（月份放第一列，一個月一行）。",
    "  當月的數會隨貨物卸完不斷修訂：每次導入都會記一期，所以盡量每天（至少每周）導入一次，連同最近幾個月一起更新。",
    "",
    "「周度浮倉」頁：全球原油浮倉、在途（oil on water），單位千桶。",
    "  Inventories → 商品 Crude → 勾選 add Oil-On-Water，按 onshore/offshore 拆分 → 粒度 Weekly；",
    "  浮倉的天數門檻用 Kpler 預設（停留 7 天以上），以後不要改，否則前後不可比。",
    "",
    "存檔前一定要：全選 → 複製 → 選擇性貼上「值」。還帶著 Kpler 公式的文件導入時會被拒收",
    "（公式離開裝了插件的電腦就算不出來，只會悄悄少一部分數據）。",
    "",
    "然後在後台「數據導入」頁上傳，數據源選「Kpler（Excel 插件）」，預覽沒問題就確認。",
    "  - 表頭用模板裡的名字，不要換成 Kpler 原表的國家名：「Saudi Arabia」已經是 OPEC 月報產量的名字，會對錯序列。",
    "  - 單位不對（例如插件給的是桶而不是千桶）時，第一次導入後我會在數量級上發現；先照模板的單位取。",
    "  - 要加國家或者其他數據，告訴我，我先在配置裡加序列，模板會自動多出一列。",
)


def kpler(cfg: Config) -> bytes:
    """Kpler 取數模板（.xlsx）：說明、月度流量、周度浮倉。列從配置裡 provider 為 kpler 的序列生成。"""
    series = [s for s in cfg.series.values() if s.provider == "kpler"]
    if not series:
        raise ValueError("配置裡沒有 Kpler 的序列")
    wb = Workbook()
    guide = wb.active
    guide.title = "說明"
    for i, line in enumerate(KPLER_GUIDE, 1):
        guide.cell(i, 1, line or None)
    guide["A1"].font = Font(bold=True, size=13)
    guide.column_dimensions["A"].width = 110
    for title, frequency, first in (("月度流量", "monthly", "月份"), ("周度浮倉", "weekly", "日期")):
        sheet = wb.create_sheet(title)
        columns = [s for s in series if s.frequency == frequency]
        for j, text in enumerate([first, *[s.aliases[0] for s in columns]], 1):
            cell = sheet.cell(1, j, text)
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="EEF2F6")
            sheet.column_dimensions[cell.column_letter].width = 14 if j == 1 else 24
        for j, s in enumerate(columns, 2):                      # 第二行：中文名和單位（也是序列的名字，認得出）
            sheet.cell(2, j, s.name_zh).font = Font(color="888888")
        sheet.cell(2, 1, "（中文名）").font = Font(color="888888")
        sheet.cell(3, 1, date(2026, 10, 1) if frequency == "monthly" else date(2026, 11, 20)).number_format = \
            "yyyy-mm" if frequency == "monthly" else "yyyy-mm-dd"
        sheet.freeze_panes = "B3"
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
