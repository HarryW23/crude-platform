"""數字核對：大模型寫的文字裡，每一個數字都必須能在給它的事實裡找到。

這是「大模型不產生數字」這條規矩的執行手段。核對不過的解讀照樣保存，但會標記並列出對不上的數字，
由人決定用不用。
"""
from __future__ import annotations

import re

_NUMBER = re.compile(r"(?<![\w.])[-−+]?\d[\d,]*(?:\.\d+)?")
_ALWAYS_ALLOWED = {float(n) for n in range(0, 13)} | {100.0}     # 序數、月份、「三大機構」「100%」這類


def _values(text: str) -> set[float]:
    out = set()
    for token in _NUMBER.findall(text):
        try:
            out.add(abs(float(token.replace(",", "").replace("−", "-"))))
        except ValueError:
            continue
    return out


def unknown_numbers(text: str, reference: str) -> list[str]:
    """text 裡出現、但 reference 裡找不到的數字（按絕對值比較，容許四捨五入到更少的小數位）。"""
    allowed = _values(reference) | _ALWAYS_ALLOWED
    rounded = {round(v, d) for v in allowed for d in (0, 1, 2)}
    unknown = []
    for token in _NUMBER.findall(text):
        try:
            value = abs(float(token.replace(",", "").replace("−", "-")))
        except ValueError:
            continue
        if value not in allowed and value not in rounded and token not in unknown:
            unknown.append(token)
    return unknown
