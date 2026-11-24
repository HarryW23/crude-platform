"""配置載入與校驗：把 config/ 下的 YAML 變成一個校驗過的 Config 對象。

config/ 的結構：
    master/units.yaml          原子單位與單位字典
    master/locations.yaml      地區
    master/commodities.yaml    品種
    master/sources.yaml        數據源
    master/series/*.yaml       序列（可按領域拆成多個文件）
    parameters/*.yaml          參數
    indicators/*.yaml          指標
    refineries/*.yaml          煉廠模型（一個文件一個模型）
    balances/*.yaml            供需平衡表（一個文件一張表）
    relations/*.yaml           關係模型（線性擬合）
    signals/*.yaml             方向信號
    geo/playbooks.yaml         地緣事件預案
    geo/analogs.yaml           歷史事件類比（事實和出處；市場反應現算）
    projection.yaml            價格推演設定
    schedule.yaml              定時任務
    notifications.yaml         通知渠道與規則
    freshness.yaml             數據斷更的判斷規則
    backtest.yaml              回測設定
    fair_value.yaml            基本面公允價值模型
    backup.yaml                備份位置與保留策略
    llm.yaml                   大模型的調用限制
    jin10.yaml                 金十數據：額度、財經日曆的換算規則、快訊關鍵詞
    calendars.yaml             交易所節假日日曆

校驗一次做完並一次報全：結構（字段名、類型）、引用（單位、地區、品種、公式裡的名字）、
唯一性（代碼、別名）、公式語法、循環引用、量綱。任何一項不過，整份配置都不生效。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import ValidationError

from . import formula as fx
from . import units
from .balance import BalanceSpec, compile_balance
from .calendars import WEEKDAYS, Calendar
from .calendars import build as build_calendar
from .refinery import RefinerySpec, compile_model
from .schema import (
    FREQUENCY_ORDER,
    AnalogDef,
    AtomDef,
    BacktestSettings,
    BackupSettings,
    BalanceDef,
    CalendarDef,
    CalendarsFile,
    CommodityDef,
    CurveDef,
    FairValueModel,
    Frequency,
    FreshnessDef,
    IndicatorDef,
    Jin10Settings,
    LLMSettings,
    LocationDef,
    NotificationsDef,
    ParameterDef,
    PlaybookDef,
    PricingView,
    ProjectionSettings,
    RefineryModelDef,
    RelationDef,
    ScheduleDef,
    SeriesDef,
    SeriesFile,
    SignalDef,
    SourceDef,
    Strict,
    UnitDef,
)
from .units import Dim

_NORM_RE = re.compile(r"[\s\-_/().,#:：（）|｜]+")
T = TypeVar("T", bound=Strict)


class ConfigError(ValueError):
    """配置有錯。訊息會列出全部問題，並指出文件和條目。"""


def normalize_alias(text: str) -> str:
    """別名比對用的歸一化：忽略大小寫、空白和常見標點，繁體轉成簡體。
    國內資訊商導出的表頭是簡體（山东汽油92），配置裡寫的是繁體（山東汽油92）：兩種寫法都能認出來。"""
    from .zh import to_simplified
    return _NORM_RE.sub("", to_simplified(str(text))).lower()


@dataclass
class Config:
    atoms: set[str] = field(default_factory=set)
    units: dict[str, UnitDef] = field(default_factory=dict)
    locations: dict[str, LocationDef] = field(default_factory=dict)
    commodities: dict[str, CommodityDef] = field(default_factory=dict)
    sources: dict[str, SourceDef] = field(default_factory=dict)
    series: dict[str, SeriesDef] = field(default_factory=dict)
    parameters: dict[str, ParameterDef] = field(default_factory=dict)
    indicators: dict[str, IndicatorDef] = field(default_factory=dict)
    refineries: dict[str, RefinerySpec] = field(default_factory=dict)
    balances: dict[str, BalanceSpec] = field(default_factory=dict)
    relations: dict[str, RelationDef] = field(default_factory=dict)
    signals: dict[str, SignalDef] = field(default_factory=dict)
    playbooks: dict[str, PlaybookDef] = field(default_factory=dict)
    analogs: dict[str, AnalogDef] = field(default_factory=dict)         # 歷史事件類比（geo/analogs.yaml）
    projection: ProjectionSettings | None = None
    schedule: ScheduleDef = field(default_factory=ScheduleDef)
    notifications: NotificationsDef = field(default_factory=NotificationsDef)
    freshness: FreshnessDef = field(default_factory=FreshnessDef)
    backtest: BacktestSettings = field(default_factory=BacktestSettings)
    backup: BackupSettings = field(default_factory=BackupSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    jin10: Jin10Settings = field(default_factory=Jin10Settings)
    fair_values: dict[str, FairValueModel] = field(default_factory=dict)
    curves: dict[str, CurveDef] = field(default_factory=dict)          # 期貨曲線：代碼 → 模板
    views: dict[str, PricingView] = field(default_factory=dict)        # 計價視角：代碼 → 定義（見 schema.PricingView）
    calendar_defs: dict[str, CalendarDef] = field(default_factory=dict)
    calendars: dict[str, Calendar] = field(default_factory=dict)       # 交易日曆：代碼 → 推出來的假期（calendars.py）

    def calendar_of(self, code: str) -> Calendar:
        """序列的交易日曆；沒指定的只按周一到周五算。"""
        name = self.series[code].calendar if code in self.series else None
        return self.calendars.get(name, WEEKDAYS) if name else WEEKDAYS

    @property
    def market_series(self) -> list[str]:
        """日度和周度的序列：用來判斷「數據截至哪一天」。"""
        return [s.code for s in self.series.values() if s.frequency in ("daily", "weekly")]

    def has(self, name: str) -> bool:
        """name 是不是一個能取值的序列、計價視角或指標。"""
        return name in self.series or name in self.indicators or name in self.views

    def name_of(self, name: str) -> str:
        if name in self.views:
            return self.views[name].name_zh
        return self.indicators[name].name_zh if name in self.indicators else self.series[name].name_zh

    def frequency_of(self, name: str) -> Frequency:
        """一個序列或指標的頻率。指標取它依賴的序列裡最粗的頻率；只依賴參數的指標算 daily。"""
        if name in self.series:
            return self.series[name].frequency
        if name in self.views:
            return "daily"
        if name not in self.indicators:
            return "daily"
        found = [self.frequency_of(n) for n in fx.names(self.indicators[name].formula)]
        return max(found, key=FREQUENCY_ORDER.index, default="daily")

    def dim(self, unit: str) -> Dim:
        return units.parse_unit(unit, self.atoms)

    def unit_of(self, name: str) -> str:
        """公式裡一個名字的單位。"""
        if name in self.series:
            return self.series[name].unit
        if name in self.parameters:
            return self.parameters[name].unit
        if name in self.views:
            return self.curves[self.views[name].curve].unit
        return self.indicators[name].unit


class _Loader:
    def __init__(self, config_dir: Path):
        self.dir = config_dir
        self.errors: list[str] = []

    def read(self, path: Path) -> Any:
        try:
            with path.open(encoding="utf-8") as fh:
                return yaml.safe_load(fh)
        except FileNotFoundError:
            self.errors.append(f"找不到配置文件：{self.rel(path)}")
        except yaml.YAMLError as exc:
            self.errors.append(f"{self.rel(path)}：YAML 格式錯誤（{exc}）")
        return None

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.dir)) if path.is_relative_to(self.dir) else str(path)

    def files(self, subdir: str) -> list[Path]:
        found = sorted((self.dir / subdir).glob("*.yaml"))
        if not found:
            self.errors.append(f"{subdir}/ 下沒有任何 .yaml 文件")
        return found

    def build(self, model: type[T], raw: Any, path: Path) -> T | None:
        label = raw.get("code") or raw.get("key") or "?" if isinstance(raw, dict) else "?"
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            for err in exc.errors():
                where = ".".join(str(x) for x in err["loc"])
                self.errors.append(f"{self.rel(path)} 的 {label}：{where + '：' if where else ''}{err['msg']}")
            return None

    def section(self, model: type[T], name: str) -> T | None:
        """一個可選的單文件配置（例如 backtest.yaml）：文件不存在、或內容有錯（錯誤已記下）時回傳 None。"""
        path = self.dir / name
        return self.build(model, self.read(path) or {}, path) if path.exists() else None

    def optional_items(self, model: type[T], name: str, label: str) -> dict | None:
        """一個可選的列表文件（例如 geo/analogs.yaml），按 code 建索引；文件不存在時回傳 None。"""
        path = self.dir / name
        return self.keyed(self.items(model, path), label) if path.exists() else None

    def items(self, model: type[T], path: Path, raw: Any = None) -> list[T]:
        raw = self.read(path) if raw is None else raw
        if raw is None:
            return []
        if not isinstance(raw, list):
            self.errors.append(f"{self.rel(path)}：內容應該是一個列表")
            return []
        return [obj for obj in (self.build(model, item, path) for item in raw) if obj is not None]

    def keyed(self, objects: list, label: str, key: str = "code") -> dict:
        out: dict = {}
        for obj in objects:
            code = getattr(obj, key)
            if code in out:
                self.errors.append(f"{label} {code} 重複定義")
            out[code] = obj
        return out


def _optional(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.yaml")) if directory.is_dir() else []


def _load_master(ld: _Loader, cfg: Config) -> None:
    """字典：單位、地區、品種、數據源。"""
    master = ld.dir / "master"
    unit_doc = ld.read(master / "units.yaml") or {}
    cfg.atoms = {a.code for a in ld.items(AtomDef, master / "units.yaml", unit_doc.get("atoms", []))}
    cfg.units = ld.keyed(ld.items(UnitDef, master / "units.yaml", unit_doc.get("units", [])), "單位")
    cfg.locations = ld.keyed(ld.items(LocationDef, master / "locations.yaml"), "地區")
    cfg.commodities = ld.keyed(ld.items(CommodityDef, master / "commodities.yaml"), "品種")
    cfg.sources = ld.keyed(ld.items(SourceDef, master / "sources.yaml"), "數據源")


def _load_series(ld: _Loader, cfg: Config) -> None:
    """序列、期貨曲線（展開成 Mn）、計價視角。"""
    series: list[SeriesDef] = []
    curves: list[CurveDef] = []
    views: list[PricingView] = []
    for path in ld.files("master/series"):
        doc = ld.build(SeriesFile, ld.read(path) or {}, path)
        if doc:
            series += [s for curve in doc.curves for s in curve.expand()] + doc.series
            curves += doc.curves
            views += doc.views
    cfg.curves = ld.keyed(curves, "期貨曲線")
    cfg.views = ld.keyed(views, "計價視角")
    cfg.series = ld.keyed(series, "序列")


def _load_models(ld: _Loader, cfg: Config) -> None:
    """參數、指標，以及會編譯出參數和指標的煉廠模型、平衡表、信號（信號權重是參數）；關係模型。"""
    params = [p for path in ld.files("parameters") for p in ld.items(ParameterDef, path)]
    indicators = [i for path in ld.files("indicators") for i in ld.items(IndicatorDef, path)]
    specs: list[RefinerySpec] = []
    for model in (ld.build(RefineryModelDef, ld.read(p) or {}, p) for p in _optional(ld.dir / "refineries")):
        if model:
            model_params, model_indicators, spec = compile_model(model)
            params += model_params
            indicators += model_indicators
            specs.append(spec)
    balance_specs: list[BalanceSpec] = []
    for table in (ld.build(BalanceDef, ld.read(p) or {}, p) for p in _optional(ld.dir / "balances")):
        if table:
            table_indicators, table_spec = compile_balance(table)
            indicators += table_indicators
            balance_specs.append(table_spec)
    cfg.balances = ld.keyed(balance_specs, "平衡表")
    cfg.relations = ld.keyed([r for path in _optional(ld.dir / "relations")
                              for r in ld.items(RelationDef, path)], "關係模型")
    cfg.signals = ld.keyed([s for path in _optional(ld.dir / "signals")
                            for s in ld.items(SignalDef, path)], "信號")
    # 每個信號的權重是一個參數 W_{code}：帶生效日期和版本記錄，可以按回測結果採用新版本（backtest.calibrate）；
    # 配置裡的 weight 是它的初始值。回測按每個日期當時生效的權重合成，結果可以重現。
    params += [ParameterDef(key=sig.weight_parameter, name_zh=f"信號權重：{sig.name_zh}", group="信號權重",
                            value=sig.weight, verified=True,
                            description="模組內的相對權重；0 = 不參與合成。初始值來自 config/signals/，"
                                        "可在回測頁按校準結果採用新值")
               for sig in cfg.signals.values()]
    cfg.parameters = ld.keyed(params, "參數", key="key")
    cfg.indicators = ld.keyed(indicators, "指標")
    cfg.refineries = ld.keyed(specs, "煉廠模型")


def _load_sections(ld: _Loader, cfg: Config) -> None:
    """可選的配置文件：沒有這個文件就用預設值（內容有錯時錯誤已記下，最後統一報）。"""
    cfg.analogs = ld.optional_items(AnalogDef, "geo/analogs.yaml", "歷史類比") or cfg.analogs
    cfg.playbooks = ld.optional_items(PlaybookDef, "geo/playbooks.yaml", "事件預案") or cfg.playbooks
    cfg.fair_values = ld.optional_items(FairValueModel, "fair_value.yaml", "公允價值模型") or cfg.fair_values
    cfg.projection = ld.section(ProjectionSettings, "projection.yaml")
    cfg.schedule = ld.section(ScheduleDef, "schedule.yaml") or ScheduleDef()
    cfg.notifications = ld.section(NotificationsDef, "notifications.yaml") or NotificationsDef()
    cfg.backtest = ld.section(BacktestSettings, "backtest.yaml") or BacktestSettings()
    cfg.jin10 = ld.section(Jin10Settings, "jin10.yaml") or Jin10Settings()
    cfg.llm = ld.section(LLMSettings, "llm.yaml") or LLMSettings()
    cfg.backup = ld.section(BackupSettings, "backup.yaml") or BackupSettings()
    cfg.freshness = ld.section(FreshnessDef, "freshness.yaml") or FreshnessDef()
    ld.keyed(cfg.notifications.channels, "通知渠道")
    channel_codes = {c.code for c in cfg.notifications.channels}
    for job in cfg.schedule.jobs:
        unknown = [c for c in job.notify or [] if c not in channel_codes]
        if unknown:
            ld.errors.append(f"schedule.yaml 的任務 {job.code}：notify 裡有不存在的通知渠道：{'、'.join(unknown)}")
    calendars_doc = ld.section(CalendarsFile, "calendars.yaml")
    if calendars_doc:
        cfg.calendar_defs = ld.keyed(calendars_doc.calendars, "交易日曆")
        cfg.calendars = {code: build_calendar(d) for code, d in cfg.calendar_defs.items()}
    for curve in cfg.curves.values():                 # 換月的工作日按曲線的日曆算
        if curve.roll is not None and curve.calendar in cfg.calendars:
            curve.roll._calendar = cfg.calendars[curve.calendar]


def load_config(config_dir: Path) -> Config:
    ld = _Loader(config_dir)
    cfg = Config()
    _load_master(ld, cfg)
    _load_series(ld, cfg)
    _load_models(ld, cfg)
    _load_sections(ld, cfg)

    errors = ld.errors + _validate(cfg)
    if errors:
        raise ConfigError("配置校驗失敗：\n  - " + "\n  - ".join(dict.fromkeys(errors)))
    return cfg


def _validate(cfg: Config) -> list[str]:
    return (_validate_dictionary(cfg) + _validate_references(cfg) + _validate_projection(cfg)
            + _validate_schedule(cfg) + _validate_freshness(cfg) + _validate_indicators(cfg))


def _validate_dictionary(cfg: Config) -> list[str]:
    """單位、地區、品種、交易日曆都在字典裡；序列 / 參數 / 指標 / 計價視角不重名；別名不指向兩條序列。"""
    errors: list[str] = []

    def check_unit(unit: str, owner: str) -> None:
        try:
            cfg.dim(unit)
        except units.UnitError as exc:
            errors.append(f"{owner}：{exc}")

    for unit in cfg.units.values():
        check_unit(unit.code, f"單位 {unit.code}")
    for s in cfg.series.values():
        if s.unit not in cfg.units:
            errors.append(f"序列 {s.code}：單位 {s.unit} 不在單位字典裡")
        if s.location not in cfg.locations:
            errors.append(f"序列 {s.code}：地區 {s.location} 不在地區字典裡")
        if s.commodity not in cfg.commodities:
            errors.append(f"序列 {s.code}：品種 {s.commodity} 不在品種字典裡")
    for p in cfg.parameters.values():
        check_unit(p.unit, f"參數 {p.key}")
    for spec in cfg.refineries.values():
        if spec.definition.location not in cfg.locations:
            errors.append(f"煉廠模型 {spec.code}：地區 {spec.definition.location} 不在地區字典裡")

    # 序列、參數、指標共用一個命名空間（都會出現在公式裡）
    spaces = (("序列", set(cfg.series)), ("參數", set(cfg.parameters)), ("指標", set(cfg.indicators)),
              ("計價視角", set(cfg.views)))
    for i, (label_a, a) in enumerate(spaces):
        for label_b, b in spaces[i + 1:]:
            errors += [f"{label_a}與{label_b}重名：{name}" for name in sorted(a & b)]

    owner_of: dict[str, str] = {}
    for s in cfg.series.values():
        for alias in (s.code, s.name_zh, *s.aliases):
            norm = normalize_alias(alias)
            if norm and owner_of.setdefault(norm, s.code) != s.code:
                errors.append(f"別名「{alias}」同時指向 {owner_of[norm]} 和 {s.code}")

    for s in cfg.series.values():
        if s.calendar is not None and s.calendar not in cfg.calendars:
            errors.append(f"序列 {s.code}：交易日曆 {s.calendar} 不在 calendars.yaml 裡")
    for c in cfg.curves.values():
        if c.calendar is not None and c.calendar not in cfg.calendars:
            errors.append(f"曲線 {c.code}：交易日曆 {c.calendar} 不在 calendars.yaml 裡")
    return errors


def _validate_references(cfg: Config) -> list[str]:
    """模型之間的引用：計價視角、關係模型、信號、事件預案、公允價值、回測錨定引用的東西都存在。"""
    errors: list[str] = []
    if cfg.projection is not None:
        codes = [m.code for m in cfg.projection.drift_models]
        if len(codes) != len(set(codes)):
            errors.append("projection.yaml：drift_models 的代碼重複")
        clash = set(codes) & set(cfg.fair_values)
        if clash:
            errors.append(f"projection.yaml：統計漂移模型和公允價值模型重名：{'、'.join(sorted(clash))}")
    for v in cfg.views.values():
        curve = cfg.curves.get(v.curve)
        if curve is None:
            errors.append(f"計價視角 {v.code}：曲線 {v.curve} 不存在")
        elif curve.roll is None:
            errors.append(f"計價視角 {v.code}：曲線 {v.curve} 沒有換月規則（roll），不知道每天的 Mn 是哪個月份")

    for r in cfg.relations.values():
        for side in (r.y, r.x):
            if side not in cfg.series and side not in cfg.indicators:
                errors.append(f"關係模型 {r.code}：{side} 既不是序列也不是指標")

    for sig in cfg.signals.values():
        if sig.source == "fair_value":
            if sig.input not in cfg.fair_values:
                errors.append(f"信號 {sig.code}：{sig.input} 不是公允價值模型（fair_value.yaml）")
        elif not cfg.has(sig.input):
            errors.append(f"信號 {sig.code}：{sig.input} 既不是序列也不是指標")
    for pb in cfg.playbooks.values():
        errors += [f"事件預案 {pb.code}：{i.target} 既不是序列也不是指標" for i in pb.impacts if not cfg.has(i.target)]
    for m in cfg.fair_values.values():
        if m.target not in cfg.series:
            errors.append(f"公允價值模型 {m.code}：target {m.target} 不是序列")
        errors += [f"公允價值模型 {m.code}：因子 {f.input} 既不是序列也不是指標"
                   for f in m.factors if not cfg.has(f.input)]
    if cfg.backtest.anchor and cfg.backtest.anchor not in cfg.series:
        errors.append(f"backtest.yaml：anchor {cfg.backtest.anchor} 不是序列")
    return errors


def _validate_freshness(cfg: Config) -> list[str]:
    errors: list[str] = []
    fr = cfg.freshness
    for label, codes in (("required", fr.required), ("ignore", fr.ignore),
                         ("overrides", [o.series for o in fr.overrides]),
                         ("repeats.series", fr.repeats.series), ("repeats.ignore", fr.repeats.ignore)):
        errors += [f"freshness.yaml 的 {label}：{code} 不是序列" for code in codes if code not in cfg.series]
    missing = [f for f in ("daily", "weekly", "monthly", "quarterly") if f not in fr.defaults]
    if missing:
        errors.append(f"freshness.yaml 的 defaults 缺少：{'、'.join(missing)}")
    return errors


def _validate_schedule(cfg: Config) -> list[str]:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    errors = []
    try:
        ZoneInfo(cfg.schedule.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"schedule.yaml：不認識的時區 {cfg.schedule.timezone}")
    seen: set[str] = set()
    for job in cfg.schedule.jobs:
        if job.code in seen:
            errors.append(f"定時任務 {job.code} 重複定義")
        seen.add(job.code)
        if job.source is None:
            continue
        source = cfg.sources.get(job.source)
        wanted = "api" if job.action == "pull" else "excel"
        if source is None:
            errors.append(f"定時任務 {job.code}：數據源 {job.source} 不存在")
        elif source.kind != wanted:
            errors.append(f"定時任務 {job.code}：{job.action} 需要 {wanted} 類型的數據源，"
                          f"{job.source} 是 {source.kind}")
    return errors


def _validate_projection(cfg: Config) -> list[str]:
    p = cfg.projection
    if p is None:
        return []
    errors = []
    if p.anchor not in cfg.series:
        errors.append(f"projection.yaml：anchor {p.anchor} 不是序列")
    if p.balance_indicator not in cfg.indicators:
        errors.append(f"projection.yaml：balance_indicator {p.balance_indicator} 不是指標")
    for key in (p.sensitivity_parameter, *p.module_weights.values()):
        if key not in cfg.parameters:
            errors.append(f"projection.yaml：參數 {key} 不存在")
    if errors:
        return errors
    try:    # 敏感度的單位必須是 價格 ÷（平衡 × 天），這樣 敏感度 × 累積庫存變化（平衡 × 天數）才是價格
        stock = units.multiply(cfg.dim(cfg.indicators[p.balance_indicator].unit), cfg.dim("d"))
        expected = units.divide(cfg.dim(cfg.series[p.anchor].unit), stock)
        if cfg.dim(cfg.parameters[p.sensitivity_parameter].unit) != expected:
            errors.append(f"projection.yaml：{p.sensitivity_parameter} 的單位應該是 {units.format_dim(expected)}")
        est = p.sensitivity_estimate
        if est is not None:
            if est.stocks not in cfg.series:
                errors.append(f"projection.yaml：sensitivity_estimate.stocks {est.stocks} 不是序列")
            elif cfg.dim(cfg.series[est.stocks].unit) != stock:
                errors.append(f"projection.yaml：sensitivity_estimate.stocks 的單位應該是 {units.format_dim(stock)}"
                              "（平衡 × 天，即庫存）")
    except units.UnitError as exc:
        errors.append(f"projection.yaml：{exc}")
    return errors


def _validate_indicators(cfg: Config) -> list[str]:
    errors: list[str] = []
    known = set(cfg.series) | set(cfg.parameters) | set(cfg.indicators) | set(cfg.views)
    deps: dict[str, list[str]] = {}
    for ind in cfg.indicators.values():
        owner = f"指標 {ind.code}" + (f"（由模型 {ind.model} 生成）" if ind.model else "")
        try:
            deps[ind.code] = fx.names(ind.formula)
        except fx.FormulaError as exc:
            errors.append(f"{owner}：{exc}")
            continue
        unknown = [n for n in deps[ind.code] if n not in known]
        if unknown:
            errors.append(f"{owner}：公式引用了不存在的名字 {', '.join(unknown)}")
            continue
        try:
            declared = cfg.dim(ind.unit)
            inferred = fx.infer_unit(ind.formula, lambda name: cfg.dim(cfg.unit_of(name)))
        except (units.UnitError, fx.FormulaError) as exc:
            errors.append(f"{owner}：{exc}")
            continue
        if declared != inferred:
            errors.append(f"{owner}：聲明的單位是 {ind.unit}，但公式算出來的單位是 {units.format_dim(inferred)}")

    state: dict[str, int] = {}

    def visit(code: str, path: list[str]) -> None:
        if state.get(code) == 2:
            return
        if state.get(code) == 1:
            errors.append("指標循環引用：" + " → ".join([*path[path.index(code):], code]))
            return
        state[code] = 1
        for name in deps.get(code, []):
            if name in cfg.indicators:
                visit(name, [*path, code])
        state[code] = 2

    for code in cfg.indicators:
        visit(code, [])
    for rule in cfg.jin10.calendar_rules:
        if rule.series not in cfg.series:
            errors.append(f"jin10.yaml：日曆規則「{rule.match}」寫入的序列 {rule.series} 不存在")
    if any("循環引用" in e for e in errors):
        return errors

    # lag / ma / diff 等按「期」數的函數，是在公式各輸入對齊後的日期上數的：
    # 混了不同頻率時，周度數據會被攤到每天，lag(周度, 1) 就變成錯開 1 天。乾脆不允許這樣寫。
    for ind in cfg.indicators.values():
        if ind.code not in deps or not fx.uses_window(ind.formula):
            continue
        dynamic = [n for n in deps[ind.code] if n in cfg.series or n in cfg.indicators or n in cfg.views]
        freqs = {cfg.frequency_of(n) for n in dynamic}
        if len(freqs) > 1:
            errors.append(f"指標 {ind.code}：公式用了按期數計算的函數（lag / ma / diff 等），但輸入的頻率不同"
                          f"（{'、'.join(sorted(freqs))}）。請先分別算成同一頻率的指標再組合")
    return errors


def calendar_warnings(cfg: Config, today: date) -> list[str]:
    """逐年登記的日曆（例如中國長假）缺了今年、或 12 月了還沒有明年的，提醒補上。"""
    out = []
    for cal in cfg.calendars.values():
        if cal.years is None:
            continue
        wanted = (today.year, today.year + 1) if today.month == 12 else (today.year,)
        missing = [y for y in wanted if not cal.covers(y)]
        if missing:
            out.append(f"交易日曆 {cal.code}（{cal.name_zh}）還沒有登記 {'、'.join(map(str, missing))} 年的假期："
                       "在 config/calendars.yaml 補上（國務院每年 11 月前後公布），沒登記的年份只按周一到周五算")
    return out
