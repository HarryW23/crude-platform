import copy

import pytest
from sqlalchemy import func, select

from crudelab.master import ConfigError, sync_to_db, validate
from crudelab.models import Parameter, Series, SeriesAlias


def test_shipped_config_is_valid_and_curves_expand(cfg):
    codes = {s["code"] for s in cfg.series}
    assert {"BRENT_M1", "BRENT_M3", "SC_M2", "ESPO_DIFF", "USDCNY"} <= codes


def test_sync_is_idempotent_and_keeps_edited_parameters(session, cfg):
    n_series = session.scalar(select(func.count()).select_from(Series))
    n_alias = session.scalar(select(func.count()).select_from(SeriesAlias))
    session.get(Parameter, "BBL_PER_T_GASOIL").value = 7.5
    stats = sync_to_db(session, cfg)
    session.commit()
    assert stats["new_aliases"] == 0 and stats["new_parameters"] == 0
    assert session.scalar(select(func.count()).select_from(Series)) == n_series
    assert session.scalar(select(func.count()).select_from(SeriesAlias)) == n_alias
    assert session.get(Parameter, "BBL_PER_T_GASOIL").value == 7.5


@pytest.mark.parametrize("mutate, expected", [
    (lambda c: c.series[0].update(unit="USD/lb"), "不在單位字典"),
    (lambda c: c.series.append(dict(c.series[0])), "重複"),
    (lambda c: c.indicators.append({"code": "BAD", "formula": "BRENT_M1 - NOPE"}), "不存在的名字"),
    (lambda c: c.indicators.append({"code": "BAD", "formula": "__import__('os')"}), "不允許"),
    (lambda c: c.indicators.extend([{"code": "A1", "formula": "B1 + BRENT_M1"},
                                    {"code": "B1", "formula": "A1 * 2"}]), "循環引用"),
    (lambda c: c.series[1].update(aliases=["LCOc1"]), "同時指向"),
])
def test_validation_catches_config_mistakes(cfg, mutate, expected):
    bad = copy.deepcopy(cfg)
    mutate(bad)
    with pytest.raises(ConfigError, match=expected):
        validate(bad)
