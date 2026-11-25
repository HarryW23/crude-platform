"""數據需求清單：系統需要哪些外部數據和接口、各自供哪些功能用、憑證怎麼保管。

清單是從 config/ 生成的（`crudelab data-needs`），所以不會和實際配置脫節：加了序列、指標，重新生成即可。
「從哪裡取得」是按序列的性質歸類的規劃建議，不影響實際取數——任何序列都可以用 Excel 導入或手動錄入。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import credentials
from . import formula as fx
from .config import Config
from .schema import SeriesDef

CN_FUTURES = ("SC_M", "FU_M", "LU_M", "BU_M", "PP_CN", "LLDPE_CN", "PTA_CN", "MEG_CN")
MODULES = {"spread": "地域價差", "balance": "供需", "refinery": "煉廠", "positioning": "資金持倉", "geo": "地緣",
           "common": "公用"}
FREQUENCIES = {"daily": "日", "weekly": "周", "monthly": "月", "quarterly": "季"}


@dataclass(frozen=True)
class Provider:
    code: str
    name: str
    how: str            # 現在怎麼拿到
    credential: str     # 需要什麼憑證
    status: str
    note: str = ""


PROVIDERS: dict[str, Provider] = {p.code: p for p in (
    Provider("eia", "EIA（美國能源信息署）公開數據",
             "接口自動取數：後台「數據源」頁點「立即取數」，或 `crudelab pull eia`",
             "`EIA_API_KEY`，在 eia.gov/opendata 免費申請",
             "接口已寫好，**沒有用真實密鑰調用過**",
             "序列號是按記憶登記的，第一次取數後請核對每條是否取到、數量級是否合理。"),
    Provider("exchange_intl", "國際交易所期貨結算價（ICE、NYMEX、DME / IFAD）",
             "路透終端導出 Excel 後在後台導入", "路透 / LSEG 訂閱；之後可接 LSEG API（`LSEG_*`）",
             "Excel 導入可用；API 預留"),
    Provider("exchange_cn", "國內交易所期貨結算價（INE、上期所、大商所、鄭商所）",
             "路透或 Wind 導出 Excel 後導入", "路透 / Wind 等任一行情訂閱", "Excel 導入可用"),
    Provider("pra", "報價機構的現貨評估價（Platts、Argus 等）",
             "路透終端導出 Excel 後導入",
             "路透訂閱 **加** 對應報價機構的授權",
             "Excel 導入可用",
             "即期布倫特、迪拜現貨與掉期、各油種現貨升貼水、新加坡和西北歐成品油、化工品外盤價，多數是報價機構的評估價。"
             "它們在路透終端裡通常需要另外訂閱才看得到，**請確認你們的訂閱範圍**，以及授權是否允許把數據存進自建系統。"),
    Provider("osp", "產油國官方售價（沙特、伊拉克等）",
             "每月公佈一次，手動錄入或隨 Excel 導入", "公開信息，路透有轉載", "手動 / Excel"),
    Provider("freight", "油輪運價（波羅的海交易所 TD 航線）",
             "路透終端導出 Excel 後導入", "路透訂閱（含波羅的海交易所數據）", "Excel 導入可用",
             "WS 點折成 美元/桶 還需要 Worldscale 每年的基準費率，見下面「需要外部數據的參數」。"),
    Provider("fx", "匯率", "路透導出 Excel 後導入", "任一行情訂閱", "Excel 導入可用"),
    Provider("lseg_macro", "美元指數、美債收益率", "`scripts/lseg_export.py` 從路透導出後導入", "路透 / LSEG 訂閱",
             "導出腳本可用", "國債 RIC 同時有價格和收益率字段：配置裡的 lseg.field 要指定收益率那個，否則會取到價格。"),

    Provider("cn_vendor", "國內現貨價（隆眾、卓創、金聯創等）",
             "資訊商網站或終端導出 Excel 後導入", "國內資訊商訂閱；之後可接其 API（`CN_VENDOR_API_KEY`）",
             "Excel 導入可用；API 預留",
             "山東地煉汽柴油、液化氣、石油焦，華東汽柴油和純苯，航煤出廠價。兩個中國煉廠模型依賴這一組。"),
    Provider("agency", "機構月報（IEA、OPEC）", "從月報的表格整理成 Excel 後導入",
             "IEA 月報需訂閱；OPEC 月報公開", "Excel 導入可用",
             "IEA / OPEC 的供需只用於和 EIA 口徑對照，不影響平衡表本身。OPEC+ 分國產量（CRUDE_PROD_*，千桶/日）取 OPEC 月報的"
             "二手來源表：後台導入頁下載模板，把月報 PDF 裡的表複製貼上後導入（數據源選「OPEC 月報（二手來源）」）；"
             "配額和可持續產能是參數（config/parameters/opec.yaml），需要你按 OPEC+ 的決定錄入。"),
    Provider("consult", "煉廠檢修（諮詢機構）", "從諮詢機構的報告整理成 Excel 後導入",
             "IIR、Platts、Energy Aspects 等任一家的訂閱", "Excel 導入可用",
             "全球煉廠停工量（檢修 + 非計劃）。各家口徑不同，固定用一家。"),
    Provider("cftc", "CFTC / ICE 持倉報告", "定時任務自動取（CFTC 公開接口；ICE 由路透導出）", "CFTC 不需要；ICE 見路透",
             "CFTC 接口已接通"),
    Provider("jin10", "金十數據", "定時任務自動取（財經日曆、報價）", "`JIN10_TOKEN`（`crudelab secret set JIN10_TOKEN`）",
             "已接通", "報價只保存最近 1 個交易日，歷史補不了，從接上那天起積累。"),
)}
RESERVED_APIS = (
    ("Kpler", "`KPLER_API_TOKEN`", "船期、到港量、浮倉、分國別進出口流量",
     "目前沒有任何序列依賴它。接通後可以把全球平衡表從「機構口徑」細化到分國別自下而上。"),
)


def provider_of(s: SeriesDef) -> str:
    if s.provider:
        return s.provider
    if s.eia_id:
        return "eia"
    if s.jin10 or s.code.startswith("CAL_"):
        return "jin10"
    if s.cftc or s.kind == "position":
        return "cftc"
    if s.code.startswith(("IEA_", "OPEC_")):
        return "agency"
    if s.kind == "fx":
        return "fx"
    if s.kind == "freight":
        return "freight"
    if s.kind == "osp":
        return "osp"
    if s.kind == "futures":
        return "exchange_cn" if s.code.startswith(CN_FUTURES) else "exchange_intl"
    if s.unit.startswith("CNY"):
        return "cn_vendor"
    return "pra"


def _leaves(cfg: Config, code: str, seen: dict[str, set[str]]) -> set[str]:
    if code not in seen:
        out: set[str] = set()
        for name in fx.names(cfg.indicators[code].formula):
            if name in cfg.indicators:
                out |= _leaves(cfg, name, seen)
            elif name in cfg.series:
                out.add(name)
            elif name in cfg.views:                     # 計價視角：用到曲線上的幾個月份
                from .contracts import view_months_needed
                view = cfg.views[name]
                out |= {f"{view.curve}_M{n}" for n in view_months_needed(cfg, view)}
        seen[code] = out
    return seen[code]


def dependents(cfg: Config) -> dict[str, dict[str, set[str]]]:
    """每條序列被哪些看得到的指標用到，按模組分。"""
    seen: dict[str, set[str]] = {}
    out: dict[str, dict[str, set[str]]] = {code: {} for code in cfg.series}
    for code, ind in cfg.indicators.items():
        if ind.hidden:
            continue
        for leaf in _leaves(cfg, code, seen):
            out[leaf].setdefault(ind.module, set()).add(code)
    return out


def render(cfg: Config) -> str:
    used = dependents(cfg)
    groups: dict[str, list[SeriesDef]] = {code: [] for code in PROVIDERS}
    for s in cfg.series.values():
        groups[provider_of(s)].append(s)
    lines = ["# 數據與接口需求清單", "",
             "> 這份文檔由 `crudelab data-needs --output docs/data-requirements.md` 從 `config/` 生成。"
             "不要手改；改了配置後重新生成。", "",
             f"系統目前定義了 **{len(cfg.series)} 條序列**，供 {sum(not i.hidden for i in cfg.indicators.values())} 個指標、"
             f"{len(cfg.refineries)} 個煉廠模型、{len(cfg.balances)} 張平衡表使用。"
             "下面按「從哪裡取得」分組。任何序列都可以用 Excel 導入或在後台手動錄入，接口只是省去人工。", "",
             "## 一覽", "",
             "| 來源 | 序列數 | 現在怎麼取得 | 需要的憑證 | 狀態 |", "|---|---|---|---|---|"]
    for code, p in PROVIDERS.items():
        lines.append(f"| {p.name} | {len(groups[code])} | {p.how} | {p.credential} | {p.status} |")
    lines += ["", "序列有沒有數據、最新到哪一天，在後台的「序列」頁看。", ""]

    lines += ["## 各來源的序列", ""]
    for code, p in PROVIDERS.items():
        if not groups[code]:
            continue
        lines += [f"### {p.name}", ""]
        if p.note:
            lines += [p.note, ""]
        lines += ["| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |", "|---|---|---|---|---|"]
        for s in groups[code]:
            by_module = used[s.code]
            usage = "、".join(f"{MODULES[m]} {len(c)}" for m, c in by_module.items()) or "（目前沒有指標用到）"
            extra = f"　`{s.eia_id}`" if s.eia_id else ""
            freq = FREQUENCIES[s.frequency] + ("（只對當期生效：日期記適用期間的 1 日，例如官價記裝船月）"
                                               if s.within_period else "") + \
                ("（按報告期保存：補錄歷史報告時填發佈日期）" if s.vintaged else "")
            lines.append(f"| `{s.code}`{extra} | {s.name_zh} | {s.unit} | {freq} | {usage} |")
        lines.append("")

    missing = [p for p in cfg.parameters.values() if not p.seed_versions]
    lines += ["## 需要外部數據的參數", "",
              "這些參數沒有預設值，要你提供；沒填時用到它們的指標顯示「缺參數」。在後台「參數」頁錄入。", "",
              "| 參數 | 名稱 | 單位 | 說明 |", "|---|---|---|---|"]
    lines += [f"| `{p.key}` | {p.name_zh} | {p.unit} | {p.description or ''} |" for p in missing]
    lines += ["", "其中運費基準費率（`FLAT_*`）來自 Worldscale 協會的年度費率表，需要訂閱，每年 1 月 1 日更新一次，"
              "錄入時按年填生效日期。", ""]

    lines += ["## 預留、尚未使用的數據接口", "", "| 接口 | 憑證 | 能提供什麼 | 現狀 |", "|---|---|---|---|"]
    lines += [f"| {name} | {cred} | {what} | {state} |" for name, cred, what, state in RESERVED_APIS]
    lines += ["| 路透 / LSEG Workspace | App Key（在 Workspace 裡生成，只在那台電腦上用） | 以 RIC 直接取行情，取代人工導出 Excel | "
              "`scripts/lseg_export.py` 在裝了 Workspace 的電腦上導出 CSV，再導入；RIC 清單見 `scripts/lseg_rics.txt` |",
              "| 國內資訊商 API | `CN_VENDOR_API_KEY` | 國內現貨價、開工率、庫存 | 預留 |", ""]

    lines += ["## 大模型接口", "",
              "在後台「大模型」頁選擇用哪一家、哪個型號、開不開思考模式；不選時相關功能停用，其餘不受影響。"
              "每次調用都受 `config/llm.yaml` 的頻率和用量上限約束，出錯會自動暫停，不自動重試。", "",
              "型號不內置預設值，從供應商返回的模型列表裡選（供應商會改名、停用舊名字）。", "",
              "| 供應商 | 憑證 | 思考模式 | 狀態 |", "|---|---|---|---|",
              "| DeepSeek | `DEEPSEEK_API_KEY` | 支持（請求參數 `thinking`） | 請求格式用本機模擬服務驗過；沒有用真實密鑰調用過 |",
              "| OpenAI | `OPENAI_API_KEY` | 不支持 | 與 DeepSeek 共用同一段接入代碼；沒有用真實密鑰調用過 |",
              "| Claude | `ANTHROPIC_API_KEY` | 不支持 | 沒有調用過 |", "",
              "**用大模型時，數據會離開你的系統。** 發給供應商的內容：", "",
              "| 功能 | 發出去的內容 |", "|---|---|",
              "| 從文字抽取事件 | 你貼進去的那段文字，加上地區字典 |",
              "| 報告解讀 | 整份報告正文：各指標的最新值、平衡表、毛利、事件、推演結果 |",
              "| 問答 | 你的問題、對話歷史，以及它查到的數據（指標值、序列的最近幾個點、平衡表、毛利拆解等） |", "",
              "報告和問答會把由行情算出來的數字發給第三方。行情數據（尤其是報價機構的評估價）的授權通常限制再分發，"
              "**啟用前請確認你們的數據授權是否允許**。只想避開這個問題的話，可以只用「抽取事件」，或不配置大模型。", ""]

    lines += ["## 憑證清單與保管", "",
              "系統認得的全部憑證。除了它們，沒有別的地方需要密鑰。", "",
              "| 名稱 | 用途 |", "|---|---|"]
    lines += [f"| `{name}` | {description} |" for name, description in credentials.KNOWN.items()]
    lines += ["", "通知類的密鑰（`NOTIFY_*`、`SMTP_PASSWORD`）可以加大寫後綴存好幾個群，例如 `NOTIFY_WECOM_WEBHOOK_TRADING`，"
              "保管規則完全相同（脫敏、文件權限都一樣適用）。",
              "",
              "保管規則：", "",
              "- **放在哪裡**：密鑰文件 `~/.crudelab/secrets.env`（在項目文件夾之外，權限僅本人可讀寫），或環境變量。"
              "用 `crudelab secret set 名稱` 寫入，輸入不回顯，也不會留在終端的歷史記錄裡。",
              "- **不放在哪裡**：git 倉庫、`config/` 下的任何文件、數據庫、`data/` 文件夾、頁面、聊天記錄、郵件。",
              "- **查看狀態**：`crudelab secret list` 只顯示每個憑證設了沒有、來自哪裡，任何時候都不顯示內容。",
              "- **系統怎麼防洩漏**：密鑰不會出現在接口回應、頁面、日誌、錯誤訊息、操作日誌裡；"
              "外部服務出錯時只報狀態碼，不轉述對方返回的原文；所有日誌和錯誤回應另有一層自動遮蔽。",
              "- **倉庫怎麼防誤提交**：`.gitignore` 排除常見的密鑰文件；`scripts/check_secrets.py` 在每次推送時由 CI 掃描。",
              "- **一旦懷疑洩漏**：立刻到供應商後台把那個密鑰作廢並重發，再用 `crudelab secret set` 換上新的。"
              "不要只是把它從文件裡刪掉——已經洩漏的密鑰刪了也還能用。",
              "- **申請密鑰時**：能限制權限、額度、來源 IP 的都限制上。大模型的密鑰建議設月度消費上限。", ""]
    return "\n".join(lines)


# ------------------------------ 給 LSEG 導出腳本的 RIC 清單 ------------------------------

LSEG_PROVIDERS = ("exchange_intl", "exchange_cn", "pra", "freight", "fx", "lseg_macro")      # 路透終端上可能取得到的
_RIC = re.compile(r"^[0-9A-Z]{1,8}c\d{1,2}$|^[0-9A-Za-z.]+=[A-Z]*$|^[A-Z0-9]+-$")


def ric_of(s: SeriesDef) -> str | None:
    """序列的路透代碼（RIC）：優先用 lseg.ric；舊配置裡放在別名中、看起來像 RIC 的也認。"""
    if s.lseg:
        return s.lseg.ric
    return next((a for a in s.aliases if _RIC.match(a)), None)


def lseg_rics_text(cfg: Config) -> str:
    """scripts/lseg_rics.txt 的內容：有 RIC 的序列一行一條；可能在路透上、但還不知道 RIC 的列在後面（註釋掉）。"""
    lines = ["# crudelab 從 LSEG Workspace 導出的序列清單（由 crudelab lseg-rics 從配置生成，不要手改這個文件）",
             "# 每行：序列代碼  RIC  [字段]。字段不寫時依次試 SETTLE、TRDPRC_1、CLOSE、MID_PRICE、BID。",
             "# 要加 RIC：在 config/master/series/ 裡給那條序列的 aliases 加上 RIC，再運行 crudelab lseg-rics。", ""]
    known: list[tuple[SeriesDef, str | None]] = []
    unknown: list[tuple[SeriesDef, str | None]] = []
    for s in cfg.series.values():
        if provider_of(s) not in LSEG_PROVIDERS:
            continue
        ric = ric_of(s)
        (known if ric else unknown).append((s, ric))
    for s, ric in known:
        field = s.lseg.field if s.lseg and s.lseg.field else ""
        note = f"   # 替代：{s.lseg.proxy}" if s.lseg and s.lseg.proxy else ""
        lines.append(f"{s.code:<24} {ric:<16} {field}".rstrip() + note)
    if unknown:
        lines += ["", f"# 下面 {len(unknown)} 條可能在路透上，但還不知道 RIC（現貨評估價通常要另外訂閱 Platts / Argus）："]
        lines += [f"# {s.code:<22} ?    {s.name_zh}" for s, _ in unknown]
    return "\n".join(lines) + "\n"
