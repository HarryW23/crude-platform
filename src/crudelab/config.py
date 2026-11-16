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

校驗一次做完並一次報全：結構（字段名、類型）、引用（單位、地區、品種、公式裡的名字）、
唯一性（代碼、別名）、公式語法、循環引用、量綱。任何一項不過，整份配置都不生效。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import ValidationError

from . import formula as fx
from . import units
from .refinery import RefinerySpec, compile_model
from .schema import (
    AtomDef,
    CommodityDef,
    IndicatorDef,
    LocationDef,
    ParameterDef,
    RefineryModelDef,
    SeriesDef,
    SeriesFile,
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
    """別名比對用的歸一化：忽略大小寫、空白和常見標點。"""
    return _NORM_RE.sub("", str(text)).lower()


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

    def dim(self, unit: str) -> Dim:
        return units.parse_unit(unit, self.atoms)

    def unit_of(self, name: str) -> str:
        """公式裡一個名字的單位。"""
        if name in self.series:
            return self.series[name].unit
        if name in self.parameters:
            return self.parameters[name].unit
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


def load_config(config_dir: Path) -> Config:
    ld = _Loader(config_dir)
    master = config_dir / "master"
    cfg = Config()

    unit_doc = ld.read(master / "units.yaml") or {}
    cfg.atoms = {a.code for a in ld.items(AtomDef, master / "units.yaml", unit_doc.get("atoms", []))}
    cfg.units = ld.keyed(ld.items(UnitDef, master / "units.yaml", unit_doc.get("units", [])), "單位")
    cfg.locations = ld.keyed(ld.items(LocationDef, master / "locations.yaml"), "地區")
    cfg.commodities = ld.keyed(ld.items(CommodityDef, master / "commodities.yaml"), "品種")
    cfg.sources = ld.keyed(ld.items(SourceDef, master / "sources.yaml"), "數據源")

    series: list[SeriesDef] = []
    for path in ld.files("master/series"):
        doc = ld.build(SeriesFile, ld.read(path) or {}, path)
        if doc:
            series += [s for curve in doc.curves for s in curve.expand()] + doc.series
    cfg.series = ld.keyed(series, "序列")

    params = [p for path in ld.files("parameters") for p in ld.items(ParameterDef, path)]
    indicators = [i for path in ld.files("indicators") for i in ld.items(IndicatorDef, path)]
    specs: list[RefinerySpec] = []
    refinery_dir = config_dir / "refineries"
    for path in sorted(refinery_dir.glob("*.yaml")) if refinery_dir.is_dir() else []:
        model = ld.build(RefineryModelDef, ld.read(path) or {}, path)
        if model:
            model_params, model_indicators, spec = compile_model(model)
            params += model_params
            indicators += model_indicators
            specs.append(spec)
    cfg.parameters = ld.keyed(params, "參數", key="key")
    cfg.indicators = ld.keyed(indicators, "指標")
    cfg.refineries = ld.keyed(specs, "煉廠模型")

    errors = ld.errors + _validate(cfg)
    if errors:
        raise ConfigError("配置校驗失敗：\n  - " + "\n  - ".join(dict.fromkeys(errors)))
    return cfg


def _validate(cfg: Config) -> list[str]:
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
    spaces = (("序列", set(cfg.series)), ("參數", set(cfg.parameters)), ("指標", set(cfg.indicators)))
    for i, (label_a, a) in enumerate(spaces):
        for label_b, b in spaces[i + 1:]:
            errors += [f"{label_a}與{label_b}重名：{name}" for name in sorted(a & b)]

    owner_of: dict[str, str] = {}
    for s in cfg.series.values():
        for alias in (s.code, s.name_zh, *s.aliases):
            norm = normalize_alias(alias)
            if norm and owner_of.setdefault(norm, s.code) != s.code:
                errors.append(f"別名「{alias}」同時指向 {owner_of[norm]} 和 {s.code}")

    errors += _validate_indicators(cfg)
    return errors


def _validate_indicators(cfg: Config) -> list[str]:
    errors: list[str] = []
    known = set(cfg.series) | set(cfg.parameters) | set(cfg.indicators)
    deps: dict[str, list[str]] = {}
    for ind in cfg.indicators.values():
        owner = f"指標 {ind.code}" + (f"（煉廠模型 {ind.model}）" if ind.model else "")
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
    return errors
