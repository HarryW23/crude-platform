"""配置文件的結構定義。config/ 下每一種 YAML 條目對應這裡的一個類。

所有類都禁止未知字段：字段名打錯會在載入時直接報錯，而不是被悄悄忽略。
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Code = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)
EPOCH = date(1900, 1, 1)          # 「自始生效」的生效日期

Module = Literal["spread", "balance", "geo", "refinery", "common"]
SeriesKind = Literal["futures", "swap", "spot", "diff", "osp", "freight", "fx", "stock", "flow", "ratio"]
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
    notes: str | None = None

    @property
    def resolved_valid_days(self) -> int:
        return DEFAULT_VALID_DAYS[self.frequency] if self.valid_days is None else self.valid_days


class CurveDef(Strict):
    """期貨曲線模板，展開成 {code}_M{n} 多條連續合約序列。aliases 裡的 {n} 換成月序。"""
    code: str = Code
    name_zh: str
    commodity: str
    location: str
    kind: SeriesKind
    unit: str
    months: list[int] = Field(min_length=1)
    module: Module = "common"
    aliases: list[str] = []

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
    module: Literal["spread", "balance", "refinery"]     # 地緣模組的信號來自事件庫，不在這裡定義
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


class ProjectionSettings(Strict):
    """價格推演的全局設定。算法見 projection.py。"""
    anchor: str                    # 推演的價格：序列代碼
    balance_indicator: str         # 用哪個平衡指標衡量情境對供需的影響
    sensitivity_parameter: str     # 參數 key：平衡每變化一個單位，價格反向變化多少
    volatility_lookback: int = Field(default=250, ge=20)
    volatility_min_points: int = Field(default=60, ge=20)
    horizons: list[HorizonDef] = Field(min_length=1)
    module_weights: dict[Literal["spread", "balance", "refinery", "geo"], str]     # 模組 → 權重參數 key


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
    """
    code: str = Code
    name_zh: str
    enabled: bool = True
    action: Literal["pull", "import_folder", "report"]
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
