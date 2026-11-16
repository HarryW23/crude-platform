"""把校驗過的 Config 同步到數據庫的主數據表。

config/ 是主數據的唯一來源，數據庫裡的主數據表只是它的鏡像（為了外鍵約束和查詢方便）。
同步規則：
  字典類（單位、地區、品種、數據源、序列、參數說明）  以配置為準覆蓋；配置裡刪掉的序列和參數置為 inactive，數據保留
  參數數值    配置只提供初始值：參數還沒有任何版本時寫入；之後一律以後台為準，不覆蓋
  參數核對狀態  只在新建時取配置值；之後以後台為準
  別名        只新增，不動後台學到的別名
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Config, normalize_alias
from .models import Commodity, DataSource, Location, Parameter, ParameterVersion, Series, SeriesAlias, Unit

SYSTEM_ACTOR = "config"


def _mirror(session: Session, model: type, pk: str, rows: list[dict]) -> None:
    existing = {getattr(obj, pk): obj for obj in session.scalars(select(model))}
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
        benchmark=s.benchmark, module=s.module, external_ids={"eia": s.eia_id} if s.eia_id else {},
        notes=s.notes, active=True) for s in cfg.series.values()])
    session.flush()
    for row in session.scalars(select(Series).where(Series.code.not_in(list(cfg.series)))):
        row.active = False

    known_aliases = set(session.scalars(select(SeriesAlias.alias_norm)))
    new_aliases = 0
    for s in cfg.series.values():
        for alias in (s.code, s.name_zh, *s.aliases):
            norm = normalize_alias(alias)
            if norm and norm not in known_aliases:
                session.add(SeriesAlias(alias_norm=norm, alias=alias, series_code=s.code, origin="config"))
                known_aliases.add(norm)
                new_aliases += 1

    existing = {p.key: p for p in session.scalars(select(Parameter))}
    has_versions = set(session.scalars(select(ParameterVersion.key).distinct()))
    seeded = 0
    for p in cfg.parameters.values():
        row = existing.get(p.key)
        if row is None:
            row = Parameter(key=p.key, verified=p.verified)
            session.add(row)
        row.name_zh, row.unit, row.group = p.name_zh, p.unit, p.group
        row.description, row.model, row.active = p.description, p.model, True
        if p.key not in has_versions:
            session.flush()
            for v in p.seed_versions:
                session.add(ParameterVersion(key=p.key, valid_from=v.valid_from, value=v.value,
                                             note=v.note or "配置初始值", created_by=SYSTEM_ACTOR))
                seeded += 1
    for key, row in existing.items():
        if key not in cfg.parameters:
            row.active = False

    return {"series": len(cfg.series), "parameters": len(cfg.parameters), "indicators": len(cfg.indicators),
            "refinery_models": len(cfg.refineries), "new_aliases": new_aliases, "seeded_parameter_versions": seeded}
