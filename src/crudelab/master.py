"""主數據：讀取 config/ 下的 YAML，校驗後同步到數據庫。

YAML 是主數據的唯一來源（可以放進 git 做版本管理）。同步規則：
  - 字典類（單位、地區、品種、數據源、序列）：以 YAML 為準覆蓋
  - 參數：只新增，不覆蓋後台已經改過的值
  - 別名：只新增，不動後台學到的別名
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Commodity, DataSource, Location, Parameter, Series, SeriesAlias, Unit

CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_NORM_RE = re.compile(r"[\s\-_/().,#:：（）]+")


class ConfigError(ValueError):
    """配置文件有錯。訊息會指出是哪個文件、哪一項。"""


def normalize_alias(text: str) -> str:
    """別名比對用的歸一化：忽略大小寫、空白和常見標點。"""
    return _NORM_RE.sub("", str(text)).lower()


@dataclass
class MasterConfig:
    units: list[dict] = field(default_factory=list)
    locations: list[dict] = field(default_factory=list)
    commodities: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    series: list[dict] = field(default_factory=list)
    parameters: list[dict] = field(default_factory=list)
    indicators: list[dict] = field(default_factory=list)


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or []


def _expand_curves(curves: list[dict]) -> list[dict]:
    """把期貨曲線模板展開成逐月序列。"""
    out = []
    for curve in curves:
        for n in curve["months"]:
            item = {k: v for k, v in curve.items() if k not in ("months", "aliases")}
            item["code"] = f"{curve['code']}_M{n}"
            item["name_zh"] = f"{curve['name_zh']} M{n}"
            item["contract"] = f"M{n}"
            item["aliases"] = [a.replace("{n}", str(n)) for a in curve.get("aliases", [])]
            out.append(item)
    return out


def load_config(config_dir: Path) -> MasterConfig:
    master = config_dir / "master"
    series_doc = _load_yaml(master / "series.yaml")
    cfg = MasterConfig(
        units=_load_yaml(master / "units.yaml"),
        locations=_load_yaml(master / "locations.yaml"),
        commodities=_load_yaml(master / "commodities.yaml"),
        sources=_load_yaml(master / "sources.yaml"),
        series=_expand_curves(series_doc.get("curves", [])) + series_doc.get("series", []),
        parameters=_load_yaml(config_dir / "parameters.yaml"),
        indicators=_load_yaml(config_dir / "indicators.yaml"),
    )
    validate(cfg)
    return cfg


def validate(cfg: MasterConfig) -> None:
    """交叉校驗：引用必須存在，代碼不能重複，三類名字共用一個命名空間。"""
    errors: list[str] = []

    def unique(items: list[dict], key: str, label: str) -> set[str]:
        seen: set[str] = set()
        for item in items:
            code = item.get(key)
            if not code:
                errors.append(f"{label}：有一項缺少 {key}")
            elif code in seen:
                errors.append(f"{label}：{code} 重複")
            seen.add(code)
        return seen

    units = unique(cfg.units, "code", "單位")
    locations = unique(cfg.locations, "code", "地區")
    commodities = unique(cfg.commodities, "code", "品種")
    unique(cfg.sources, "code", "數據源")
    series = unique(cfg.series, "code", "序列")
    params = unique(cfg.parameters, "key", "參數")
    indicators = unique(cfg.indicators, "code", "指標")

    for s in cfg.series:
        code = s.get("code", "?")
        if not CODE_RE.match(str(code)):
            errors.append(f"序列 {code}：代碼只能用大寫字母、數字、下劃線")
        if s.get("unit") not in units:
            errors.append(f"序列 {code}：單位 {s.get('unit')} 不在單位字典裡")
        if s.get("location") not in locations:
            errors.append(f"序列 {code}：地區 {s.get('location')} 不在地區字典裡")
        if s.get("commodity") not in commodities:
            errors.append(f"序列 {code}：品種 {s.get('commodity')} 不在品種字典裡")

    for name, group in (("參數", params), ("指標", indicators)):
        for code in group:
            if not CODE_RE.match(str(code)):
                errors.append(f"{name} {code}：代碼只能用大寫字母、數字、下劃線")
    for a, b, label in ((series, params, "序列與參數"), (series, indicators, "序列與指標"),
                        (params, indicators, "參數與指標")):
        for clash in sorted(a & b):
            errors.append(f"{label}重名：{clash}")

    alias_owner: dict[str, str] = {}
    for s in cfg.series:
        for alias in [s["code"], s.get("name_zh", ""), *s.get("aliases", [])]:
            norm = normalize_alias(alias)
            if not norm:
                continue
            owner = alias_owner.setdefault(norm, s["code"])
            if owner != s["code"]:
                errors.append(f"別名「{alias}」同時指向 {owner} 和 {s['code']}")

    from .indicators import validate_definitions
    errors.extend(validate_definitions(cfg.indicators, series, params))

    if errors:
        raise ConfigError("配置校驗失敗：\n  - " + "\n  - ".join(errors))


def _upsert(session: Session, model: type, pk: str, values: dict) -> None:
    obj = session.get(model, values[pk])
    if obj is None:
        session.add(model(**values))
    else:
        for k, v in values.items():
            setattr(obj, k, v)


def sync_to_db(session: Session, cfg: MasterConfig) -> dict[str, int]:
    for u in cfg.units:
        _upsert(session, Unit, "code", dict(code=u["code"], name_zh=u["name_zh"], dimension=u["dimension"]))
    for loc in cfg.locations:
        _upsert(session, Location, "code", dict(code=loc["code"], name_zh=loc["name_zh"], region=loc["region"]))
    for c in cfg.commodities:
        _upsert(session, Commodity, "code", dict(code=c["code"], name_zh=c["name_zh"], category=c["category"]))
    for src in cfg.sources:
        _upsert(session, DataSource, "code", dict(
            code=src["code"], name=src["name"], kind=src["kind"], status=src["status"],
            priority=src.get("priority", 50), notes=src.get("notes")))
    session.flush()

    for s in cfg.series:
        external = {"eia": s["eia_id"]} if s.get("eia_id") else {}
        _upsert(session, Series, "code", dict(
            code=s["code"], name_zh=s["name_zh"], commodity_code=s["commodity"], location_code=s["location"],
            kind=s["kind"], unit_code=s["unit"], frequency=s.get("frequency", "daily"),
            contract=s.get("contract"), benchmark=s.get("benchmark"), module=s.get("module", "common"),
            external_ids=external, notes=s.get("notes"), active=True))
    session.flush()

    existing = set(session.scalars(select(SeriesAlias.alias_norm)))
    new_aliases = 0
    for s in cfg.series:
        for alias in [s["code"], s["name_zh"], *s.get("aliases", [])]:
            norm = normalize_alias(alias)
            if norm and norm not in existing:
                session.add(SeriesAlias(alias_norm=norm, alias=alias, series_code=s["code"], origin="config"))
                existing.add(norm)
                new_aliases += 1

    new_params = 0
    for p in cfg.parameters:
        obj = session.get(Parameter, p["key"])
        if obj is None:
            session.add(Parameter(key=p["key"], name_zh=p["name_zh"], value=float(p["value"]), unit=p.get("unit"),
                                  group=p.get("group", "通用"), description=p.get("description")))
            new_params += 1
        else:  # 值不覆蓋，只更新說明性字段
            obj.name_zh, obj.unit = p["name_zh"], p.get("unit")
            obj.group, obj.description = p.get("group", "通用"), p.get("description")

    return {"units": len(cfg.units), "locations": len(cfg.locations), "commodities": len(cfg.commodities),
            "sources": len(cfg.sources), "series": len(cfg.series), "new_aliases": new_aliases,
            "new_parameters": new_params, "indicators": len(cfg.indicators)}
