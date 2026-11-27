"""測試回報（0.51.0）的五個問題：清理過的點按時點還原、只讀命令不改庫、單位核對用實際採用的值、
月報沒有合計不導、全部被篩掉的文件不算成功。"""
from datetime import date, datetime, timedelta

import pytest

from crudelab import db
from crudelab.imports import ImportFlowError, commit_batch, nothing_importable, preview_file
from crudelab.ingest import Obs, load_points, to_series, write_observations
from crudelab.master import sync_to_db


def test_points_removed_by_config_are_still_there_as_of_before_the_cleanup(session, cfg, monkeypatch):
    """bad_days 清理之前做的研究，按時點（as_of）回放時要看得到當時的那個點，包括之後被清理掉的。"""
    written = datetime(2026, 7, 17, 9)
    write_observations(session, [Obs("BRENT_M1", date(2026, 7, 15), 70.0), Obs("BRENT_M1", date(2026, 7, 16), 112.0)],
                       "reuters_xls", now=written)
    session.commit()
    bad = cfg.series["BRENT_M1"].model_copy(update={"bad_days": [date(2026, 7, 16)]})
    monkeypatch.setitem(cfg.series, "BRENT_M1", bad)
    assert sync_to_db(session, cfg)["removed_points"] == 1
    session.commit()

    def days(as_of):
        points = to_series(load_points(session, ["BRENT_M1"], as_of=as_of), "BRENT_M1")
        return {str(d.date()): v for d, v in points.items()}

    assert days(None) == {"2026-07-15": 70.0}                                   # 現在：清理掉了
    assert days(written + timedelta(hours=1)) == {"2026-07-15": 70.0, "2026-07-16": 112.0}   # 清理之前：兩筆都在
    assert days(written - timedelta(hours=1)) == {}                              # 寫入之前：都還沒有


def test_unit_check_compares_with_the_value_actually_used(session, settings):
    """同一天有低優先級的演示數據（70,000）時，單位核對仍按平台實際採用的值（路透的 70）比。"""
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(3)]
    write_observations(session, [Obs("BRENT_M1", d, 70.0) for d in days], "reuters_xls")
    write_observations(session, [Obs("BRENT_M1", d, 70_000.0) for d in days], "demo")
    lines = ["Date,BRENT_M1"] + [f"{d},71000" for d in days]
    warnings = preview_file(session, settings, "\n".join(lines).encode(), "r.csv", "reuters_xls")["warnings"]
    assert any(w.startswith("BRENT_M1") and "1,014 倍" in w for w in warnings)


def test_files_whose_points_are_all_screened_out_are_not_a_success(session, settings, cfg):
    lines = ["Date,BRENT_M1", "2026-09-01,0", "2026-09-02,0", "2026-09-03,0"]
    preview = preview_file(session, settings, "\n".join(lines).encode(), "zeros.csv", "reuters_xls")
    assert nothing_importable(preview) == ("文件裡認出的 3 個數據點全部被排除（價格為 0、data_from 之前或 bad_days，"
                                           "見提醒），沒有能入庫的數據")
    with pytest.raises(ImportFlowError, match="全部被排除"):
        commit_batch(session, preview["batch_id"])
    session.rollback()                                       # 放掉寫鎖：收件夾任務用自己的會話

    import os
    import time

    from crudelab import jobs
    inbox = settings.data_dir / "inbox"
    inbox.mkdir(parents=True)
    path = inbox / "zeros.csv"
    path.write_text("\n".join(lines), encoding="utf-8")
    old = time.time() - 120
    os.utime(path, (old, old))
    result = jobs.run_now(cfg, settings, "IMPORT_INBOX")
    assert result["status"] == "failed" and list((inbox / "failed").iterdir())


def test_opec_report_without_any_total_row_is_rejected():
    from crudelab.connectors.momr import MomrError, read_pages
    page = "\n".join(["Table 5 - 7: DoC crude oil production based on secondary sources, tb/d",
                      "Secondary sources Aug 26 Sep 26 Oct 26", "Algeria 961 965 970", "Iraq 3,534 3,995 4,491",
                      "Saudi Arabia 7,257 8,012 9,194", "Notes: Totals may not add up."])
    with pytest.raises(MomrError, match="沒有任何合計行"):
        read_pages([page])


