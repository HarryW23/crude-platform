"""性質測試（hypothesis）：不是挑幾個例子，而是用大量隨機輸入檢查「永遠應該成立」的規律，專找邊界情況的 bug。

失敗時 hypothesis 會把輸入縮到最簡單的反例並打印出來；把它加成一個普通測試再修。
"""
import re
from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from crudelab import contracts, domain, formula, jin10, units
from crudelab.config import load_config
from crudelab.llm.guard import unknown_numbers
from crudelab.settings import PROJECT_ROOT

CFG = load_config(PROJECT_ROOT / "config")
DAYS = st.dates(min_value=date(2005, 1, 1), max_value=date(2035, 12, 31))
FAST = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# ------------------------------ 公式 ------------------------------

@FAST
@given(st.text(max_size=60))
def test_formula_parser_never_crashes_with_an_unexpected_error(text):
    """配置裡寫什麼亂七八糟的公式，都只能得到 FormulaError（給人看的說明），不能是別的異常。"""
    try:
        formula.parse(text)
    except formula.FormulaError:
        pass


@FAST
@given(st.floats(-1e6, 1e6), st.floats(-1e6, 1e6), st.sampled_from(["+", "-", "*", "/"]))
def test_formula_arithmetic_matches_python(a, b, op):
    assume(op != "/" or abs(b) > 1e-9)
    expected = {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else 0}[op]
    assert formula.evaluate(f"A {op} B", {"A": a, "B": b}) == pytest.approx(expected)


@FAST
@given(st.sampled_from(sorted(CFG.indicators)))
def test_every_configured_formula_parses_and_names_resolve(code):
    text = CFG.indicators[code].formula
    formula.parse(text)
    assert all(CFG.has(name) or name in CFG.parameters for name in formula.names(text))


# ------------------------------ 單位 ------------------------------

UNIT_CODES = sorted(CFG.units)


@FAST
@given(st.sampled_from(UNIT_CODES), st.sampled_from(UNIT_CODES))
def test_unit_algebra(a, b):
    da, db = units.parse_unit(a, CFG.atoms), units.parse_unit(b, CFG.atoms)
    assert units.divide(units.multiply(da, db), db) == da
    assert units.multiply(da, db) == units.multiply(db, da)
    assert units.parse_unit(units.format_dim(da), CFG.atoms) == da if da else True


# ------------------------------ 交易日曆與換月 ------------------------------

@FAST
@given(st.sampled_from(sorted(CFG.calendars)), DAYS, st.integers(0, 30))
def test_calendar_counting_is_consistent(code, day, n):
    calendar = CFG.calendars[code]
    business = calendar.prev_business(day)
    assert calendar.is_business(business) and business <= day and (day - business).days < 20
    earlier = calendar.minus_business(business, n)
    assert calendar.is_business(earlier) and calendar.business_days_between(earlier, business) == n


ROLLING = sorted(code for code, curve in CFG.curves.items() if curve.roll is not None)


@FAST
@given(st.sampled_from(ROLLING), DAYS, st.integers(0, 400))
def test_front_month_is_monotonic_and_correct(curve, day, gap):
    rule = CFG.curves[curve].roll
    o = contracts.front_ordinal(rule, day)
    assert contracts.last_trading_day(rule, o) >= day > contracts.last_trading_day(rule, o - 1)
    later = day + timedelta(days=gap)
    assert contracts.front_ordinal(rule, later) >= o                       # 時間往前走，M1 不會倒退
    assert contracts.rolls_between(rule, day, later) == contracts.front_ordinal(rule, later) - o


# ------------------------------ 大模型回答的數字核對 ------------------------------

@FAST
@given(st.lists(st.floats(0.01, 1e6, allow_nan=False).map(lambda v: round(v, 2)), min_size=1, max_size=5))
def test_numbers_taken_from_the_reference_always_pass(values):
    """回答裡照抄參考資料的數字（含千分位、負號寫成全角減號），一律不算編造。"""
    reference = "；".join(f"{v:,.2f}" for v in values)
    answer = "，".join(f"−{v:,.2f}" if i % 2 else f"{v:.2f}" for i, v in enumerate(values))
    assert unknown_numbers(answer, reference) == []


@FAST
@given(st.floats(1000, 1e6).map(lambda v: round(v, 2)))
def test_a_number_missing_from_the_reference_is_flagged(value):
    assume(abs(value - 123.45) > 1)
    assert unknown_numbers(f"價格是 {value:.2f}", "參考：123.45") == [f"{value:.2f}"]


# ------------------------------ 金十時間 ------------------------------

@FAST
@given(st.datetimes(min_value=datetime(2010, 1, 1), max_value=datetime(2035, 1, 1)))
def test_jin10_time_formats_agree(moment):
    moment = moment.replace(microsecond=0)
    utc = moment.replace(tzinfo=UTC)
    epoch = int(utc.timestamp())
    assert jin10.parse_time(epoch) == moment                               # 秒
    assert jin10.parse_time(epoch * 1000) == moment                        # 毫秒
    assert jin10.parse_time(utc.isoformat()) == moment                     # 帶時區的 ISO
    beijing = (moment + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    assert jin10.parse_time(beijing) == moment                             # 不帶時區：按北京時間


# ------------------------------ 取值約束 ------------------------------

@FAST
@given(st.text(min_size=1, max_size=20))
def test_constraint_builder_only_accepts_plain_words(value):
    """生成 CHECK 約束的值只能是字母、數字、下劃線：任何能拼出 SQL 的字符都拒絕。"""
    if re.fullmatch(r"[A-Za-z0-9_]+", value):
        assert domain.in_list("x", (value,)) == f"x IN ('{value}')"
    else:
        with pytest.raises(ValueError):
            domain.in_list("x", (value,))


# ------------------------------ 文件導入 ------------------------------

CELL = st.one_of(st.just(""), st.text(alphabet="0123456789.,-−% ", max_size=10), st.text(max_size=12),
                 st.dates(date(1990, 1, 1), date(2030, 12, 31)).map(str), st.sampled_from(["#N/A", "n/a", "—", "NaN"]))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow,
                                                                  HealthCheck.function_scoped_fixture])
@given(st.lists(st.lists(CELL, min_size=1, max_size=5), min_size=0, max_size=8),
       st.sampled_from(["Date", "日期", "", "Brent", "LCOc1"]))
def test_any_csv_gives_a_preview_or_a_clear_error(session, settings, rows, first_header):
    """亂七八糟的表格（空格、錯位、文字混數字、各種缺值寫法）：要麼給出預覽，要麼給出 ImportFlowError 說明，
    不能是別的異常（那樣頁面上只會看到「服務器錯誤」）。"""
    from crudelab.imports import ImportFlowError, preview_file
    width = max((len(r) for r in rows), default=1)
    header = [first_header, *(f"列{i}" for i in range(1, width))]
    text = "\n".join(",".join(cells) for cells in [header, *rows])
    try:
        preview = preview_file(session, settings, text.encode("utf-8"), "fuzz.csv", "demo")
    except ImportFlowError:
        session.rollback()
        return
    assert "sheets" in preview
    session.rollback()
