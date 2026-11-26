"""撤銷導入：新加的刪掉、覆蓋的恢復、按期保存的還原；留記錄，按時點仍看得到撤銷前；後進先出。"""
from datetime import date, datetime

import pytest
from sqlalchemy import func, select

from crudelab.import_revert import revert_batch
from crudelab.imports import ImportFlowError, commit_batch, preview_file
from crudelab.ingest import load_points, to_series
from crudelab.models import AuditLog, ImportBatch, Observation, ObservationVintage, utcnow


def load(session, settings, lines: list[str], name: str, source="reuters_xls", vintage=None) -> int:
    preview = preview_file(session, settings, "\n".join(lines).encode(), name, source, vintage=vintage)
    return commit_batch(session, preview["batch_id"])["batch_id"]


def values(session, code, as_of=None):
    return {str(d.date()): v for d, v in to_series(load_points(session, [code], as_of=as_of), code).items()}


def test_revert_removes_what_the_batch_added_and_restores_what_it_overwrote(session, settings):
    first = load(session, settings, ["Date,BRENT_M1", "2026-09-01,70", "2026-09-02,71", "2026-09-03,72"], "a.csv")
    wrong = load(session, settings, ["Date,BRENT_M1", "2026-09-02,710", "2026-09-03,72", "2026-09-04,730"], "b.csv")
    session.commit()
    assert values(session, "BRENT_M1") == {"2026-09-01": 70, "2026-09-02": 710, "2026-09-03": 72, "2026-09-04": 730}
    during = utcnow()

    plan = revert_batch(session, wrong, "analyst", dry_run=True)
    assert (plan["removed"], plan["restored"], plan["series"]) == (1, 1, ["BRENT_M1"])      # 9-03 沒變，不算
    assert values(session, "BRENT_M1")["2026-09-02"] == 710                                # dry_run 不改
    done = revert_batch(session, wrong, "analyst")
    session.commit()
    assert values(session, "BRENT_M1") == {"2026-09-01": 70, "2026-09-02": 71, "2026-09-03": 72}
    assert values(session, "BRENT_M1", as_of=during) == {"2026-09-01": 70, "2026-09-02": 710, "2026-09-03": 72,
                                                         "2026-09-04": 730}               # 撤銷之前看到的
    batch = session.get(ImportBatch, wrong)
    assert batch.status == "reverted" and batch.reverted_by == "analyst" and done["removed"] == 1
    assert session.scalars(select(AuditLog).where(AuditLog.action == "revert_import")).one().entity_id == str(wrong)
    with pytest.raises(ImportFlowError, match="已經在 .* 撤銷過"):
        revert_batch(session, wrong)
    revert_batch(session, first)                                                           # 再撤第一批：全空
    assert session.scalar(select(func.count()).select_from(Observation)) == 0


def test_a_batch_changed_later_can_only_be_reverted_after_the_later_one(session, settings):
    a = load(session, settings, ["Date,BRENT_M1", "2026-09-01,70", "2026-09-02,71"], "a.csv")
    b = load(session, settings, ["Date,BRENT_M1", "2026-09-02,75", "2026-09-03,76"], "b.csv")
    with pytest.raises(ImportFlowError, match=rf"之後又被改過：#{b}（b.csv）.*先撤銷之後的那一批"):
        revert_batch(session, a)
    revert_batch(session, b)
    revert_batch(session, a)
    assert values(session, "BRENT_M1") == {}
    with pytest.raises(ImportFlowError, match="找不到導入批次"):
        revert_batch(session, 9999)


