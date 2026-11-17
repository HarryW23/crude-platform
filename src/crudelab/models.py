"""數據表定義。表結構的變更一律通過 migrations/ 下的遷移腳本，不要直接改庫。

六類表：
  主數據     unit / location / commodity / data_source / series / series_alias / parameter
  事實數據   import_batch / observation / parameter_version
  判斷       scenario / adjustment / event / projection / projection_case —— 人的判斷，和數據分開存
  產出       report —— 生成時刻的快照
  修訂歷史   observation_revision
  操作日誌   audit_log

時間一律存 UTC（不帶時區的 datetime）。

兩條時間軸要分清：
  業務時間   observation.obs_date、parameter_version.valid_from —— 這個值「說的是哪一天」
  記錄時間   created_at / updated_at / replaced_at —— 系統「什麼時候知道的」
有了記錄時間，就能還原任意時點當時看到的數據（as_of），回測時不會偷看到後來的修訂。
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {"ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
          "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s", "pk": "pk_%(table_name)s"}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


# ----------------------------- 主數據（來源：config/，由 master.sync_to_db 寫入） -----------------------------

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
    priority: Mapped[int] = mapped_column(Integer)       # 越小越優先
    notes: Mapped[str | None] = mapped_column(Text)


class Series(Base):
    __tablename__ = "series"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(128))
    commodity_code: Mapped[str] = mapped_column(ForeignKey("commodity.code"))
    location_code: Mapped[str] = mapped_column(ForeignKey("location.code"))
    kind: Mapped[str] = mapped_column(String(16))
    unit_code: Mapped[str] = mapped_column(ForeignKey("unit.code"))
    frequency: Mapped[str] = mapped_column(String(16))
    valid_days: Mapped[int] = mapped_column(Integer)                 # 一個值最多沿用幾天
    contract: Mapped[str | None] = mapped_column(String(8))          # 連續合約月序 M1 / M2 …
    benchmark: Mapped[str | None] = mapped_column(String(64))
    module: Mapped[str] = mapped_column(String(16))
    external_ids: Mapped[dict] = mapped_column(JSON)                 # {"eia": "PET.…"}
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)      # 已從配置移除的序列置為 False，數據保留


class SeriesAlias(Base):
    """列名 → 序列 的對照。導入時自動識別靠它；人工確認過的對應也存回這裡（origin=learned）。"""
    __tablename__ = "series_alias"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alias_norm: Mapped[str] = mapped_column(String(128), unique=True)
    alias: Mapped[str] = mapped_column(String(128))
    series_code: Mapped[str] = mapped_column(ForeignKey("series.code"))
    origin: Mapped[str] = mapped_column(String(16))                  # config / learned


class Parameter(Base):
    """參數的說明信息。數值在 parameter_version 裡，帶生效日期。"""
    __tablename__ = "parameter"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32))
    group: Mapped[str] = mapped_column("grp", String(64))
    description: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))            # 由哪個煉廠模型生成
    verified: Mapped[bool] = mapped_column(Boolean, default=False)   # 數值是否已由人核對
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# ----------------------------- 事實數據 -----------------------------

class ParameterVersion(Base):
    """參數值，只增不改。

    某一天生效的值 = valid_from 不晚於那一天的版本中 valid_from 最新的那個；
    同一個 valid_from 有多條記錄時（更正），以 id 最大的為準。
    """
    __tablename__ = "parameter_version"
    __table_args__ = (Index("ix_parameter_version_key_valid_from", "key", "valid_from"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(ForeignKey("parameter.key"))
    valid_from: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ImportBatch(Base):
    """一次文件導入。原文件按 sha256 保存在 data/raw/，批次記下當時用的列對應。"""
    __tablename__ = "import_batch"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_code: Mapped[str] = mapped_column(ForeignKey("data_source.code"))
    filename: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    stored_path: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16))                  # previewed / committed
    mapping: Mapped[dict | None] = mapped_column(JSON)
    stats: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64))


class Observation(Base):
    """一個數據點的當前值：某序列、某天、某來源。同一序列同一天可以並存多個來源。"""
    __tablename__ = "observation"
    __table_args__ = (UniqueConstraint("series_code", "obs_date", "source_code"),
                      Index("ix_observation_series_code_obs_date", "series_code", "obs_date"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_code: Mapped[str] = mapped_column(ForeignKey("series.code"))
    obs_date: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    source_code: Mapped[str] = mapped_column(ForeignKey("data_source.code"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"))
    source_ref: Mapped[str | None] = mapped_column(String(256))      # 例如 "Sheet1!C15"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)   # 第一次入庫
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)   # 當前值入庫


class ScenarioRow(Base):
    """情境：一組有名字的人為調整，疊加在數據之上，不改動數據本身。"""
    __tablename__ = "scenario"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)


class Adjustment(Base):
    """一條調整：在某段日期裡，給某個序列或指標加上一個數。只增不改；撤銷是填 removed_at。"""
    __tablename__ = "adjustment"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_code: Mapped[str] = mapped_column(ForeignKey("scenario.code"), index=True)
    target: Mapped[str] = mapped_column(String(64))                  # 序列或指標代碼
    date_from: Mapped[date] = mapped_column(Date)
    date_to: Mapped[date | None] = mapped_column(Date)               # 空 = 一直持續
    delta: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)                   # 依據：為什麼這樣調
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Event(Base):
    """地緣事件。draft=True 的是大模型從文字裡抽取、還沒有人確認的草稿，不參與任何計算。"""
    __tablename__ = "event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(16))
    location_code: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))      # rumor / confirmed / ongoing / resolved / dismissed
    bias: Mapped[int] = mapped_column(Integer)                       # 對油價的方向判斷：−2 強利空 … +2 強利多
    started_on: Mapped[date | None] = mapped_column(Date)
    ended_on: Mapped[date | None] = mapped_column(Date)
    summary: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)                 # 出處：連結或原文摘錄
    scenario_code: Mapped[str | None] = mapped_column(ForeignKey("scenario.code"))   # 這個事件的量化影響
    origin: Mapped[str] = mapped_column(String(16))                  # manual / llm
    draft: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Projection(Base):
    """一次價格推演的設定：若干個情境分支，各有機率。結果每次按當前數據現算。"""
    __tablename__ = "projection"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)


class ProjectionCase(Base):
    """推演裡的一個分支。scenario_code 為空 = 基準（只看數據，不加任何調整）。"""
    __tablename__ = "projection_case"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    projection_id: Mapped[int] = mapped_column(ForeignKey("projection.id"), index=True)
    label: Mapped[str] = mapped_column(String(128))
    scenario_code: Mapped[str | None] = mapped_column(ForeignKey("scenario.code"))
    probability: Mapped[float] = mapped_column(Float)                # 0–1
    price_adjustment: Mapped[float] = mapped_column(Float, default=0.0)   # 直接加在價格上的判斷（如風險溢價）
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Report(Base):
    """一份報告 = 生成那一刻的數據快照。生成後不再變化，之後數據怎麼改都不影響已出的報告。"""
    __tablename__ = "report"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))                    # daily / weekly / monthly
    title: Mapped[str] = mapped_column(String(200))
    data_date: Mapped[date | None] = mapped_column(Date)
    facts: Mapped[dict] = mapped_column(JSON)                        # 結構化的事實：全部來自指標引擎
    markdown: Mapped[str] = mapped_column(Text)                      # 由 facts 按固定模板排出的正文
    narrative: Mapped[str | None] = mapped_column(Text)              # 大模型寫的解讀（可沒有）
    narrative_meta: Mapped[dict | None] = mapped_column(JSON)        # 哪個模型寫的、數字核對結果
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ----------------------------- 修訂歷史與日誌 -----------------------------

class ObservationRevision(Base):
    """數據點被改寫前的舊值。配合 observation 可以還原任意時點的數據。"""
    __tablename__ = "observation_revision"
    __table_args__ = (Index("ix_observation_revision_point", "series_code", "obs_date", "source_code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_code: Mapped[str] = mapped_column(ForeignKey("series.code"))
    obs_date: Mapped[date] = mapped_column(Date)
    source_code: Mapped[str] = mapped_column(ForeignKey("data_source.code"))
    value: Mapped[float] = mapped_column(Float)                      # 被取代的舊值
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"))
    source_ref: Mapped[str | None] = mapped_column(String(256))
    recorded_at: Mapped[datetime] = mapped_column(DateTime)          # 舊值當初入庫的時間
    replaced_at: Mapped[datetime] = mapped_column(DateTime)          # 被取代的時間
    replaced_by: Mapped[str] = mapped_column(String(64))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(128))
    detail: Mapped[dict | None] = mapped_column(JSON)
