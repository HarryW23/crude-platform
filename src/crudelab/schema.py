"""配置文件的結構定義。config/ 下每一種 YAML 條目對應這裡的一個類。

所有類都禁止未知字段：字段名打錯會在載入時直接報錯，而不是被悄悄忽略。
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Code = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)
EPOCH = date(1900, 1, 1)          # 「自始生效」的生效日期

Module = Literal["spread", "balance", "geo", "refinery", "positioning", "common"]
SeriesKind = Literal["futures", "swap", "spot", "diff", "osp", "freight", "fx", "stock", "flow", "ratio", "position"]
Frequency = Literal["daily", "weekly", "monthly", "quarterly"]      # 由細到粗
FREQUENCY_ORDER: tuple[str, ...] = ("daily", "weekly", "monthly", "quarterly")
DEFAULT_VALID_DAYS: dict[str, int] = {"daily": 0, "weekly": 7, "monthly": 35, "quarterly": 100}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------ 主數據 ------------------------------

class AtomDef(Strict):
    code: str
    name_zh: str


class UnitDef(Strict):
    code: str
    name_zh: str
    dimension: str                 # 給人看的分類：price / fx / volume …


class LocationDef(Strict):
    code: str = Code
    name_zh: str
    region: str


class CommodityDef(Strict):
    code: str = Code
    name_zh: str
    category: Literal["crude", "product", "chem", "freight", "fx", "fundamental"]


class SourceDef(Strict):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    kind: Literal["manual", "excel", "api"]
    status: Literal["active", "reserved"]
    priority: int = 50             # 越小越優先
    notes: str | None = None


class CftcRef(Strict):
    """CFTC 公開報告裡的一列：合約代碼（cftc_contract_market_code）和字段名。"""
    market: str = Field(pattern=r"^[0-9A-Z]{6}$")
    field: str = Field(pattern=r"^[a-z0-9_]+$")
    report: Literal["disaggregated_futures"] = "disaggregated_futures"


class SeriesDef(Strict):
    code: str = Code
    name_zh: str
    commodity: str
    location: str
    kind: SeriesKind
    unit: str
    frequency: Frequency = "daily"
    valid_days: int | None = Field(default=None, ge=0)   # 一個值最多沿用幾天；缺省按頻率定
    contract: str | None = None                          # 連續合約月序：M1 / M2 …
    benchmark: str | None = None
    module: Module = "common"
    aliases: list[str] = []
    eia_id: str | None = None
    cftc: CftcRef | None = None                          # 由 CFTC 公開報告自動取數
    notes: str | None = None

    @property
    def resolved_valid_days(self) -> int:
        return DEFAULT_VALID_DAYS[self.frequency] if self.valid_days is None else self.valid_days


class RollRule(Strict):
    """合約的最後交易日規則（只按周一到周五算工作日，交易所假期用 overrides 修正）。

    最後交易日 = 交割月往前 months_before 個月的那個月裡：
      day = "last_business"  → 該月最後一個工作日，再往前 business_days_before 個工作日
      day = 整數 d           → 該月第 d 日（不是工作日就取它之前最近的工作日），再往前 business_days_before 個工作日
    例：Brent 是交割月前第二個月的最後一個工作日（months_before 2）；
        WTI 是交割月前一個月 25 日之前 3 個工作日（months_before 1, day 25, business_days_before 3）。
    """
    months_before: int = Field(ge=0, le=3)
    day: Literal["last_business"] | int = "last_business"
    business_days_before: int = Field(default=0, ge=0, le=10)
    verified: bool = False                        # 規則是否已經和交易所 / 數據商的實際換月日核對過
    note: str | None = None
    overrides: dict[str, date] = {}               # 合約月份 "YYYY-MM" → 實際最後交易日（假期等例外）

    @model_validator(mode="after")
    def _check(self) -> RollRule:
        if isinstance(self.day, int) and not 1 <= self.day <= 28:
            raise ValueError("day 必須在 1–28 之間，或寫 last_business")
        for key in self.overrides:
            if len(key) != 7 or key[4] != "-" or not key[:4].isdigit() or not 1 <= int(key[5:] or 0) <= 12:
                raise ValueError(f"overrides 的鍵必須寫成 YYYY-MM：{key}")
        return self


class CurveDef(Strict):
    """期貨曲線模板，展開成 {code}_M{n} 多條連續合約序列。aliases 裡的 {n} 換成月序。

    roll 是合約的換月規則：有了它，系統才知道每天的 Mn 是哪個具體合約，換月日的跳動才能剔除。
    月序必須從 M1 開始連續（每個月都有合約），否則「換月後 M1 是之前的 M2」不成立。
    """
    code: str = Code
    name_zh: str
    commodity: str
    location: str
    kind: SeriesKind
    unit: str
    months: list[int] = Field(min_length=1)
    module: Module = "common"
    aliases: list[str] = []
    roll: RollRule | None = None

    @model_validator(mode="after")
    def _months(self) -> CurveDef:
        if sorted(self.months) != list(range(1, len(self.months) + 1)):
            raise ValueError(f"曲線 {self.code} 的月序必須是從 1 開始的連續整數")
        return self

    def expand(self) -> list[SeriesDef]:
        return [SeriesDef(code=f"{self.code}_M{n}", name_zh=f"{self.name_zh} M{n}", commodity=self.commodity,
                          location=self.location, kind=self.kind, unit=self.unit, contract=f"M{n}",
                          module=self.module, aliases=[a.replace("{n}", str(n)) for a in self.aliases])
                for n in self.months]


class SeriesFile(Strict):
    curves: list[CurveDef] = []
    series: list[SeriesDef] = []


# ------------------------------ 參數 ------------------------------

class VersionDef(Strict):
    valid_from: date
    value: float
    note: str | None = None


class ParameterDef(Strict):
    """參數 = 帶生效日期的常數。

    寫 value（自始生效）或 versions（多個生效日期）二選一；兩個都不寫表示「必填但尚未提供」，
    用到它的指標會顯示「缺參數」。verified=false 表示數值是暫定的，尚待核對。
    """
    key: str = Code
    name_zh: str
    unit: str = "1"
    group: str = "通用"
    description: str | None = None
    value: float | None = None
    versions: list[VersionDef] = []
    verified: bool = False
    model: str | None = None       # 由哪個煉廠模型生成（手寫的參數不填）

    @model_validator(mode="after")
    def _one_way(self):
        if self.value is not None and self.versions:
            raise ValueError("value 和 versions 只能寫一個")
        return self

    @property
    def seed_versions(self) -> list[VersionDef]:
        if self.value is not None:
            return [VersionDef(valid_from=EPOCH, value=self.value)]
        return sorted(self.versions, key=lambda v: v.valid_from)


# ------------------------------ 指標 ------------------------------

class IndicatorDef(Strict):
    code: str = Code
    name_zh: str
    module: Module
    group: str = "其他"            # 前台分組用
    unit: str
    formula: str
    ffill_limit: int = Field(default=0, ge=0)   # 輸入缺值時最多沿用幾天（跨市場假期）
    notes: str | None = None
    hidden: bool = False           # 中間量：不在列表裡顯示，但可以被引用和追溯
    model: str | None = None       # 由哪個煉廠模型生成


# ------------------------------ 煉廠模型 ------------------------------

class FeedDef(Strict):
    code: str = Code
    name_zh: str
    price: str                     # 公式片段，單位必須等於模型的 unit
    share: float = Field(gt=0, le=1)


class ProductDef(Strict):
    code: str = Code
    name_zh: str
    price: str                     # 公式片段，單位必須等於模型的 unit
    yield_: float = Field(alias="yield", gt=0, le=1)
    consumption_tax: str | None = None   # 從量消費稅的參數 key（單位同模型 unit）


class CostDef(Strict):
    code: str = Code
    name_zh: str
    value: float | None = None
    description: str | None = None


class TaxDef(Strict):
    """中國的增值稅 + 附加。產品價與原料成本都是含增值稅價。"""
    vat_rate: str                  # 參數 key
    surcharge_rate: str            # 參數 key：城建 + 教育附加，計稅基礎是 增值稅 + 消費稅


class RefineryModelDef(Strict):
    code: str = Code
    name_zh: str
    kind: Literal["benchmark", "fuel", "integrated"]
    location: str
    unit: str                      # 模型口徑：每桶原油（USD/bbl）或每噸原油（CNY/t）
    ffill_limit: int = Field(default=5, ge=0)
    notes: str | None = None
    feedstock: list[FeedDef] = Field(min_length=1)
    products: list[ProductDef] = Field(min_length=1)
    costs: list[CostDef] = []
    tax: TaxDef | None = None

    @model_validator(mode="after")
    def _check(self):
        if abs(sum(f.share for f in self.feedstock) - 1) > 1e-9:
            raise ValueError("原料配比 share 之和必須等於 1")
        if sum(p.yield_ for p in self.products) > 1 + 1e-9:
            raise ValueError("產品收率之和不能超過 1")
        for label, items in (("原料", self.feedstock), ("產品", self.products), ("成本", self.costs)):
            codes = [i.code for i in items]
            if len(codes) != len(set(codes)):
                raise ValueError(f"{label}代碼重複")
        if self.tax is None and any(p.consumption_tax for p in self.products):
            raise ValueError("產品寫了 consumption_tax 但模型沒有 tax 設定")
        return self


# ------------------------------ 平衡表 ------------------------------

class BalanceLineDef(Strict):
    code: str = Code
    name_zh: str
    value: str                     # 公式片段，單位必須等於平衡表的 unit


class BalanceDef(Strict):
    """供需平衡表：供應各項之和 − 需求各項之和 = 平衡（隱含庫存變化）。"""
    code: str = Code
    name_zh: str
    unit: str
    frequency: Frequency
    ffill_limit: int = Field(default=0, ge=0)
    notes: str | None = None
    supply: list[BalanceLineDef] = Field(min_length=1)
    demand: list[BalanceLineDef] = Field(min_length=1)
    actual_stock_change: str | None = None     # 公式片段：實際觀測到的庫存變化，用來算未解釋項

    @model_validator(mode="after")
    def _check(self):
        codes = [line.code for line in (*self.supply, *self.demand)]
        if len(codes) != len(set(codes)):
            raise ValueError("供應和需求各項的代碼不能重複")
        return self


# ------------------------------ 關係模型 ------------------------------

class RelationDef(Strict):
    """兩個量之間的線性關係：y = a + b·x。用來回答「按現在的 x，y 通常在什麼水平」。"""
    code: str = Code
    name_zh: str
    y: str                         # 序列或指標代碼
    x: str
    lookback_days: int = Field(default=1095, ge=30)     # 用最近多少天的樣本來擬合
    min_points: int = Field(default=30, ge=10)          # 樣本少於這個數就不擬合
    notes: str | None = None


# ------------------------------ 信號 ------------------------------

class SignalDef(Strict):
    """一個方向信號：看某個序列或指標的最新值相對自身歷史偏高還是偏低。

    z = (最新值 − 回看期均值) / 回看期標準差；score = clip(z / 2, −1, 1) × direction。
    direction = +1 表示數值越高對油價越利多，−1 表示越高越利空。
    """
    code: str = Code
    name_zh: str
    module: Literal["spread", "balance", "refinery", "positioning"]     # 地緣模組的信號來自事件庫，不在這裡定義
    input: str
    direction: Literal[1, -1]
    lookback_days: int = Field(default=730, ge=30)
    min_points: int = Field(default=30, ge=10)
    weight: float = Field(default=1.0, gt=0)
    notes: str | None = None


# ------------------------------ 地緣：事件預案 ------------------------------

EventCategory = Literal["conflict", "chokepoint", "sanction", "opec_policy", "outage", "policy", "demand", "other"]


class ImpactDef(Strict):
    target: str                    # 序列或指標代碼，通常是平衡表的單項
    delta: float                   # 示意值：套用時由使用者確認或修改
    duration_days: int | None = Field(default=None, ge=1)     # 空 = 一直持續
    note: str | None = None


class PlaybookDef(Strict):
    """事件預案：某一類事件通常影響哪些項目。套用到具體事件時生成一個情境，數值可以逐項改。"""
    code: str = Code
    name_zh: str
    category: EventCategory
    description: str
    impacts: list[ImpactDef] = Field(min_length=1)


# ------------------------------ 推演設定 ------------------------------

class HorizonDef(Strict):
    code: str = Code
    name_zh: str
    calendar_days: int = Field(ge=1)
    trading_days: int = Field(ge=1)


class SensitivityEstimate(Strict):
    """從歷史估計「庫存變化 → 價格變化」的設定。"""
    stocks: str                                   # 庫存序列（月度，例如 OECD 商業庫存），單位要和 平衡 × 天 一致
    lookback_days: int = Field(default=3650, ge=365)
    min_points: int = Field(default=36, ge=12)


class ProjectionSettings(Strict):
    """價格推演的全局設定。算法見 projection.py。"""
    anchor: str                    # 推演的價格：序列代碼
    balance_indicator: str         # 用哪個平衡指標衡量情境對供需的影響
    sensitivity_parameter: str     # 參數 key：累積庫存每變化一個單位，價格反向變化多少
    sensitivity_estimate: SensitivityEstimate | None = None    # 從歷史估計敏感度的設定（見 sensitivity.py）
    volatility_lookback: int = Field(default=250, ge=20)
    volatility_min_points: int = Field(default=60, ge=20)
    horizons: list[HorizonDef] = Field(min_length=1)
    module_weights: dict[Literal["spread", "balance", "refinery", "positioning", "geo"], str]   # 模組 → 權重參數 key


# ------------------------------ 定時任務 ------------------------------

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class JobDef(Strict):
    """一個定時任務：什麼時候、做什麼。

    時間二選一：
      at + days        每逢 days 的 at 時刻（時區見 schedule.yaml 的 timezone）。可再加 day_of_month 限定每月幾號
      every_minutes    每隔幾分鐘
    動作：
      pull             從拉取型數據源取數（source = 數據源代碼）。沒配密鑰時記為「跳過」，不算失敗
      import_folder    導入收件夾裡的 Excel / CSV（source = 記在哪個數據源名下；folder = data/ 下的文件夾）
      report           生成報告（kind = daily / weekly / monthly；narrative = 是否讓大模型加解讀）
      check_freshness  檢查數據有沒有斷更（規則在 freshness.yaml）。有斷更時記為失敗，從而觸發通知
      backup           備份數據庫、配置和原始文件，並按 backup.yaml 清理舊的定時備份
      calendar         同步金十財經日曆（含市場預期），把超預期換算成序列（規則在 jin10.yaml）
      news             同步金十快訊，只收錄命中原油關鍵詞的（關鍵詞在 jin10.yaml）
    """
    code: str = Code
    name_zh: str
    enabled: bool = True
    action: Literal["pull", "import_folder", "report", "check_freshness", "backup", "calendar", "news"]
    at: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] | Literal["daily", "weekdays"] = "daily"
    day_of_month: int | None = Field(default=None, ge=1, le=28)
    every_minutes: int | None = Field(default=None, ge=1, le=1440)
    catch_up_hours: int = Field(default=12, ge=0, le=168)   # 程序沒開而錯過的任務，多少小時內補跑一次
    source: str | None = None
    folder: str = "inbox"
    kind: Literal["daily", "weekly", "monthly"] | None = None
    narrative: bool = False

    @model_validator(mode="after")
    def _check(self):
        if (self.at is None) == (self.every_minutes is None):
            raise ValueError("at 和 every_minutes 必須寫一個，且只能寫一個")
        if self.every_minutes is not None and (self.days != "daily" or self.day_of_month is not None):
            raise ValueError("every_minutes 不能和 days / day_of_month 一起用")
        if self.action in ("pull", "import_folder") and not self.source:
            raise ValueError(f"{self.action} 任務需要 source")
        if self.action == "report" and not self.kind:
            raise ValueError("report 任務需要 kind")
        if ".." in self.folder or self.folder.startswith(("/", "\\")):
            raise ValueError("folder 必須是 data/ 下的相對路徑")
        return self

    @property
    def weekdays(self) -> set[int]:
        if self.days == "daily":
            return set(range(7))
        if self.days == "weekdays":
            return set(range(5))
        return {WEEKDAYS.index(d) for d in self.days}


class ScheduleDef(Strict):
    timezone: str = "Asia/Shanghai"
    jobs: list[JobDef] = []


# ------------------------------ 通知 ------------------------------

class ChannelDef(Strict):
    """一個通知渠道。地址和密碼不寫在這裡：群機器人的地址本身就是密鑰，一律用 crudelab secret set 保存。

    type 與對應的密鑰名：
      wecom 企業微信群機器人 → NOTIFY_WECOM_WEBHOOK      dingtalk 釘釘群機器人 → NOTIFY_DINGTALK_WEBHOOK
      feishu 飛書群機器人   → NOTIFY_FEISHU_WEBHOOK      slack → NOTIFY_SLACK_WEBHOOK
      webhook 自定義地址（收到一個 JSON）→ NOTIFY_WEBHOOK_URL
      email 郵件 → SMTP_PASSWORD（其餘郵件設定寫在這裡）
    """
    code: str = Code
    name_zh: str
    type: Literal["wecom", "dingtalk", "feishu", "slack", "webhook", "email"]
    enabled: bool = True
    smtp_host: str | None = None
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_security: Literal["ssl", "starttls"] = "ssl"
    smtp_username: str | None = None
    mail_from: str | None = None
    mail_to: list[str] = []

    @model_validator(mode="after")
    def _check(self):
        if self.type == "email" and not (self.smtp_host and self.mail_from and self.mail_to):
            raise ValueError("email 渠道需要 smtp_host、mail_from、mail_to")
        return self


class NotificationsDef(Strict):
    """什麼時候發通知。目前只針對定時任務：失敗、持續失敗的提醒、恢復。"""
    on_failure: bool = True
    on_recovery: bool = True
    repeat_hours: int = Field(default=6, ge=1, le=168)     # 持續失敗時，隔多久再提醒一次
    channels: list[ChannelDef] = []


# ------------------------------ 數據新鮮度 ------------------------------

class FreshnessRule(Strict):
    """一種頻率的序列多久沒更新算斷更。

    basis   observed：看最新一個數據點的日期（不晚於今天）離今天多遠
            received：看最近一次收到新值或修訂的時間離今天多遠。帶預測期的月度、季度數據用這個，
                      因為它們的「最新日期」本來就在未來，看日期永遠不會斷更
    count   weekdays：只數周一至周五（日度行情用，周末不算）；calendar：日曆天
    """
    max_days: int = Field(ge=1, le=400)
    basis: Literal["observed", "received"] = "observed"
    count: Literal["weekdays", "calendar"] = "calendar"


class FreshnessOverride(FreshnessRule):
    series: str


class FreshnessDef(Strict):
    defaults: dict[Literal["daily", "weekly", "monthly", "quarterly"], FreshnessRule] = {
        "daily": FreshnessRule(max_days=3, count="weekdays"),
        "weekly": FreshnessRule(max_days=14),
        "monthly": FreshnessRule(max_days=40, basis="received"),
        "quarterly": FreshnessRule(max_days=45, basis="received"),
    }
    watch: Literal["has_data", "all"] = "has_data"      # has_data：只檢查有過數據的序列
    required: list[str] = []                            # 必須有數據的序列：從沒有過數據也報
    ignore: list[str] = []                              # 不檢查的序列
    overrides: list[FreshnessOverride] = []


# ------------------------------ 回測 ------------------------------

class BacktestSettings(Strict):
    """回測的設定。算法見 backtest.py。"""
    anchor: str | None = None                  # 用哪個價格算之後的收益；不填就用 projection.yaml 的 anchor
    # 按數據日期回測時，每種頻率的數據在它的日期之後幾天才算「已知」（發佈延遲）
    publication_lag_days: dict[Literal["daily", "weekly", "monthly", "quarterly"], int] = {
        "daily": 0, "weekly": 5, "monthly": 40, "quarterly": 60}
    neutral_band: float = Field(default=0.15, gt=0, lt=1)    # 分數絕對值小於它算「中性」，不計入命中率
    min_effective_samples: int = Field(default=30, ge=5)      # 有效樣本少於它就標為樣本不足
    vintage_max_dates: int = Field(default=400, ge=10)        # 按入庫時間回測最多評估多少個日期（每個日期都要重算一遍）


# ------------------------------ 大模型的調用限制 ------------------------------

class LLMSettings(Strict):
    """用哪一家大模型（見 llm/registry.py）和調用限制（見 llm/limits.py）。密鑰不在這裡。

    所有計數都按「最近 60 秒 / 最近 24 小時」滾動計算，前台、後台、調度器幾個進程共用（記在數據庫裡）。
    """
    provider: Literal["none", "deepseek", "openai", "anthropic", "compatible"] = "none"
    model: str | None = None
    base_url: str | None = Field(default=None, pattern=r"^https://")     # 只允許 https：密鑰不走明文
    max_requests_per_minute: int = Field(default=10, ge=1, le=600)
    max_requests_per_day: int = Field(default=300, ge=1, le=100000)
    max_tokens_per_day: int = Field(default=1_500_000, ge=1000)
    max_concurrent: int = Field(default=2, ge=1, le=20)
    min_interval_seconds: float = Field(default=1.0, ge=0, le=60)
    timeout_seconds: float = Field(default=120, ge=10, le=600)


# ------------------------------ 金十數據 ------------------------------

class CalendarRule(Strict):
    """財經日曆的一項怎麼變成序列：標題包含 match（以及 require，如果給了）時，
    把「實際 − 公佈時凍結的預期」乘以 scale 寫進 series（日期 = 公佈的北京時間日期）。"""
    match: str
    require: str | None = None          # 例如單位「万桶」：標題裡沒有它就不換算，免得單位錯了還照算
    series: str
    scale: float = 1.0
    note: str | None = None


class Jin10Settings(Strict):
    """金十數據（MCP）的設定。token 不在這裡：用 crudelab secret set JIN10_TOKEN 保存。"""
    url: str = Field(default="https://mcp.jin10.com/mcp", pattern=r"^https://")
    protocol_version: str = "2025-11-25"
    max_calls_per_tool_per_day: int = Field(default=1000, ge=1, le=1500)   # 對方的上限是 1500，留出餘量
    calendar_rules: list[CalendarRule] = []
    flash_keywords: list[str] = []
    flash_pages: int = Field(default=3, ge=1, le=10)


# ------------------------------ 備份 ------------------------------

class BackupSettings(Strict):
    """備份的設定。做法見 backup.py。

    directory 相對路徑時在數據目錄（data/）下；絕對路徑可以指向另一塊硬盤、NAS 或雲同步文件夾。
    放在和數據同一塊磁盤上只防誤刪、防改壞，防不了硬盤損壞：正式使用時務必指到別處。
    環境變量 CRUDELAB_BACKUP_DIR 優先於這裡（不同機器的路徑不同時用）。
    保留：定時備份按「每天最後一份留 keep_daily 天、每周最後一份留 keep_weekly 周、
    每月最後一份留 keep_monthly 月」清理。
    手動備份和恢復前自動做的備份不會被自動刪除。
    """
    directory: str = "backups"
    keep_daily: int = Field(default=14, ge=1, le=366)
    keep_weekly: int = Field(default=8, ge=0, le=520)
    keep_monthly: int = Field(default=12, ge=0, le=240)
    include_raw: bool = True                 # 是否包含導入時保存的原始文件（data/raw）。它們是追溯的依據，建議保留
    max_age_hours: int = Field(default=36, ge=1, le=24 * 31)   # 最近一次成功備份超過這個時間，後台顯示警告


# ------------------------------ 公允價值 ------------------------------

class FairValueFactor(Strict):
    input: str                          # 序列或指標代碼
    name_zh: str | None = None          # 不寫就用序列或指標的名字


class FairValueModel(Strict):
    """基本面公允價值模型：在滾動窗口裡把價格回歸到幾個基本面因子上。算法見 fair_value.py。"""
    code: str = Code
    name_zh: str
    target: str                                         # 價格序列
    transform: Literal["log", "level"] = "log"          # log：回歸對數價格，偏離按百分比理解
    factors: list[FairValueFactor] = Field(min_length=1, max_length=6)
    window_days: int = Field(default=1095, ge=180)      # 只用最近這麼多天的數據擬合
    min_points: int = Field(default=120, ge=30)         # 樣本少於它不擬合
    min_r2: float = Field(default=0.3, ge=0, lt=1)      # 擬合優度低於它：只展示，不作為推演的漂移
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self):
        inputs = [f.input for f in self.factors]
        if len(inputs) != len(set(inputs)):
            raise ValueError("因子不能重複")
        if self.min_points < 10 * (len(self.factors) + 1):
            raise ValueError("min_points 至少要是 10 ×（因子數 + 1），否則擬合不穩定")
        return self
