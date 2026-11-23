"""換月規則核對：用路透連續合約每天對應的合約找實際換月日，和配置的規則比較。"""
from datetime import date, timedelta

import pandas as pd
import pytest

from crudelab import contracts, rollcheck
from crudelab.cli import main


def contract_rows(cfg, curve, start, end):
    """按規則生成每個工作日 M1 的（日期, 到期日, 合約月）：和規則完全一致的數據商數據。"""
    rule = cfg.curves[curve].roll
    rows = []
    for ts in pd.bdate_range(start, end):
        o = contracts.front_ordinal(rule, ts.date())
        rows.append((ts.date(), contracts.last_trading_day(rule, o).isoformat(), contracts.contract_label(o)))
    return rows


def test_rule_generated_contracts_are_consistent(cfg):
    rows = contract_rows(cfg, "BRENT", date(2026, 1, 5), date(2026, 9, 30))
    check = rollcheck.check_curve(cfg, "BRENT", rows)
    assert check.consistent and len(check.expected) == len(check.actual) >= 8
    check.verified = False
    assert "可以把這條曲線的 roll.verified 改成 true" in rollcheck.report([check])[1]
    check.verified = True                                    # 已經標了的不再提示
    assert len(rollcheck.report([check])) == 1


def test_late_roll_is_reported_with_override_suggestion(cfg):
    rule = cfg.curves["BRENT"].roll
    rows = contract_rows(cfg, "BRENT", date(2026, 1, 5), date(2026, 9, 30))
    # 2026-05 合約：數據商的實際到期日比規則晚一個交易日 → 換月也晚一天
    label = "2026-05"
    ltd = contracts.last_trading_day(rule, contracts.ordinal(2026, 5))
    real = ltd + timedelta(days=1)
    while real.weekday() >= 5:
        real += timedelta(days=1)
    rows = [(d, real.isoformat() if e == ltd.isoformat() else e, m) for d, e, m in rows]
    rows = [(d, real.isoformat(), label) if d == real else (d, e, m) for d, e, m in rows]
    check = rollcheck.check_curve(cfg, "BRENT", rows)
    assert not check.consistent and len(check.shifted) == 1
    rule_day, actual_day, k = check.shifted[0]
    assert k == 1 and actual_day > rule_day
    assert check.overrides == {label: real}
    text = "\n".join(rollcheck.report([check]))
    assert "晚 1 個交易日" in text and f'"{label}": {real}' in text


def test_offset_curve_only_compares_roll_days(cfg):
    rows = contract_rows(cfg, "DUBAI_SWAP", date(2026, 1, 5), date(2026, 6, 30))
    check = rollcheck.check_curve(cfg, "DUBAI_SWAP", rows)
    assert check.consistent and check.overrides == {} and "月序偏移（M1 = DCBc2）" in check.note


def test_cli_and_bad_files(cfg, tmp_path, capsys, settings):
    good = tmp_path / "c.csv"
    lines = ["日期,序列代碼,RIC,到期日,合約月"]
    rows = contract_rows(cfg, "BRENT", date(2026, 1, 5), date(2026, 6, 30))
    lines += [f"{d},BRENT_M1,LCOc1,{e},{m}" for d, e, m in rows]
    lines += ["2026-01-05,USDCNY,CNY=,,"]                    # 不是曲線的序列：忽略
    good.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert main(["verify-rolls", str(good)]) == 0
    assert "BRENT（2026-01-05 ~ 2026-06-30）" in capsys.readouterr().out
    bad = tmp_path / "bad.csv"
    bad.write_text("日期,值\n2026-01-05,1\n", encoding="utf-8")
    assert main(["verify-rolls", str(bad)]) == 1
    assert "不是合約對照文件" in capsys.readouterr().err
    bad.write_text("日期,序列代碼,RIC,到期日,合約月\nnot-a-date,BRENT_M1,LCOc1,,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="第 2 行的日期看不懂"):
        rollcheck.read_contracts(bad)
    assert rollcheck.report([]) == ["文件裡沒有任何有換月規則的曲線的 M1（序列代碼像 BRENT_M1）"]


def test_contract_sidecar_is_refused_by_import_with_a_hint(session, settings):
    from crudelab.imports import ImportFlowError, preview_file
    content = "日期,序列代碼,RIC,到期日,合約月\n2026-11-20,BRENT_M1,LCOc1,2026-11-30,JAN7\n".encode()
    with pytest.raises(ImportFlowError, match="crudelab verify-rolls"):
        preview_file(session, settings, content, "reuters_20261124_contracts.csv", "reuters_xls")


def test_exchange_specific_rule_details(cfg):
    """2026-11-23 用路透的到期日核對出來的兩條交易所細則。"""
    from crudelab.schema import RollRule
    # ICE 柴油：14 日之前第 2 個工作日；14 日是周日時直接從 14 日往前數（12 日周五是第 1 個，11 日是第 2 個）
    gasoil = cfg.curves["GASOIL_ICE"].roll
    assert contracts.last_trading_day(gasoil, contracts.ordinal(2024, 1)) == date(2024, 1, 11)
    assert contracts.last_trading_day(gasoil, contracts.ordinal(2024, 2)) == date(2024, 2, 12)   # 14 日周三：不受影響
    plain = gasoil.model_copy(update={"count_from_day": False})
    assert contracts.last_trading_day(plain, contracts.ordinal(2024, 1)) == date(2024, 1, 10)   # 舊寫法差一天
    # ICE Brent：元旦前一個工作日不做最後交易日
    brent = cfg.curves["BRENT"].roll
    assert contracts.last_trading_day(brent, contracts.ordinal(2024, 2)) == date(2023, 12, 28)   # 12-29 是元旦前一天
    assert contracts.last_trading_day(brent, contracts.ordinal(2025, 2)) == date(2024, 12, 30)
    assert contracts.last_trading_day(brent, contracts.ordinal(2024, 3)) == date(2024, 1, 31)    # 其他月份不受影響
    assert contracts.last_trading_day(brent, contracts.ordinal(2026, 10)) == date(2026, 8, 28)   # 英國銀行假日
    with pytest.raises(ValueError, match="MM-DD"):
        RollRule(months_before=1, not_eve_of=["12/25"])
    with pytest.raises(ValueError, match="count_from_day 只用於整數 day"):
        RollRule(months_before=1, count_from_day=True)
