"""分油種的桶/噸（#28）：每個係數都和它寫明的 API 度一致，用到原油桶噸換算的公式都用了對應油種的係數。"""
import re

import pytest

LITRES_PER_BBL = 158.987


def bbl_per_t(api: float) -> float:
    """1 公噸的桶數：比重 = 141.5 / (API + 131.5)，1 桶的質量 = 比重 × 158.987 千克。"""
    return 1000 / (141.5 / (api + 131.5) * LITRES_PER_BBL)


@pytest.mark.parametrize("key", ["BBL_PER_T_ESPO", "BBL_PER_T_ARAB_MEDIUM", "BBL_PER_T_ARAB_LIGHT",
                                 "BBL_PER_T_BONNY_LIGHT", "BBL_PER_T_WTI_MIDLAND"])
def test_factor_matches_its_api(cfg, key):
    p = cfg.parameters[key]
    api = float(re.match(r"API (\d+(?:\.\d+)?)", p.description).group(1))
    value = p.value if p.value is not None else p.versions[-1].value
    assert value == pytest.approx(bbl_per_t(api), abs=0.006)
    assert "來源" in p.description


def test_formula_uses_grade_specific_factor(cfg):
    expect = {"FRT_TD3C_BBL": "BBL_PER_T_ARAB_LIGHT", "FRT_TD15_BBL": "BBL_PER_T_BONNY_LIGHT",
              "FRT_TD20_BBL": "BBL_PER_T_BONNY_LIGHT", "FRT_TD25_BBL": "BBL_PER_T_WTI_MIDLAND",
              "CN_CRUDE_ESPO_CNY_T": "BBL_PER_T_ESPO", "CN_CRUDE_ARAB_MEDIUM_CNY_T": "BBL_PER_T_ARAB_MEDIUM"}
    for code, factor in expect.items():
        assert factor in cfg.indicators[code].formula and "BBL_PER_T_CRUDE" not in cfg.indicators[code].formula


def test_generic_factor_is_close_to_medium_crude():
    assert bbl_per_t(33.0) == pytest.approx(7.31, abs=0.01)          # 通用 7.33 約等於 API 33 的中質原油
