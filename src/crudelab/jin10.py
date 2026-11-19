"""金十數據：財經日曆（含市場預期）、原油相關快訊、報價。接口是 MCP（見 connectors/mcp.py）。

額度：對方每個工具每天最多 1500 次（北京時間的日曆日）。每次調用前查 external_call 表裡今天這個工具用了多少，
超過 jin10.yaml 的 max_calls_per_tool_per_day 就不發請求；每次調用都記一條（不記內容）。

財經日曆（sync_calendar）
  每次同步整張日曆（接口只給近期，沒有歷史參數），按「公佈時間 + 標題」更新。
  實際值第一次出現時，把當時的預期凍結成 consensus_at_release：「超預期」一律用它，不用之後可能被改的預期。
  jin10.yaml 的 calendar_rules 把特定項目（例如 EIA 原油庫存）的超預期換算後寫成序列，可以進指標、信號、回測。
  時間：金十的 pub_time 是北京時間，存成 UTC；序列的日期用公佈當天的北京日期。

⚠ 日曆條目和快訊的字段是按金十的接入指南寫的；條目的標題寫法、快訊條目的字段名沒有在真實接口上核對過。
  crudelab jin10 probe 會把真實的樣子列出來。
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import credentials, db
from .config import Config
from .connectors.mcp import MCPClient, MCPError
from .ingest import Obs, write_observations
from .models import CalendarEvent, ExternalCall, utcnow

SOURCE = "jin10"
BEIJING = ZoneInfo("Asia/Shanghai")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


class Jin10NotReady(RuntimeError):
    """還沒有設定 token。"""


class Jin10LimitReached(RuntimeError):
    """今天這個工具的調用次數到了本地上限，沒有發請求。"""


def beijing_today() -> date:
    return datetime.now(BEIJING).date()


def used_today(tool: str) -> int:
    """今天（北京時間）這個工具已經調用了幾次。"""
    with db.session_scope() as session:
        return session.scalar(select(func.count()).select_from(ExternalCall).where(
            ExternalCall.source == SOURCE, ExternalCall.tool == tool, ExternalCall.day == beijing_today())) or 0


class Jin10:
    """帶額度控制和調用記錄的金十客戶端。用完要 close()。

    調用記錄用獨立的數據庫會話寫入並立即提交：調用方的事務即使回滾，用量也不會漏記。
    因此調用接口之前，調用方的會話不能有未提交的寫入（SQLite 會互相鎖住）：先取數、再寫庫。
    """

    def __init__(self, cfg: Config, client: MCPClient | None = None):
        self.cfg = cfg
        if client is None:
            token = credentials.get("JIN10_TOKEN")
            if not token:
                raise Jin10NotReady("還沒有設定 JIN10_TOKEN（在服務器上運行 crudelab secret set JIN10_TOKEN）")
            client = MCPClient("金十", cfg.jin10.url, token, cfg.jin10.protocol_version)
        self.client = client

    def close(self) -> None:
        self.client.close()

    def _record(self, tool: str, status: str, message: str | None) -> None:
        with db.session_scope() as session:
            session.add(ExternalCall(source=SOURCE, tool=tool, day=beijing_today(), status=status,
                                     message=message))

    def _guarded(self, tool: str, run):
        limit = self.cfg.jin10.max_calls_per_tool_per_day
        if used_today(tool) >= limit:
            raise Jin10LimitReached(f"金十的 {tool} 今天已經調用了 {limit} 次（jin10.yaml 的上限），明天再取")
        try:
            result = run()
        except MCPError as exc:
            self._record(tool, "failed", credentials.redact(exc)[:300])
            raise
        self._record(tool, "ok", None)
        return result

    def call(self, tool: str, **arguments):
        return self._guarded(tool, lambda: self.client.call(tool, arguments))

    def resource(self, uri: str):
        return self._guarded(uri, lambda: self.client.read_resource(uri))


# ------------------------------ 工具 ------------------------------

def rows_of(content) -> list[dict]:
    """structuredContent 裡的列表：可能直接是列表，也可能在 data 或 data.items 裡。"""
    data = content.get("data", content) if isinstance(content, dict) else content
    if isinstance(data, dict):
        data = data.get("items", [])
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def number(text) -> float | None:
    """「-523.5」「3.2%」「-1.2万」這類寫法裡的數字（不換算單位：單位由規則的 scale 處理）。"""
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    found = _NUMBER.findall(str(text).replace(",", ""))
    return float(found[0]) if len(found) == 1 else None


def parse_time(text) -> datetime | None:
    """金十的時間（北京時間，例如 2026-11-19 23:30:00）→ 不帶時區的 UTC。"""
    if not text:
        return None
    raw = str(text).strip().replace("T", " ").replace("/", "-")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            local = datetime.strptime(raw, fmt).replace(tzinfo=BEIJING)
        except ValueError:
            continue
        return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return None


def _clip(value, size: int = 64) -> str | None:
    if value is None or value == "":
        return None
    return str(value)[:size]


# ------------------------------ 財經日曆 ------------------------------

def surprise(event: CalendarEvent) -> float | None:
    """實際 − 公佈時凍結的預期。任一邊不是單個數字就是 None。"""
    actual, expected = number(event.actual), number(event.consensus_at_release)
    return None if actual is None or expected is None else actual - expected


def sync_calendar(session: Session, cfg: Config, jin: Jin10) -> dict:
    rows = rows_of(jin.call("list_calendar"))
    now = utcnow()
    stats = {"items": len(rows), "new": 0, "updated": 0, "released": 0, "skipped": 0, "observations": 0}
    existing = {e.key: e for e in session.scalars(select(CalendarEvent).where(CalendarEvent.source == SOURCE))}
    touched: list[CalendarEvent] = []
    for row in rows:
        when, title = parse_time(row.get("pub_time")), str(row.get("title") or "").strip()
        if when is None or not title:
            stats["skipped"] += 1
            continue
        key = hashlib.sha256(f"{when.isoformat()}|{title}".encode()).hexdigest()[:40]
        fields = {"star": int(row["star"]) if str(row.get("star", "")).isdigit() else None,
                  "previous": _clip(row.get("previous")), "consensus": _clip(row.get("consensus")),
                  "actual": _clip(row.get("actual")), "revised": _clip(row.get("revised")),
                  "affect": _clip(row.get("affect_txt"))}
        event = existing.get(key)
        if event is None:
            event = CalendarEvent(source=SOURCE, key=key, pub_time=when, title=title[:300], first_seen_at=now,
                                  updated_at=now, **fields)
            session.add(event)
            existing[key] = event
            stats["new"] += 1
        else:
            changed = {k: v for k, v in fields.items() if getattr(event, k) != v and not (k == "actual" and v is None)}
            for k, v in changed.items():
                setattr(event, k, v)
            if changed:
                event.updated_at = now
                stats["updated"] += 1
        if event.actual is not None and event.released_at is None:
            event.released_at, event.consensus_at_release = now, event.consensus      # 凍結公佈時的預期
            stats["released"] += 1
        touched.append(event)
    session.flush()
    stats["observations"] = _write_surprises(session, cfg, touched)
    return stats


def _write_surprises(session: Session, cfg: Config, events: list[CalendarEvent]) -> int:
    observations = []
    for event in events:
        value = surprise(event) if event.released_at else None
        if value is None:
            continue
        for rule in cfg.jin10.calendar_rules:
            if rule.match in event.title and (rule.require is None or rule.require in event.title):
                day = event.pub_time.replace(tzinfo=ZoneInfo("UTC")).astimezone(BEIJING).date()
                observations.append(Obs(rule.series, day, value * rule.scale, f"jin10:calendar:{event.key[:12]}"))
                break
    if observations:
        write_observations(session, observations, SOURCE, actor="jin10")
    return len(observations)


def calendar_view(session: Session, days_back: int = 7, days_ahead: int = 21, min_star: int = 0) -> list[dict]:
    """頁面用：最近 days_back 天到之後 days_ahead 天的日曆，附超預期。"""
    now = utcnow()
    start, end = now - timedelta(days=days_back), now + timedelta(days=days_ahead)
    rows = session.scalars(select(CalendarEvent).where(CalendarEvent.pub_time >= start, CalendarEvent.pub_time <= end)
                           .order_by(CalendarEvent.pub_time))
    out = []
    for e in rows:
        if (e.star or 0) < min_star:
            continue
        out.append({"id": e.id, "pub_time": e.pub_time.isoformat(timespec="minutes") + "Z", "title": e.title,
                    "star": e.star, "previous": e.previous, "consensus": e.consensus_at_release or e.consensus,
                    "actual": e.actual, "revised": e.revised, "affect": e.affect, "surprise": surprise(e),
                    "released": e.released_at is not None, "upcoming": e.pub_time > now})
    return out



# ------------------------------ 快訊 ------------------------------

_TAG = re.compile(r"<[^>]+>")
FLASH_KEEP_DAYS = 90


def flash_text(item: dict) -> str:
    """快訊條目裡的正文：依次試 content / title / introduction（也試套在 data 裡的），去掉 HTML 標籤。"""
    sources = [item, item.get("data") if isinstance(item.get("data"), dict) else {}]
    for src in sources:
        for key in ("content", "title", "introduction", "text"):
            value = src.get(key)
            if isinstance(value, str) and value.strip():
                return _TAG.sub("", value).strip()
    return ""


def sync_flash(session: Session, cfg: Config, jin: Jin10) -> dict:
    """同步最新快訊，只收錄命中關鍵詞的。從最新一頁往後翻，遇到已經收錄過的就停（最多 flash_pages 頁）。"""
    from .models import NewsFlash
    keywords = [k.lower() for k in cfg.jin10.flash_keywords]
    known = set(session.scalars(select(NewsFlash.ext_id).where(NewsFlash.source == SOURCE)))   # 以前收錄過的
    seen_now: set[str] = set()                                     # 這一輪看過的（同一條可能出現在相鄰兩頁）
    stats = {"seen": 0, "kept": 0, "pages": 0}
    cursor, new_rows = None, []
    for _ in range(cfg.jin10.flash_pages):
        content = jin.call("list_flash", **({"cursor": cursor} if cursor else {}))
        stats["pages"] += 1
        rows = rows_of(content)
        reached_known = False
        for item in rows:
            stats["seen"] += 1
            text = flash_text(item)
            when = parse_time(item.get("time") or item.get("pub_time") or item.get("create_time"))
            if not text or when is None:
                continue
            ext_id = str(item.get("id") or hashlib.sha256(f"{when}|{text}".encode()).hexdigest()[:40])[:64]
            if ext_id in known:                 # 翻到上次收錄過的地方：之後都是舊的
                reached_known = True
                continue
            if ext_id in seen_now:
                continue
            seen_now.add(ext_id)
            hit = [k for k in cfg.jin10.flash_keywords if k.lower() in text.lower()]
            if not hit or not keywords:
                continue
            new_rows.append(NewsFlash(source=SOURCE, ext_id=ext_id, time=when, content=text[:4000],
                                      url=_clip(item.get("url"), 500), keywords=hit,
                                      important=bool(item.get("important") or (item.get("star") or 0) >= 3)))
        data = content.get("data", {}) if isinstance(content, dict) else {}
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if reached_known or not cursor or not (isinstance(data, dict) and data.get("has_more")):
            break
    session.add_all(new_rows)
    stats["kept"] = len(new_rows)
    return stats


def prune_flash(session: Session, now: datetime | None = None) -> int:
    from sqlalchemy import delete

    from .models import NewsFlash
    cutoff = (now or utcnow()) - timedelta(days=FLASH_KEEP_DAYS)
    return session.execute(delete(NewsFlash).where(NewsFlash.time < cutoff)).rowcount or 0
