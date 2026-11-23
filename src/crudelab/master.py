"""把校驗過的 Config 同步到數據庫的主數據表。

config/ 是主數據的唯一來源，數據庫裡的主數據表只是它的鏡像（為了外鍵約束和查詢方便）。
同步規則：
  字典類（單位、地區、品種、數據源、序列、參數說明）  以配置為準覆蓋；配置裡刪掉的序列和參數置為 inactive，數據保留
  參數數值    配置只提供初始值：參數還沒有任何版本時寫入；之後一律以後台為準，不覆蓋
  參數核對狀態  只在新建時取配置值；之後以後台為準
  別名        配置來的別名（origin=config）以配置為準：新增、改指向、配置裡刪掉的也刪掉；
              後台學到的別名（origin=learned）是人工確認過的，不動，同名時優先於配置
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Config, normalize_alias
from .models import Commodity, DataSource, Location, Parameter, ParameterVersion, Series, SeriesAlias, Unit

SYSTEM_ACTOR = "config"
ALIAS_MAX = 128               # series_alias 兩個字段的長度上限（PostgreSQL 會嚴格檢查）


def _mirror(session: Session, model: type[Any], pk: str, rows: list[dict]) -> None:
    existing: dict = {getattr(obj, pk): obj for obj in session.scalars(select(model))}
    for values in rows:
        obj = existing.get(values[pk])
        if obj is None:
            session.add(model(**values))
        else:
            for column, value in values.items():
                setattr(obj, column, value)


def sync_to_db(session: Session, cfg: Config) -> dict[str, int]:
    _mirror(session, Unit, "code", [u.model_dump() for u in cfg.units.values()])
    _mirror(session, Location, "code", [loc.model_dump() for loc in cfg.locations.values()])
    _mirror(session, Commodity, "code", [c.model_dump() for c in cfg.commodities.values()])
    _mirror(session, DataSource, "code", [s.model_dump() for s in cfg.sources.values()])
    session.flush()

    _mirror(session, Series, "code", [dict(
        code=s.code, name_zh=s.name_zh, commodity_code=s.commodity, location_code=s.location, kind=s.kind,
        unit_code=s.unit, frequency=s.frequency, valid_days=s.resolved_valid_days, contract=s.contract,
        benchmark=s.benchmark, module=s.module, external_ids={**({"eia": s.eia_id} if s.eia_id else {}),
                      **({"cftc": s.cftc.model_dump()} if s.cftc else {}),
                      **({"jin10": s.jin10} if s.jin10 else {})},
        notes=s.notes, active=True, within_period=s.within_period,
        vintaged=s.vintaged) for s in cfg.series.values()])
    session.flush()
    for stale in session.scalars(select(Series).where(Series.code.not_in(list(cfg.series)))):
        stale.active = False

    wanted: dict[str, tuple[str, str]] = {}               # 規範化別名 → (原文, 序列)
    for s in cfg.series.values():
        for alias in (s.code, s.name_zh, *s.aliases, *([s.lseg.ric] if s.lseg else [])):
            norm = normalize_alias(alias)
            if norm and len(norm) <= ALIAS_MAX:
                wanted.setdefault(norm, (alias[:ALIAS_MAX], s.code))
    current = {a.alias_norm: a for a in session.scalars(select(SeriesAlias))}
    new_aliases = 0
    for norm, entry in current.items():
        if entry.origin != "config":
            continue
        if norm not in wanted:
            session.delete(entry)                           # 配置裡已經沒有這個別名
        else:
            entry.alias, entry.series_code = wanted[norm]     # 配置改了指向
    for norm, (alias, code) in wanted.items():
        if norm not in current:
            session.add(SeriesAlias(alias_norm=norm, alias=alias, series_code=code, origin="config"))
            new_aliases += 1

    existing = {p.key: p for p in session.scalars(select(Parameter))}
    has_versions = set(session.scalars(select(ParameterVersion.key).distinct()))
    seeded = 0
    for p in cfg.parameters.values():
        param = existing.get(p.key)
        if param is None:
            param = Parameter(key=p.key, verified=p.verified)
            session.add(param)
        param.name_zh, param.unit, param.group = p.name_zh, p.unit, p.group
        param.description, param.model, param.active = p.description, p.model, True
        if p.key not in has_versions:
            session.flush()
            for v in p.seed_versions:
                session.add(ParameterVersion(key=p.key, valid_from=v.valid_from, value=v.value,
                                             note=v.note or "配置初始值", created_by=SYSTEM_ACTOR))
                seeded += 1
    for key, param in existing.items():
        if key not in cfg.parameters:
            param.active = False

    return {"series": len(cfg.series), "parameters": len(cfg.parameters), "indicators": len(cfg.indicators),
            "refinery_models": len(cfg.refineries), "balances": len(cfg.balances),
            "new_aliases": new_aliases, "seeded_parameter_versions": seeded}
