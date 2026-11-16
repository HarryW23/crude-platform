import shutil

import pytest
import yaml
from sqlalchemy import func, select

from crudelab.config import ConfigError, load_config
from crudelab.master import sync_to_db
from crudelab.models import Parameter, ParameterVersion, Series, SeriesAlias
from crudelab.parameters import effective_versions, set_value
from crudelab.settings import PROJECT_ROOT

CONFIG_DIR = PROJECT_ROOT / "config"


def test_shipped_config_is_valid(cfg):
    assert {"BRENT_M1", "BRENT_M3", "SC_M2", "ESPO_DIFF", "USDCNY"} <= set(cfg.series)
    assert cfg.series["ARAB_LIGHT_OSP_ASIA"].resolved_valid_days == 35
    assert cfg.series["BRENT_M1"].resolved_valid_days == 0
    assert set(cfg.refineries) == {"SG_COMPLEX", "SG_SIMPLE", "NWE_COMPLEX", "CN_FUEL_SD", "CN_INTEGRATED"}
    assert cfg.parameters["FLAT_TD3C"].seed_versions == []             # 必填但尚未提供
    assert [v.value for v in cfg.parameters["CN_VAT_RATE"].seed_versions] == [0.17, 0.16, 0.13]


def _edit(tmp_path, relative, mutate):
    """複製一份配置，改其中一個文件，回傳新的配置目錄。"""
    target = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, target)
    path = target / relative
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return target


@pytest.mark.parametrize("relative, mutate, expected", [
    ("master/series/crude.yaml", lambda d: d["series"][0].update(unit="USD/lb"), "不在單位字典"),
    ("master/series/crude.yaml", lambda d: d["series"].append(dict(d["series"][0])), "重複定義"),
    ("master/series/crude.yaml", lambda d: d["series"][0].update(alias=["x"]), "Extra inputs"),     # 字段名打錯
    ("master/series/crude.yaml", lambda d: d["series"][1].update(aliases=["Dated Brent"]), "同時指向"),
    ("master/series/crude.yaml", lambda d: d["series"][0].update(code="dated"), "should match pattern"),
    ("master/units.yaml", lambda d: d["units"].append({"code": "USD/lb", "name_zh": "x", "dimension": "price"}),
     "沒有登記的原子單位"),
    ("indicators/spread.yaml", lambda d: d[0].update(formula="BRENT_M1 - NOPE"), "不存在的名字"),
    ("indicators/spread.yaml", lambda d: d[0].update(formula="__import__('os')"), "不允許"),
    ("indicators/spread.yaml", lambda d: d[0].update(formula="BRENT_M1 - SC_M1"), "單位不同"),
    ("indicators/spread.yaml", lambda d: d[0].update(unit="CNY/bbl"), "公式算出來的單位是 USD/bbl"),
    ("indicators/spread.yaml", lambda d: d[0].update(formula="SPR_BRENT_DUBAI + SPR_BRENT_WTI")
        or d[1].update(formula="SPR_BRENT_WTI + BRENT_M1"), "循環引用"),
    ("indicators/spread.yaml", lambda d: d[0].update(code="BRENT_M1"), "序列與指標重名"),
    ("parameters/conversion.yaml", lambda d: d[0].update(versions=[{"valid_from": "2020-01-01", "value": 1}]),
     "只能寫一個"),
    ("refineries/sg_complex.yaml", lambda d: d["products"][0].update({"yield": 0.5}), "收率之和不能超過 1"),
    ("refineries/sg_complex.yaml", lambda d: d["feedstock"][0].update(share=0.9), "share 之和必須等於 1"),
    ("refineries/sg_complex.yaml", lambda d: d["products"][4].update(price="SG_HSFO_380"), "RM_SG_COMPLEX_REV_HSFO"),
    ("refineries/cn_fuel_shandong.yaml", lambda d: d["tax"].update(vat_rate="NOPE"), "不存在的名字 NOPE"),
])
def test_validation_catches_config_mistakes(tmp_path, relative, mutate, expected):
    with pytest.raises(ConfigError, match=expected):
        load_config(_edit(tmp_path, relative, mutate))


def test_all_errors_are_reported_together(tmp_path):
    target = _edit(tmp_path, "indicators/spread.yaml",
                   lambda d: d[0].update(formula="BRENT_M1 - NOPE") or d[1].update(unit="CNY/t"))
    with pytest.raises(ConfigError) as exc:
        load_config(target)
    assert "NOPE" in str(exc.value) and "SPR_BRENT_DUBAI" in str(exc.value)


def test_sync_is_idempotent_and_backend_edits_win(session, cfg):
    counts = lambda: [session.scalar(select(func.count()).select_from(m))  # noqa: E731
                      for m in (Series, SeriesAlias, Parameter, ParameterVersion)]
    before = counts()
    set_value(session, "BBL_PER_T_GASOIL", 7.5, note="口徑調整")
    stats = sync_to_db(session, cfg)
    session.commit()
    assert stats["new_aliases"] == 0 and stats["seeded_parameter_versions"] == 0
    assert counts() == [*before[:3], before[3] + 1]
    assert effective_versions(session, "BBL_PER_T_GASOIL")[-1].value == 7.5      # 沒有被配置覆蓋
    assert session.get(Parameter, "BBL_PER_T_GASOIL").verified is True


def test_removed_series_is_deactivated_not_deleted(session, cfg, tmp_path):
    smaller = load_config(_edit(tmp_path, "master/series/market.yaml",
                                lambda d: d["series"].pop(next(i for i, s in enumerate(d["series"])
                                                               if s["code"] == "EIA_REFINERY_UTIL"))))
    sync_to_db(session, smaller)
    session.commit()
    assert session.get(Series, "EIA_REFINERY_UTIL").active is False
