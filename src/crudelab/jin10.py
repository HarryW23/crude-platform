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
    """金十的時間 → 不帶時區的 UTC。三種寫法：
      - 不帶時區的文本（例如 2026-11-19 23:30:00）：按北京時間
      - 帶時區的 ISO 文本（例如 2026-11-19T11:21:25+08:00）：按它寫的時區
      - 數字（K 線的 time）：Unix 時間戳，秒；大於 1e11 的按毫秒
    """
    if text is None or text == "" or isinstance(text, bool):
        return None
    utc = ZoneInfo("UTC")
    if isinstance(text, (int, float)) or (isinstance(text, str) and text.strip().isdigit()):
        stamp = float(text)
        if stamp > 1e11:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(text).strip().replace("/", "-")
    if _TZ_SUFFIX.search(raw):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(utc).replace(tzinfo=None)
        except ValueError:
            return None
    raw = raw.replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            local = datetime.strptime(raw, fmt).replace(tzinfo=BEIJING)
        except ValueError:
            continue
        return local.astimezone(utc).replace(tzinfo=None)
    return None


_TZ_SUFFIX = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")
NEW_YORK = ZoneInfo("America/New_York")


def trade_date(utc_naive: datetime) -> date | None:
    """一個時刻屬於哪個交易日：按紐約日期（外匯、黃金、原油都以紐約下午 5 點收盤，和 EIA、CME 的日期對齊）。
    紐約週六、週日沒有正常交易，回傳 None。"""
    day = utc_naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(NEW_YORK).date()
    return None if day.weekday() >= 5 else day


def beijing_date(utc_naive: datetime) -> date:
    return utc_naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(BEIJING).date()


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
                day = beijing_date(event.pub_time)
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


def flash_id(item: dict, when: datetime, text: str) -> str:
    """快訊的唯一標識。金十的 list_flash 每條只有 content / time / url：有 id 用 id，否則用 url（每條快訊一個詳情頁），
    都沒有才用時間 + 內容的摘要（內容被對方改過會被當成新的一條）。"""
    if item.get("id"):
        return str(item["id"])[:64]
    if item.get("url"):
        return "u:" + hashlib.sha256(str(item["url"]).strip().encode()).hexdigest()[:40]
    return hashlib.sha256(f"{when}|{text}".encode()).hexdigest()[:40]


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
            ext_id = flash_id(item, when, text)
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


# ------------------------------ 每日收盤價（報價和回填共用） ------------------------------
#
# 金十 get_kline 只給 1 分鐘 K 線；time 參數是 Unix 秒，回傳「截止到這個時刻」的最後 count 根（probe 實測）。
# 所以每個品種、每個交易日只要一次調用就能取到收盤價。口徑統一為：
#     紐約時間 16:59 開始的那根 1 分鐘 K 線的收盤價（= 紐約下午 5 點的價格）
# 外匯、黃金以紐約下午 5 點換日；原油期貨 17:00–18:00 休市，這個時刻也正好在休市前。
# 每天定時拉取和歷史回填都用這個定義，所以幾點運行都不影響數值，歷史和每日數據是同一個口徑。

CLOSE_AT = (16, 59)                   # 紐約時間
CLOSE_MAX_STALE_HOURS = 6             # 取到的 K 線比收盤時刻早超過 6 小時 = 那天沒有正常交易（假期），不記


def close_stamp(day: date) -> int:
    """day 這個紐約交易日的收盤時刻（Unix 秒）。夏令時由時區庫處理。"""
    return int(datetime(day.year, day.month, day.day, *CLOSE_AT, tzinfo=NEW_YORK).timestamp())


def completed_trade_days(now_utc: datetime, n: int) -> list[date]:
    """到 now 為止最近 n 個已經收盤的紐約工作日，由舊到新。"""
    day = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(NEW_YORK).date()
    out: list[date] = []
    while len(out) < n:
        if day.weekday() < 5 and close_stamp(day) + 60 <= now_utc.replace(tzinfo=ZoneInfo("UTC")).timestamp():
            out.append(day)
        day -= timedelta(days=1)
    return out[::-1]


def close_on(jin: Jin10, code: str, day: date) -> tuple[float, datetime] | None:
    """某品種某個紐約交易日的收盤價和那根 K 線的時間（UTC）。那天沒有交易（假期）回傳 None。"""
    stamp = close_stamp(day)
    data = jin.call("get_kline", code=code, time=stamp, count=3)
    payload = data.get("data", data) if isinstance(data, dict) else {}
    bars = []
    for b in payload.get("klines") or []:
        if isinstance(b, dict):
            when, value = parse_time(b.get("time")), number(b.get("close"))
            if when is not None and value is not None:
                bars.append((when, value))
    limit = datetime.fromtimestamp(stamp, ZoneInfo("UTC")).replace(tzinfo=None)
    usable = [b for b in bars if b[0] <= limit]
    if not usable:
        return None
    when, value = max(usable)
    if trade_date(when) != day or (limit - when).total_seconds() > CLOSE_MAX_STALE_HOURS * 3600:
        return None
    return value, when


def quote_series(session: Session) -> list:
    from .models import Series
    return [s for s in session.scalars(select(Series).where(Series.active).order_by(Series.code))
            if (s.external_ids or {}).get("jin10")]


def backfill_closes(session: Session, cfg: Config, jin: Jin10, days: int, now: datetime | None = None) -> dict:
    """給登記了 jin10 代碼的序列補最近 days 個交易日的收盤價。每品種每天一次 get_kline。
    調用前先算總次數：超過今天剩下的額度就不開始，並說明可以補多少天。
    和 sync_calendar 一樣：調用前 session 不能有未提交的寫入。"""
    series = quote_series(session)
    trade_days = completed_trade_days(now or utcnow(), days)
    need = len(series) * len(trade_days)
    left = cfg.jin10.max_calls_per_tool_per_day - used_today("get_kline")
    if need > left:
        raise Jin10LimitReached(f"要調用 get_kline {need} 次（{len(series)} 個品種 × {len(trade_days)} 天），"
                                f"今天只剩 {left} 次；最多可以補 {max(left // max(len(series), 1), 0)} 天，或明天再補")
    # 先把所有調用做完、再一次寫入：調用記錄走另一個會話，這個會話在調用期間不能有未提交的寫入（SQLite 會鎖）
    rows, collected = [], []
    for s in series:
        code = s.external_ids["jin10"]
        obs, holidays = [], 0
        for day in trade_days:
            got = close_on(jin, code, day)
            if got is None:
                holidays += 1
                continue
            obs.append(Obs(s.code, day, got[0], f"jin10:close:{code}"))
        collected.append(obs)
        rows.append({"series": s.code, "code": code, "days": len(trade_days), "closes": len(obs),
                     "no_trading": holidays, "written": 0})
    for row, obs in zip(rows, collected, strict=True):
        if obs:
            row["written"] = write_observations(session, obs, SOURCE, actor="jin10")["inserted"]
    return {"calls": need, "first": trade_days[0] if trade_days else None,
            "last": trade_days[-1] if trade_days else None, "rows": rows}