def test_read_only_commands_do_not_upgrade_or_clean_and_init_says_what_it_did(capsys):
    """查統計（coverage）不能順帶升級表結構、按新規則清理數據；備份要原樣備份舊庫；
    init 升級和清理時把做了什麼打印出來。用命令行實際走一遍（數據目錄是 conftest 設的臨時目錄）。"""
    from alembic import command
    from sqlalchemy import create_engine

    from crudelab.cli import main
    from crudelab.settings import get_settings
    assert main(["init"]) == 0
    engine = create_engine(get_settings().database_url)
    with engine.begin() as connection:                        # 回到舊版本（0024 之前沒有 data_from、bad_days）
        alembic = db.alembic_config()
        alembic.attributes["connection"] = connection
        command.downgrade(alembic, "0023")
    capsys.readouterr()

    assert main(["coverage", "eia"]) == 1
    assert "這個命令只讀、不升級：先運行 crudelab init" in capsys.readouterr().err
    assert db.schema_revision(engine) == "0023"

    assert main(["backup"]) == 0                              # 舊庫照樣能備份，備份的就是舊庫
    assert db.schema_revision(engine) == "0023"
    capsys.readouterr()

    assert main(["init"]) == 0
    assert f"表結構已從 0023 升級到 {db.head_revision()}" in capsys.readouterr().out
    assert main(["coverage", "eia"]) == 0
    engine.dispose()


def test_vintaged_series_cannot_use_config_cleanup():
    """清理只動主表，按期保存的每一期還在：按報告期回測會把清理掉的點找回來，所以不允許這樣配。"""
    from pydantic import ValidationError

    from crudelab.schema import SeriesDef
    base = dict(code="X_PROD", name_zh="x", commodity="CRUDE_SUPPLY", location="SAUDI", kind="flow", unit="kb/d",
                frequency="monthly", vintaged=True)
    for extra in ({"data_from": date(2020, 1, 1)}, {"bad_days": [date(2026, 1, 1)]}):
        with pytest.raises(ValidationError, match="按報告期保存的序列不用 bad_days / data_from"):
            SeriesDef(**base, **extra)


def test_daily_current_month_quotes_are_filed_under_the_month(session, settings):
    """「當月」官價這類 RIC 每天給一個數（lseg.period = current）：歸到當月 1 日，一個月一個數；
    沒有這樣標的只在期間內有效的序列，日期不是期間第一天照樣拒收（防止把官價記成公布日）。"""
    from crudelab.models import Series
    session.get(Series, "BASRAH_MEDIUM_OSP_ASIA").external_ids = {"lseg_period": "current"}
    session.flush()
    lines = ["Date,BASRAH_MEDIUM_OSP_ASIA", "2026-10-30,1.10", "2026-10-31,1.10", "2026-11-02,1.35",
             "2026-11-03,1.35", "2026-11-20,1.45"]
    preview = preview_file(session, settings, "\n".join(lines).encode(), "r.csv", "reuters_xls")
    assert any("歸到所屬期間的第一天，共 2 期；2026-11 期間中途數字變過" in w for w in preview["warnings"])
    commit_batch(session, preview["batch_id"])
    points = to_series(load_points(session, ["BASRAH_MEDIUM_OSP_ASIA"]), "BASRAH_MEDIUM_OSP_ASIA")
    assert {str(d.date()): v for d, v in points.items()} == {"2026-10-01": 1.10, "2026-11-01": 1.45}

    plain = ["Date,ARAB_LIGHT_OSP_ASIA", "2026-11-05,1.2", "2026-12-05,1.4"]
    preview = preview_file(session, settings, "\n".join(plain).encode(), "r2.csv", "reuters_xls")
    with pytest.raises(Exception, match="日期 2026-11-05 不對"):
        commit_batch(session, preview["batch_id"])