def test_vintaged_series_go_back_to_the_previous_report(session, settings):
    """按期保存的序列（OPEC 月報產量）：撤銷下一期，回到上一期的數；兩期都撤，什麼都不剩。"""
    header = "Date,CRUDE_PROD_SAUDI"
    oct_report = load(session, settings, [header, "2026-08-01,9650", "2026-09-01,9950", "2026-10-01,9980"],
                      "oct.csv", vintage=date(2026, 11, 12))
    nov_report = load(session, settings, [header, "2026-09-01,9950", "2026-10-01,9940", "2026-11-01,10010"],
                      "nov.csv", vintage=date(2026, 11, 20))
    assert values(session, "CRUDE_PROD_SAUDI")["2026-10-01"] == 9940
    revert_batch(session, nov_report)
    assert values(session, "CRUDE_PROD_SAUDI") == {"2026-08-01": 9650, "2026-09-01": 9950, "2026-10-01": 9980}
    left = session.scalars(select(ObservationVintage.vintage).distinct()).all()
    assert left == [date(2026, 11, 12)]
    revert_batch(session, oct_report)
    assert session.scalar(select(func.count()).select_from(ObservationVintage)) == 0


def test_extended_confirmations_are_undone_too(session, settings):
    """每天導入（不帶報告期）數字沒變時，只延長上一期的確認日期；撤銷那一批要把確認日期改回去。"""
    from crudelab.ingest import Obs, write_observations
    day1 = datetime(2026, 11, 20, 9)
    write_observations(session, [Obs("CRUDE_PROD_SAUDI", date(2026, 10, 1), 9980.0)], "reuters_xls", now=day1)
    session.commit()
    again = load(session, settings, ["Date,CRUDE_PROD_SAUDI", "2026-10-01,9980", "2026-09-01,9950"], "again.csv")
    row = session.scalars(select(ObservationVintage).where(ObservationVintage.obs_date == date(2026, 10, 1))).one()
    assert row.confirmed_at == date.today()
    revert_batch(session, again)
    session.expire_all()
    row = session.scalars(select(ObservationVintage).where(ObservationVintage.obs_date == date(2026, 10, 1))).one()
    assert row.confirmed_at is None and row.vintage == day1.date()
    assert values(session, "CRUDE_PROD_SAUDI") == {"2026-10-01": 9980}


def test_batches_from_before_the_undo_log_with_vintages_are_refused(session, settings):
    b = load(session, settings, ["Date,CRUDE_PROD_SAUDI", "2026-09-01,9950", "2026-10-01,9980"], "old.csv",
             vintage=date(2026, 11, 12))
    session.get(ImportBatch, b).undo = None                       # 模擬之前的版本導入的批次
    with pytest.raises(ImportFlowError, match="沒有撤銷記錄"):
        revert_batch(session, b)


def test_admin_api(sites, session, settings):
    b = load(session, settings, ["Date,BRENT_M1", "2026-09-01,70", "2026-09-02,71"], "a.csv")
    session.commit()
    admin = sites["admin"]
    plan = admin.post(f"/api/imports/{b}/revert?dry_run=true").json()
    assert plan["dry_run"] and plan["removed"] == 2
    assert admin.post(f"/api/imports/{b}/revert").json()["removed"] == 2
    assert admin.post(f"/api/imports/{b}/revert").status_code == 400
    listed = {r["id"]: r for r in admin.get("/api/imports").json()}
    assert listed[b]["status"] == "reverted" and listed[b]["reverted_at"]


def test_command_line_lists_and_reverts(tmp_path, capsys, monkeypatch):
    from crudelab.cli import main
    path = tmp_path / "a.csv"
    path.write_text("Date,BRENT_M1\n2026-09-01,70\n2026-09-02,71\n", encoding="utf-8")
    assert main(["import", str(path), "--source", "reuters_xls"]) == 0
    capsys.readouterr()
    assert main(["revert-import"]) == 0 and "a.csv" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert main(["revert-import", "1"]) == 1 and "沒有撤銷" in capsys.readouterr().out
    assert main(["revert-import", "1", "--yes"]) == 0
    assert "已撤銷批次 #1：刪掉 2 個點" in capsys.readouterr().out
    assert main(["revert-import", "1", "--yes"]) == 1

