"""單位代數：把單位代碼解析成量綱，供公式做量綱檢查。

單位代碼由「原子單位」用 * 和 / 連成，從左到右結合，例如：
    USD/bbl    CNY/USD    bbl/t    kb/d    USD/t
無量綱寫作 "1"（比率、收率、稅率都是無量綱）。
原子單位登記在 config/master/units.yaml 的 atoms 裡，用了沒登記的原子會報錯。

這裡只管量綱是否一致，不做數值換算：換算係數一律是顯式的參數（例如 BBL_PER_T_GASOIL，單位 bbl/t），
這樣每一步換算都看得見、追得到。
"""
from __future__ import annotations

import re

Dim = tuple[tuple[str, int], ...]          # 例如 (("USD", 1), ("bbl", -1))，按原子名排序
DIMENSIONLESS: Dim = ()
_TOKEN = re.compile(r"([*/]?)\s*([^*/\s]+)")


class UnitError(ValueError):
    pass


def _normalize(exponents: dict[str, int]) -> Dim:
    return tuple(sorted((k, v) for k, v in exponents.items() if v != 0))


def parse_unit(code: str, atoms: set[str]) -> Dim:
    text = str(code).strip()
    if not text:
        raise UnitError("單位不能為空（無量綱請寫 \"1\"）")
    exponents: dict[str, int] = {}
    position = 0
    for match in _TOKEN.finditer(text):
        if match.start() != position or bool(match.group(1)) == (position == 0):   # 首項不帶運算符，其餘必須帶
            raise UnitError(f"無法解析單位「{code}」")
        position = match.end()
        operator, atom = match.group(1), match.group(2)
        if atom == "1":
            continue
        if atom not in atoms:
            raise UnitError(f"單位「{code}」用了沒有登記的原子單位「{atom}」")
        exponents[atom] = exponents.get(atom, 0) + (-1 if operator == "/" else 1)
    if position != len(text):
        raise UnitError(f"無法解析單位「{code}」")
    return _normalize(exponents)


def multiply(a: Dim, b: Dim) -> Dim:
    out = dict(a)
    for atom, power in b:
        out[atom] = out.get(atom, 0) + power
    return _normalize(out)


def divide(a: Dim, b: Dim) -> Dim:
    return multiply(a, tuple((atom, -power) for atom, power in b))


def power(a: Dim, exponent: int) -> Dim:
    return _normalize({atom: p * exponent for atom, p in a})


def format_dim(dim: Dim) -> str:
    if not dim:
        return "1"
    top = [a if p == 1 else f"{a}^{p}" for a, p in dim if p > 0]
    bottom = [a if p == -1 else f"{a}^{-p}" for a, p in dim if p < 0]
    return ("*".join(top) or "1") + "".join(f"/{b}" for b in bottom)
