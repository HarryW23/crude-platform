"""數據表定義。表結構的變更一律通過 migrations/ 下的遷移腳本，不要直接改庫。

八類表：
  主數據     unit / location / commodity / data_source / series / series_alias / parameter
  事實數據   import_batch / observation / parameter_version
  判斷       scenario / adjustment / event / projection / projection_case —— 人的判斷，和數據分開存
  產出       report / backtest_run —— 生成時刻的快照
  帳號       user_account / user_session
  調度       job_run / scheduler_heartbeat / switch / notification / login_failure
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
    false,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {"ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
          "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s", "pk": "pk_%(table_name)s"}


def iso_utc(value: datetime | None) -> str | None:
    """數據庫裡的時間都是不帶時區的 UTC。給頁面時明確標上 Z，由頁面按使用者選的時區顯示。"""
    if value is None:
        return None
    value = value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat(timespec="seconds") + "Z"


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
    within_period: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())   # 見 SeriesDef
    vintaged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())        # 按報告期保存


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
    vintage: Mapped[date | None] = mapped_column(Date)               # 機構報告的發佈日期（補錄歷史報告時填）


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


class ObservationVintage(Base):
    """按期保存的序列（機構預測：STEO、IEA、OPEC）每一期報告裡的數字。vintage = 那一期的發佈日期。

    主表 observation 保存最新一期（頁面照常用）；這裡保存每一期，用來還原「某天之前發佈的最新一期是什麼」，
    回測就不會用到後來的修訂。只在數字變了時記新的一期（每天取數、數字沒變不會多存）。
    """
    __tablename__ = "observation_vintage"
    __table_args__ = (UniqueConstraint("series_code", "obs_date", "source_code", "vintage"),
                      Index("ix_observation_vintage_series_code_vintage", "series_code", "vintage"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_code: Mapped[str] = mapped_column(ForeignKey("series.code"))
    obs_date: Mapped[date] = mapped_column(Date)
    source_code: Mapped[str] = mapped_column(ForeignKey("data_source.code"))
    vintage: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"))
    source_ref: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
    probability: Mapped[float | None] = mapped_column(Float)         # 推演期限內發生 / 持續的機率 0–1（人的判斷）
    price_premium: Mapped[float | None] = mapped_column(Float)       # 發生時直接加在價格上的溢價（錨定價格的單位）
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
    drift_model: Mapped[str | None] = mapped_column(String(64))      # 用哪個公允價值模型給漂移；空 = 不加
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)


class MarketView(Base):
    """一條記下的觀點（判斷）。記下後內容不能改，只能撤銷：這是覆盤可信的前提。
    結果不存，到期後按當時的數據現算（數據修訂了，結果跟著修正）。"""
    __tablename__ = "market_view"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    made_on: Mapped[date] = mapped_column(Date)                       # 判斷的日期（以這天的收盤價為起點）
    horizon: Mapped[str] = mapped_column(String(8))                    # 期限代碼：W1 / M1 / M3
    target_date: Mapped[date] = mapped_column(Date, index=True)        # 到期日 = made_on + 期限的日曆天數
    series_code: Mapped[str] = mapped_column(String(64))               # 看的是哪個價格（推演的錨定價格）
    direction: Mapped[int] = mapped_column(Integer)                    # −1 偏空 / 0 中性 / +1 偏多
    low: Mapped[float | None] = mapped_column(Float)                   # 目標區間（可不填）
    high: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)            # 方向判斷對的把握 0.5–1（可不填）
    reasons: Mapped[list] = mapped_column(JSON)                        # 理由的因子標籤
    rationale: Mapped[str | None] = mapped_column(Text)
    start_price: Mapped[float] = mapped_column(Float)                  # 起點價格（made_on 當天或之前最近的值）
    start_date: Mapped[date] = mapped_column(Date)
    daily_vol: Mapped[float | None] = mapped_column(Float)            # 當時的日波動率：決定「中性」的範圍
    system: Mapped[dict | None] = mapped_column(JSON)                  # 當時系統信號的快照（綜合與各模組分數）
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_reason: Mapped[str | None] = mapped_column(Text)


class ProjectionCase(Base):
    """推演裡的一個分支。scenario_code 為空 = 基準（只看數據，不加任何調整）。"""
    __tablename__ = "projection_case"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    projection_id: Mapped[int] = mapped_column(ForeignKey("projection.id"), index=True)
    label: Mapped[str] = mapped_column(String(128))
    scenario_code: Mapped[str | None] = mapped_column(ForeignKey("scenario.code"))
    probability: Mapped[float] = mapped_column(Float)                # 0–1
    price_adjustment: Mapped[float] = mapped_column(Float, default=0.0)   # 直接加在價格上的判斷（如風險溢價）
    event_id: Mapped[int | None] = mapped_column(ForeignKey("event.id"))     # 由地緣事件同步生成的分支
    event_probability: Mapped[float | None] = mapped_column(Float)          # 同步時事件的機率（用來發現事件改過）
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


class UserAccount(Base):
    """登錄帳號。密碼只存加鹽哈希（auth.py）。帳號不刪除，只停用，這樣歷史記錄裡的操作者始終對得上。"""
    __tablename__ = "user_account"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16))                    # viewer / analyst / admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class UserSession(Base):
    """一次登錄。瀏覽器拿到的是隨機令牌，這裡只存它的 SHA-256，所以數據庫洩漏也冒充不了任何人。"""
    __tablename__ = "user_session"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_account.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class LoginFailure(Base):
    """一次登錄失敗（#36）。存數據庫而不是內存：多進程、重啟後鎖定仍然有效。
    只記小寫用戶名和時間（不記密碼、不記 IP）；過了計數窗口的記錄在下一次失敗時順手清掉。"""
    __tablename__ = "login_failure"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username_key: Mapped[str] = mapped_column(String(128), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)


class JobRun(Base):
    """定時任務的一次執行。(job_code, slot, attempt) 唯一：
    同一個時點的同一次嘗試，不管有幾個進程在跑調度，只會被執行一次。"""
    __tablename__ = "job_run"
    __table_args__ = (UniqueConstraint("job_code", "slot", "attempt"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_code: Mapped[str] = mapped_column(String(64), index=True)
    slot: Mapped[datetime] = mapped_column(DateTime)                 # 這次執行對應的計劃時點（手動觸發時是觸發時刻）
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")   # 同一個時點的第幾次（重試 + 1）
    trigger: Mapped[str] = mapped_column(String(16))                 # schedule / manual
    status: Mapped[str] = mapped_column(String(16))                  # running / ok / skipped / failed / retry / retried
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    message: Mapped[str | None] = mapped_column(Text)                # 給人看的一句話結果（已脫敏）
    detail: Mapped[dict | None] = mapped_column(JSON)


class AppSetting(Base):
    """後台頁面上改的設定（不是開關的那種），例如大模型用哪個型號、開不開思考模式。值是 JSON。不存密鑰。"""
    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str] = mapped_column(String(64))


class ExternalCall(Base):
    """一次對外部數據服務的調用（目前是金十）。用來按「每個工具每天」計數、守住對方的額度，也讓人看得到用量。"""
    __tablename__ = "external_call"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    tool: Mapped[str] = mapped_column(String(64))
    day: Mapped[date] = mapped_column(Date, index=True)             # 對方計額度的日曆日（金十按北京時間）
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(16))                  # ok / failed
    message: Mapped[str | None] = mapped_column(Text)


class CalendarEvent(Base):
    """財經日曆的一項（例如 EIA 原油庫存）。預期值會在公佈前變動：實際值第一次出現時，把當時的預期凍結下來，
    「超預期」一律用凍結的預期算，避免事後修改的預期讓歷史看起來更準。"""
    __tablename__ = "calendar_event"
    __table_args__ = (UniqueConstraint("source", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(64))                     # 公佈時間 + 標題的雜湊
    pub_time: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    title: Mapped[str] = mapped_column(String(300))
    star: Mapped[int | None] = mapped_column(Integer)                 # 重要性 1–5
    previous: Mapped[str | None] = mapped_column(String(64))
    consensus: Mapped[str | None] = mapped_column(String(64))
    actual: Mapped[str | None] = mapped_column(String(64))
    revised: Mapped[str | None] = mapped_column(String(64))
    affect: Mapped[str | None] = mapped_column(String(64))            # 對方給的「利多 / 利空」說明
    consensus_at_release: Mapped[str | None] = mapped_column(String(64))
    released_at: Mapped[datetime | None] = mapped_column(DateTime)    # 我們第一次看到實際值的時間
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NewsFlash(Base):
    """一條收錄的快訊（命中原油相關關鍵詞的才收錄）。可以交給大模型抽取成事件草稿，草稿要人確認。"""
    __tablename__ = "news_flash"
    __table_args__ = (UniqueConstraint("source", "ext_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    ext_id: Mapped[str] = mapped_column(String(64))                  # 對方的編號（沒有時用時間 + 內容的雜湊）
    time: Mapped[datetime] = mapped_column(DateTime, index=True)     # UTC
    content: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(500))
    keywords: Mapped[list] = mapped_column(JSON)                     # 命中的關鍵詞
    important: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime)  # 交給大模型抽取的時間
    drafts: Mapped[int] = mapped_column(Integer, default=0)          # 抽出了幾條事件草稿


class LLMCall(Base):
    """一次大模型調用。用來做跨進程的頻率和用量限制，也讓人看得到花了多少。不存發出去的內容和回覆。"""
    __tablename__ = "llm_call"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(32))                 # chat / extract / narrative / test
    status: Mapped[str] = mapped_column(String(16))                  # running / ok / failed
    http_status: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)                # 失敗原因（我們自己的說明，已脫敏）


class LLMPause(Base):
    """某家大模型被自動暫停（密鑰無效、餘額不足、觸發頻率限制、連續出錯）。到期自動恢復，也可以手動解除。"""
    __tablename__ = "llm_pause"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    until: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(Text)
    strikes: Mapped[int] = mapped_column(Integer, default=0)         # 連續觸發頻率限制的次數，用來加長暫停


class Switch(Base):
    """後台頁面上的開關。沒有記錄 = 用配置文件裡的預設值；有記錄 = 以這裡為準。

    key 的寫法：scheduler（定時任務總開關）、job:任務代碼、notify（通知總開關）、channel:渠道代碼。
    """
    __tablename__ = "switch"
    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    updated_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Notification(Base):
    """發出過的通知，以及每個渠道的發送結果（不含地址）。"""
    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    job_code: Mapped[str | None] = mapped_column(String(64), index=True)
    event: Mapped[str] = mapped_column(String(16))                   # failure / reminder / recovery / test
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    results: Mapped[dict] = mapped_column(JSON)                      # {渠道代碼: "ok" 或失敗原因}


class BacktestRun(Base):
    """一次回測。結果是生成那一刻的快照，存成 JSON。"""
    __tablename__ = "backtest_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16))                  # running / done / failed
    mode: Mapped[str] = mapped_column(String(16))                    # observation / vintage
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    step: Mapped[int] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    results: Mapped[dict | None] = mapped_column(JSON)


class SchedulerHeartbeat(Base):
    """調度器的心跳，只有一行。頁面用它判斷調度器是不是在運行。"""
    __tablename__ = "scheduler_heartbeat"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime)
    host: Mapped[str] = mapped_column(String(128))


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
