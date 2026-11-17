"""通知：定時任務失敗、持續失敗、恢復時，發到群機器人或郵箱。

什麼時候發（只看定時觸發的執行；手動點的當場就看到結果了，不發）：
  failure    這次失敗，而上一次有結果的執行不是失敗 —— 剛出問題
  reminder   連續失敗，且距上次為這個任務發通知已超過 repeat_hours —— 提醒還沒好，但不轟炸
  recovery   這次成功，而上一次有結果的執行是失敗 —— 好了
「跳過」（沒有可做的事）不算有結果，判斷時略過。

安全：
  - 群機器人的地址本身就是密鑰（拿到地址就能往群裡發消息），和 API key 一樣從 credentials 取，不寫進配置。
  - 通知內容發出前過一遍脫敏；發送失敗時只記狀態碼或異常類型，不記地址。
  - 地址必須是 https（本機地址除外，測試用）。
每次通知和各渠道的發送結果記在 notification 表。發送失敗不會影響任務本身，也不會重試。
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import credentials, db, switches
from .config import Config
from .models import JobRun, Notification, utcnow
from .schema import ChannelDef, JobDef

WEBHOOK_CREDENTIALS = {"wecom": "NOTIFY_WECOM_WEBHOOK", "dingtalk": "NOTIFY_DINGTALK_WEBHOOK",
                       "feishu": "NOTIFY_FEISHU_WEBHOOK", "slack": "NOTIFY_SLACK_WEBHOOK",
                       "webhook": "NOTIFY_WEBHOOK_URL"}
TYPE_NAMES = {"wecom": "企業微信群機器人", "dingtalk": "釘釘群機器人", "feishu": "飛書群機器人", "slack": "Slack",
              "webhook": "自定義地址", "email": "郵件"}
PREFIX = "【crudelab】"       # 釘釘機器人如果設了「自定義關鍵詞」安全設置，把關鍵詞設成 crudelab 即可
TIMEOUT = 15.0
log = logging.getLogger("crudelab.notifications")


def credential_name(channel: ChannelDef) -> str:
    return "SMTP_PASSWORD" if channel.type == "email" else WEBHOOK_CREDENTIALS[channel.type]


def is_configured(channel: ChannelDef) -> bool:
    return bool(credentials.get(credential_name(channel)))


# ------------------------------ 發送 ------------------------------

def _payload(kind: str, title: str, body: str, extra: dict) -> dict:
    text = f"{PREFIX}{title}\n{body}"
    if kind in ("wecom", "dingtalk"):
        return {"msgtype": "text", "text": {"content": text}}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if kind == "slack":
        return {"text": text}
    return {"source": "crudelab", "title": title, "text": body, **extra}


def _send_webhook(channel: ChannelDef, title: str, body: str, extra: dict) -> str:
    url = credentials.get(credential_name(channel))
    if not url:
        return "還沒有設定地址"
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("127.0.0.1", "localhost"):
        return "地址必須以 https:// 開頭"
    try:
        response = httpx.post(url, json=_payload(channel.type, title, body, extra), timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return f"發送失敗（{type(exc).__name__}）"          # 不記異常原文：裡面有地址
    if response.status_code != 200:
        return f"對方回報 {response.status_code}"
    try:    # 企業微信 / 釘釘 / 飛書：狀態碼 200 也可能是失敗，要看回覆裡的 errcode / code
        reply = response.json()
        code = reply.get("errcode", reply.get("code", 0)) if isinstance(reply, dict) else 0
    except ValueError:
        code = 0
    return "ok" if code in (0, None) else f"對方拒絕了這條消息（錯誤碼 {code}）"


def _send_email(channel: ChannelDef, title: str, body: str) -> str:
    password = credentials.get("SMTP_PASSWORD")
    if not password:
        return "還沒有設定 SMTP_PASSWORD"
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = PREFIX + title, channel.mail_from, ", ".join(channel.mail_to)
    message.set_content(body)
    context = ssl.create_default_context()
    try:
        if channel.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(channel.smtp_host, channel.smtp_port, timeout=TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(channel.smtp_host, channel.smtp_port, timeout=TIMEOUT)
            server.starttls(context=context)
        with server:
            server.login(channel.smtp_username or channel.mail_from, password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError:
        return "郵箱拒絕登錄：用戶名或密碼（授權碼）不對"
    except (smtplib.SMTPException, OSError) as exc:
        return f"發送失敗（{type(exc).__name__}）"
    return "ok"


def send(cfg: Config, title: str, body: str, *, event: str, job_code: str | None = None,
         only: str | None = None, ignore_switches: bool = False) -> dict[str, str]:
    """發一條通知到所有開著且已配置的渠道，並記錄。回傳 {渠道代碼: "ok" 或原因}。

    only 指定只發某一個渠道；ignore_switches 用於「發送測試」。
    """
    title, body = credentials.redact(title)[:200], credentials.redact(body)[:3000]
    with db.session_scope() as session:
        state = switches.load(session, cfg)
    results: dict[str, str] = {}
    if ignore_switches or state[switches.NOTIFY]:
        for channel in cfg.notifications.channels:
            if only and channel.code != only:
                continue
            if not ignore_switches and not state[switches.channel_key(channel.code)]:
                continue
            if not ignore_switches and not is_configured(channel):
                continue                                    # 沒配置的渠道平時不算失敗，只是不發
            try:
                results[channel.code] = (_send_email(channel, title, body) if channel.type == "email"
                                         else _send_webhook(channel, title, body, {"event": event, "job": job_code}))
            except Exception as exc:                        # 通知出錯不能影響任務
                log.exception("通知渠道 %s 出錯", channel.code)
                results[channel.code] = f"發送失敗（{type(exc).__name__}）"
    with db.session_scope() as session:
        session.add(Notification(job_code=job_code, event=event, title=title, body=body, results=results))
    return results


# ------------------------------ 什麼時候發 ------------------------------

def _previous_outcome(session: Session, job_code: str, before_id: int) -> str | None:
    """這次之前，最近一次有結果（不是跳過、不是執行中）的定時執行。"""
    return session.scalar(select(JobRun.status).where(
        JobRun.job_code == job_code, JobRun.id < before_id, JobRun.trigger == "schedule",
        JobRun.status.in_(("ok", "failed"))).order_by(JobRun.id.desc()).limit(1))


def decide(session: Session, cfg: Config, run: JobRun) -> str | None:
    """這次執行要不要發通知、發哪一種。純判斷，不發送。"""
    rules = cfg.notifications
    if run.trigger != "schedule" or run.status not in ("ok", "failed"):
        return None
    previous = _previous_outcome(session, run.job_code, run.id)
    if run.status == "ok":
        return "recovery" if previous == "failed" and rules.on_recovery else None
    if not rules.on_failure:
        return None
    if previous != "failed":
        return "failure"
    last_sent = session.scalar(select(Notification.created_at).where(
        Notification.job_code == run.job_code, Notification.event.in_(("failure", "reminder")))
        .order_by(Notification.id.desc()).limit(1))
    if last_sent is None or utcnow() - last_sent >= timedelta(hours=rules.repeat_hours):
        return "reminder"
    return None


def after_run(cfg: Config, job: JobDef, run_id: int) -> str | None:
    """任務執行完後調用：按規則決定是否通知並發出。回傳發了哪一種，沒發回 None。"""
    with db.session_scope() as session:
        run = session.get(JobRun, run_id)
        event = decide(session, cfg, run)
        message = run.message or ""
    if event is None:
        return None
    title = {"failure": f"定時任務失敗：{job.name_zh}", "reminder": f"定時任務仍在失敗：{job.name_zh}",
             "recovery": f"定時任務已恢復：{job.name_zh}"}[event]
    tail = "" if event == "recovery" else "\n到後台的「定時任務」頁查看詳情。"
    send(cfg, title, f"任務：{job.name_zh}（{job.code}）\n結果：{message}{tail}", event=event, job_code=job.code)
    return event


def recent(session: Session, limit: int = 50) -> list[Notification]:
    return list(session.scalars(select(Notification).order_by(Notification.id.desc()).limit(limit)))
