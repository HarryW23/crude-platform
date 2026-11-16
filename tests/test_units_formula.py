import pytest

from crudelab import formula as fx
from crudelab import units
from crudelab.units import UnitError, parse_unit

ATOMS = {"USD", "CNY", "bbl", "t", "gal", "WS"}


def dim(code):
    return parse_unit(code, ATOMS)


def test_unit_parsing_and_algebra():
    assert dim("USD/bbl") == (("USD", 1), ("bbl", -1))
    assert dim("1") == () and dim("USD/USD") == ()
    assert units.multiply(dim("USD/gal"), dim("gal/bbl")) == dim("USD/bbl")
    assert units.divide(dim("USD/t"), dim("bbl/t")) == dim("USD/bbl")
    assert units.divide(dim("CNY/bbl"), dim("CNY/USD")) == dim("USD/bbl")
    assert units.format_dim(dim("USD/t/WS")) == "USD/WS/t" and units.format_dim(()) == "1"
    for bad in ("", "USD/", "/bbl", "USD//bbl", "USD/lb", "USD bbl"):
        with pytest.raises(UnitError):
            dim(bad)


UNITS = {"BRENT": "USD/bbl", "GASOIL": "USD/t", "BBL_PER_T": "bbl/t", "RBOB": "USD/gal", "GAL_PER_BBL": "gal/bbl",
         "SC": "CNY/bbl", "USDCNY": "CNY/USD", "VAT": "1", "WS_PTS": "WS", "WS_BASE": "WS", "FLAT": "USD/t"}


def infer(formula):
    return fx.infer_unit(formula, lambda name: dim(UNITS[name]))


def test_unit_inference_accepts_consistent_formulas():
    assert infer("GASOIL / BBL_PER_T - BRENT") == dim("USD/bbl")
    assert infer("(2 * RBOB + RBOB) * GAL_PER_BBL / 3 - BRENT") == dim("USD/bbl")
    assert infer("SC / USDCNY - lag(BRENT, 1)") == dim("USD/bbl")
    assert infer("SC * BBL_PER_T * (1 + VAT)") == dim("CNY/t")
    assert infer("WS_PTS / WS_BASE * FLAT / BBL_PER_T") == dim("USD/bbl")
    assert infer("BRENT ** 2 / BRENT") == dim("USD/bbl")


@pytest.mark.parametrize("formula", [
    "GASOIL - BRENT",            # 美元/噸 減 美元/桶
    "SC - BRENT",                # 人民幣 減 美元
    "BRENT + 1",                 # 有量綱的量加常數
    "BRENT * (1 + GASOIL)",
    "BRENT ** VAT",
])
def test_unit_inference_rejects_inconsistent_formulas(formula):
    with pytest.raises(fx.FormulaError):
        infer(formula)


def test_formula_parsing_is_restricted():
    assert fx.names("(2 * RBOB_M1 + HO_M1) * GAL_PER_BBL / 3 - lag(WTI_M1, 1)") == \
        ["RBOB_M1", "HO_M1", "GAL_PER_BBL", "WTI_M1"]
    assert fx.uses_window("ma(X, 5)") and not fx.uses_window("abs(X)")
    for bad in ("a.b", "f(x)", "x if y else z", "[1]", "'s'", "a < b", "lag(x, n=1)", "1 +", "__import__('os')"):
        with pytest.raises(fx.FormulaError):
            fx.parse(bad)
