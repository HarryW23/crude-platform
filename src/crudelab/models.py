"""數據表定義。

三類表：
  主數據     Unit / Location / Commodity / DataSource / Series / SeriesAlias / Parameter
  事實數據   ImportBatch / Observation
  留痕       ParameterHistory / AuditLog
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ----------------------------- 主數據 -----------------------------

class Unit(Base):
    __tablename__ = "unit"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(64))
    dimension: Mapped[str] = mapped_column(String(32))


class Location(Base):
    __tablename__ = "location"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(32))


class Commodity(Base):
    __tablename__ = "commodity"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))


class DataSource(Base):
    __tablename__ = "data_source"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))        # manual / excel / api
    status: Mapped[str] = mapped_column(String(16))      # active / reserved
    priority: Mapped[int] = mapped_column(Integer, default=50)  # 越小越優先
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Series(Base):
    __tablename__ = "series"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(128))
    commodity_code: Mapped[str] = mapped_column(ForeignKey("commodity.code"))
    location_code: Mapped[str] = mapped_column(ForeignKey("location.code"))
    kind: Mapped[str] = mapped_column(String(16))
    unit_code: Mapped[str] = mapped_column(ForeignKey("unit.code"))
    frequency: Mapped[str] = mapped_column(String(16), default="daily")
    contract: Mapped[str | None] = mapped_column(String(8), nullable=True)   # M1 / M2 …
    benchmark: Mapped[str | None] = mapped_column(String(64), nullable=True)
    module: Mapped[str] = mapped_column(String(16), default="common")
    external_ids: Mapped[dict] = mapped_column(JSON, default=dict)           # {"eia": "PET.…"}
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SeriesAlias(Base):
    """列名 → 序列 的對照。導入時自動識別靠它；人工確認過的對應會存回這裡。"""
    __tablename__ = "series_alias"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alias_norm: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    alias: Mapped[str] = mapped_column(String(128))
    series_code: Mapped[str] = mapped_column(ForeignKey("series.code"))
    origin: Mapped[str] = mapped_column(String(16), default="config")        # config / learned


class Parameter(Base):
    __tablename__ = "parameter"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(128))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    group: Mapped[str] = mapped_column("grp", String(32), default="通用")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ----------------------------- 事實數據 -----------------------------

class ImportBatch(Base):
    """一次導入 = 一個批次。原文件按 sha256 保存，批次記錄當時用的列對應。"""
    __tablename__ = "import_batch"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_code: Mapped[str | None] = mapped_column(ForeignKey("data_source.code"), nullable=True)
    filename: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64))
    stored_path: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="previewed")     # previewed / committed / failed
    mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="local")


class Observation(Base):
    """一個數據點：某序列、某天、某來源的值。同一序列同一天可以並存多個來源。"""
    __tablename__ = "observation"
    __table_args__ = (UniqueConstraint("series_code", "obs_date", "source_code", name="uq_obs"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_code: Mapped[str] = mapped_column(ForeignKey("series.code"), index=True)
    obs_date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[float] = mapped_column(Float)
    source_code: Mapped[str] = mapped_column(ForeignKey("data_source.code"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)  # 例如 "Sheet1!C15"
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ----------------------------- 留痕 -----------------------------

class ParameterHistory(Base):
    __tablename__ = "parameter_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(ForeignKey("parameter.key"), index=True)
    old_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_value: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str] = mapped_column(String(64), default="local")
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), default="local")
    action: Mapped[str] = mapped_column(String(32))
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(128))
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
