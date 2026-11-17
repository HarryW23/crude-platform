"""數據斷更提醒。"""
import shutil
from datetime import date, datetime, timedelta

import httpx
import pytest
import yaml

from crudelab import db, freshness, jobs, notifications, scheduler
from crudelab.config import ConfigError, load_config
from crudelab.ingest import Obs, write_observations
from crudelab.models import Observation
from crudelab.schema import FreshnessRule
from crudelab.settings import PROJECT_ROOT

TODAY = date(2026, 11, 18)                               # 周三
NOW = datetime(2026, 11, 18, 1, 15)                      # 上海 09:15 = UTC 01:15


def put(session, code, day, value=1.0, received=None):
    write_observations(session, [Obs(code, day, value)], "manual", now=received or NOW - timedelta(hours=1))
    session.commit()


def rows(session, cfg, now=NOW):
    return {r["code"]: r for r in freshness.check(session, cfg, now)["rows"]}


def test_lag_counting():
    weekdays, calendar = FreshnessRule(max_days=3, count="weekdays"), FreshnessRule(max_days=14)
    friday = date(2026, 11, 13)
    assert [freshness.lag(weekdays, friday, friday + timedelta(days=d)) for d in range(0, 6)] == [0, 0, 0, 1, 2, 3]
    assert freshness.lag(weekdays, friday, date(2026, 11, 19)) == 4                  # 周四：超過 3 個工作日
    assert freshness.lag(calendar, friday, TODAY) == 5 and freshness.lag(calendar, TODAY, friday) == 0


def test_daily_weekly_and_monthly_rules(session, cfg):
    put(session, "BRENT_M1", date(2026, 11, 13))          # 上周五：到周三是 3 個工作日，還在期限內
    put(session, "WTI_M1", date(2026, 11, 12))            # 上周四：4 個工作日 → 斷更
    put(session, "EIA_CRUDE_STOCKS", date(2026, 11, 6))   # 周度：12 天，在 14 天內
    put(session, "EIA_CUSHING_STOCKS", date(2026, 10, 30))  # 19 天 → 斷更
    # 月度帶預測期：最新日期在未來，看的是收到的時間
    put(session, "STEO_OPEC_SUPPLY", date(2027, 6, 1), received=NOW - timedelta(days=10))
    put(session, "STEO_OECD_DEMAND", date(2027, 6, 1), received=NOW - timedelta(days=50))
    # 官價：override 用 received
    put(session, "ARAB_LIGHT_OSP_ASIA", date(2026, 11, 1), received=NOW - timedelta(days=30))
    r = rows(session, cfg)
    assert r["BRENT_M1"]["status"] == "fresh" and r["BRENT_M1"]["lag"] == 3
    assert r["WTI_M1"]["status"] == "stale" and r["WTI_M1"]["lag"] == 4
    assert r["EIA_CRUDE_STOCKS"]["status"] == "fresh" and r["EIA_CUSHING_STOCKS"]["status"] == "stale"
    assert r["STEO_OPEC_SUPPLY"]["status"] == "fresh" and r["STEO_OPEC_SUPPLY"]["last"] == "2026-11-08"
    assert r["STEO_OECD_DEMAND"]["status"] == "stale" and r["STEO_OECD_DEMAND"]["lag"] == 50
    assert r["ARAB_LIGHT_OSP_ASIA"]["basis"] == "received" and r["ARAB_LIGHT_OSP_ASIA"]["status"] == "fresh"
    assert "SC_M1" not in r                                # 從沒導入過、也沒要求的序列：不打擾
    result = freshness.check(session, cfg, NOW)
    assert (result["stale"], result["fresh"], result["problems"]) == (3, 4, 3)
    worst_first = ["STEO_OECD_DEMAND", "EIA_CUSHING_STOCKS", "WTI_M1"]
    assert [x["code"] for x in result["rows"][:3]] == worst_first
    assert freshness.summary_line(r["WTI_M1"]) == "WTI 期貨 M1：最新數據 2026-11-12，已 4 個工作日（期限 3）"


def test_required_ignore_overrides_and_validation(session, cfg, tmp_path):
    target = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", target)
    path = target / "freshness.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    strict = {"series": "BRENT_M1", "max_days": 1, "count": "weekdays"}
    doc.update(required=["SC_M1"], ignore=["WTI_M1"], overrides=[strict])
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    custom = load_config(target)
    put(session, "BRENT_M1", date(2026, 11, 13))
    put(session, "WTI_M1", date(2020, 1, 1))
    r = rows(session, custom)
    assert r["SC_M1"]["status"] == "never" and r["SC_M1"]["required"]
    assert r["WTI_M1"]["status"] == "ignored" and r["BRENT_M1"]["status"] == "stale"
    assert freshness.summary_line(r["SC_M1"]) == "上海原油期貨 M1：從沒有過數據"

    doc.update(required=["NOPE"], ignore=["ALSO_NOPE"], overrides=[])
    doc["defaults"].pop("weekly")
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(target)
    assert "required：NOPE 不是序列" in str(exc.value) and "ignore：ALSO_NOPE" in str(exc.value)
    assert "defaults 缺少：weekly" in str(exc.value)


def test_check_job_notifies_with_its_own_wording(session, cfg, settings, monkeypatch):
    sent = []
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: sent.append(json) or httpx.Response(
        200, json={"errcode": 0}, request=httpx.Request("POST", url)))
    monkeypatch.setenv("NOTIFY_WECOM_WEBHOOK", "https://example.com/hook")
    job = jobs.find_job(cfg, "CHECK_FRESHNESS")

    def run(at):
        run_id = jobs.claim(job.code, at, "schedule")
        result = jobs.execute(cfg, settings, job, run_id)
        return result, notifications.after_run(cfg, job, run_id)

    monkeypatch.setattr(freshness, "today_in", lambda cfg, now=None: TODAY)
    assert run(NOW)[0]["status"] == "skipped"              # 沒有任何數據：無從檢查
    put(session, "WTI_M1", date(2026, 11, 12))
    result, event = run(NOW + timedelta(days=1))
    assert result["status"] == "failed" and event == "failure"
    assert "1 條序列超過期限沒有更新" in result["message"] and "WTI 期貨 M1" in result["message"]
    assert sent[0]["text"]["content"].startswith("【crudelab】數據斷更\n1 條序列")
    put(session, "WTI_M1", date(2026, 11, 18))
    result, event = run(NOW + timedelta(days=2))
    assert result["status"] == "ok" and event == "recovery"
    assert sent[-1]["text"]["content"].startswith("【crudelab】數據已恢復更新")


def test_long_lists_are_truncated(session, cfg, settings, monkeypatch):
    monkeypatch.setattr(freshness, "today_in", lambda cfg, now=None: TODAY)
    for code in [c for c, s in cfg.series.items() if s.frequency == "daily"][:12]:
        put(session, code, date(2026, 10, 1))
    result = jobs.run_now(cfg, settings, "CHECK_FRESHNESS")
    assert result["message"].count("\n") == 1 + jobs.MAX_LISTED and "另有 4 條" in result["message"]


def test_scheduled_and_visible_on_both_sites(sites, session, cfg, settings):
    daily = next(j for j in scheduler.status(cfg)["jobs"] if j["code"] == "CHECK_FRESHNESS")
    assert daily["schedule"] == "周一至周五 09:15" and daily["enabled"]
    put(session, "WTI_M1", date(2020, 1, 2))
    for client in sites.values():
        body = client.get("/api/freshness").json()
        assert body["problems"] == 1 and body["rows"][0]["code"] == "WTI_M1"
    with db.session_scope() as s:
        assert s.query(Observation).count() == 1
