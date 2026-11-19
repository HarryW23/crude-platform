"""煉廠開工率與檢修（#24）；國內資訊商的簡體表頭能自動認出。"""
from datetime import date, timedelta

import pytest

from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations


def weekly(start: date, n: int) -> list[date]:
    return [start + timedelta(weeks=i) for i in range(n)]


def test_us_offline_capacity_and_utilisation_vs_five_years(session, cfg):
    days = weekly(date(2020, 1, 3), 6 * 52 + 1)                   # 周五，6 年
    obs = []
    for d in days:
        util = 90.0 if d.year < 2025 else 93.0                    # 最近一年開工比往年高 3 個百分點
        obs += [Obs("EIA_REFINERY_UTIL", d, util), Obs("EIA_REFINERY_CAPACITY", d, 18_000.0),
                Obs("EIA_REFINERY_GROSS_INPUT", d, 18_000.0 * util / 100)]
    write_observations(session, obs, "manual")
    session.commit()
    engine = IndicatorEngine(session, cfg)
    offline = engine.compute("RU_US_OFFLINE")
    assert offline.iloc[0] == pytest.approx(1800.0) and offline.iloc[-1] == pytest.approx(1260.0)
    vs5 = engine.compute("RU_US_UTIL_VS_5Y")
    assert vs5.iloc[-1] == pytest.approx(3.0)                     # 2025 年和前 5 年同期比
    assert engine.compute("RU_US_OFFLINE_VS_5Y").iloc[-1] == pytest.approx(-540.0)
    assert "SIG_US_REFINERY_RUNS" in cfg.signals and cfg.signals["SIG_US_REFINERY_RUNS"].module == "refinery"


def test_vendor_headers_in_simplified_chinese_are_recognised(session, cfg):
    from crudelab.imports import make_resolver
    resolve = make_resolver(session)
    assert resolve("山东地炼开工率") == "CN_SD_TEAPOT_UTIL"            # 隆眾等導出的簡體表頭
    assert resolve("山東地煉開工率") == "CN_SD_TEAPOT_UTIL"            # 配置裡的繁體
    assert resolve("山东汽油92") == "SD_GASOLINE_92"                    # 原有序列也受益
    assert resolve("主营炼厂开工率") == "CN_MAJOR_REFINERY_UTIL"


def test_china_runs_use_the_china_calendar(cfg):
    assert cfg.calendar_of("CN_SD_TEAPOT_UTIL").code == "CN"


def test_alias_migration_renormalises_stored_aliases(tmp_path):
    """0017：已經存著的別名按「繁轉簡」重算；撞在一起的，留配置裡登記的那條。"""
    import sqlalchemy as sa
    from alembic import command

    from crudelab import db
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    alembic = db.alembic_config()
    with engine.begin() as conn:
        alembic.attributes["connection"] = conn
        command.upgrade(alembic, "0016")
        conn.execute(sa.text("INSERT INTO unit (code, name_zh, dimension) VALUES ('pct', '百分點', 'ratio')"))
        conn.execute(sa.text("INSERT INTO commodity (code, name_zh, category) VALUES ('X', 'x', 'fundamental')"))
        conn.execute(sa.text("INSERT INTO location (code, name_zh, region) VALUES ('X', 'x', 'x')"))
        for code in ("A", "B"):
            conn.execute(sa.text(
                "INSERT INTO series (code, name_zh, commodity_code, location_code, kind, unit_code, frequency, "
                "valid_days, module, external_ids, active, within_period) "
                "VALUES (:c, :c, 'X', 'X', 'ratio', 'pct', 'weekly', 7, 'refinery', '{}', 1, 0)"), {"c": code})
        rows = [("山東開工率", "山東開工率", "A", "config"),          # 舊規則下繁簡是兩條不同的別名
                ("山东开工率", "山东开工率", "B", "learned"),          # 撞上之後：留配置那條
                ("華東開工", "華東開工", "B", "learned")]               # 只是重算
        for alias, norm, code, origin in rows:
            conn.execute(sa.text("INSERT INTO series_alias (alias_norm, alias, series_code, origin) "
                                 "VALUES (:n, :a, :c, :o)"), {"n": norm, "a": alias, "c": code, "o": origin})
    db.migrate(engine)
    with engine.connect() as conn:
        got = conn.execute(sa.text("SELECT alias_norm, series_code, origin FROM series_alias ORDER BY id")).all()
    assert [tuple(r) for r in got] == [("山东开工率", "A", "config"), ("华东开工", "B", "learned")]
