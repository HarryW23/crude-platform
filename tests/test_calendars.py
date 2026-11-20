"""交易所節假日日曆（#41）：長假不誤報斷更、換月最後交易日避開假期。"""
from datetime import date

import pytest

from crudelab import calendars, contracts, freshness
from crudelab.config import calendar_warnings
from crudelab.schema import CalendarDef, FreshnessRule, HolidayRule


def test_cme_rules_give_the_2026_settlement_holidays(cfg):
    got = sorted(d for d in cfg.calendars["CME"].holidays if d.year == 2026)
    assert got == [date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
                   date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)]
    assert date(2021, 6, 18) not in cfg.calendars["CME"].holidays            # 六月節 2022 年起才休市
    assert calendars._easter(2027) == date(2027, 3, 28) and calendars._easter(2026) == date(2026, 4, 5)


def test_uk_christmas_and_boxing_day_both_roll_past_the_weekend(cfg):
    ice = cfg.calendars["ICE_EU"]
    assert {date(2021, 12, 27), date(2021, 12, 28)} <= ice.holidays          # 25 日周六、26 日周日
    assert {date(2022, 12, 26), date(2022, 12, 27)} <= ice.holidays          # 25 日周日
    assert date(2026, 4, 6) not in ice.holidays                              # 復活節星期一 Brent 照常交易


def test_china_long_holiday_does_not_count_as_stale(cfg):
    rule = FreshnessRule(max_days=3, count="weekdays")
    cn = cfg.calendar_of("SD_GASOLINE_92")
    assert cn.code == "CN" and cfg.calendar_of("BRENT_M1").code == "ICE_EU"
    assert cfg.calendar_of("TD3C_WS").code == "WEEKDAYS"
    last, today = date(2026, 2, 13), date(2026, 2, 24)                      # 春節前最後一個交易日 → 節後第一天
    assert freshness.lag(rule, last, today) == 7                             # 只按周一到周五：會誤報
    assert freshness.lag(rule, last, today, cn) == 1                         # 按中國日曆：只隔 1 個交易日


def test_freshness_check_uses_each_series_calendar(session, cfg):
    from datetime import datetime

    from crudelab.ingest import Obs, write_observations
    write_observations(session, [Obs("SD_GASOLINE_92", date(2026, 9, 30), 8000.0),
                                 Obs("SG_GASOLINE_92", date(2026, 9, 30), 80.0)], "manual")
    session.commit()
    result = freshness.check(session, cfg, now=datetime(2026, 10, 8, 4, 0))   # 國慶後第一天
    rows = {r["code"]: r for r in result["rows"]}
    assert rows["SD_GASOLINE_92"]["status"] == "fresh" and rows["SD_GASOLINE_92"]["lag"] == 1
    assert rows["SG_GASOLINE_92"]["status"] == "stale"                       # 新加坡沒有國慶假期


def test_wti_last_trading_day_skips_thanksgiving(cfg):
    rule = cfg.curves["WTI"].roll
    # 2027-11-25 是感恩節：取之前的工作日 11/24，再往前 3 個工作日 = 11/19（只按周一到周五會算成 11/22）
    assert contracts.last_trading_day(rule, contracts.ordinal(2027, 12)) == date(2027, 11, 19)
    assert contracts.last_trading_day(rule, contracts.ordinal(2026, 12)) == date(2026, 11, 20)
    sc = cfg.curves["SC"].roll                                               # INE：交割月前一個月最後一個交易日
    assert contracts.last_trading_day(sc, contracts.ordinal(2026, 11)) == date(2026, 10, 30)
    assert contracts.last_trading_day(sc, contracts.ordinal(2027, 3)) == date(2027, 2, 26)


def test_registered_years_and_warnings(cfg):
    assert calendar_warnings(cfg, date(2027, 6, 1)) == []
    assert "2028" in calendar_warnings(cfg, date(2027, 12, 5))[0]           # 12 月了還沒有明年的
    assert "2028" in calendar_warnings(cfg, date(2028, 3, 1))[0]
    with pytest.raises(ValueError, match="years"):
        CalendarDef(code="X", name_zh="x", dates=[date(2030, 1, 2)], years=[2029])
    with pytest.raises(ValueError, match="一種"):
        HolidayRule(name="壞", month=1, day=1, weekday=0, nth=1)


def test_unknown_calendar_is_a_config_error(cfg):
    import dataclasses

    from crudelab.config import _validate
    series = dict(cfg.series)
    series["TD3C_WS"] = series["TD3C_WS"].model_copy(update={"calendar": "NOPE"})
    assert any("交易日曆 NOPE" in e for e in _validate(dataclasses.replace(cfg, series=series)))


# ------------------------------ 逐條審查類型問題時查出的三個問題 ------------------------------

def test_holiday_on_feb_29_only_in_leap_years():
    from crudelab.calendars import rule_dates
    from crudelab.schema import HolidayRule
    rule = HolidayRule(name="閏日", month=2, day=29)
    assert rule_dates(rule, 2028) == [date(2028, 2, 29)] and rule_dates(rule, 2027) == []   # 以前平年直接崩潰


def test_impossible_fixed_date_is_a_config_error():
    from crudelab.schema import HolidayRule
    with pytest.raises(ValueError, match="4 月沒有 31 日"):
        HolidayRule(name="打錯", month=4, day=31)


def test_fifth_weekday_does_not_slide_into_next_month():
    from crudelab.calendars import rule_dates
    from crudelab.schema import HolidayRule
    rule = HolidayRule(name="第五個周一", month=2, weekday=0, nth=5)
    assert rule_dates(rule, 2027) == []                       # 2027 年 2 月只有 4 個周一：以前會變成 3 月 1 日
    assert rule_dates(rule, 2016) == [date(2016, 2, 29)]      # 有第 5 個的年份照常
