"""平衡表編譯器：把聲明式的供需平衡表（config/balances/*.yaml）編譯成指標。

和煉廠模型一樣，平衡表本身不帶計算代碼，編譯後就是普通指標，
所以量綱檢查、追溯、as_of、情境調整對平衡表全部自動適用。

命名規則（B = 平衡表代碼）：
    BAL_{B}_S_{項}   供應各項        BAL_{B}_SUPPLY    供應合計
    BAL_{B}_D_{項}   需求各項        BAL_{B}_DEMAND    需求合計
    BAL_{B}_BALANCE  供應 − 需求 = 隱含庫存變化（正 = 累庫）
    BAL_{B}_ACTUAL   實際庫存變化（有 actual_stock_change 時）
    BAL_{B}_RESIDUAL 隱含 − 實際 = 未解釋項
情境調整（見 scenarios.py）通常就加在 BAL_{B}_S_* / BAL_{B}_D_* 這些單項上。
"""
from __future__ import annotations

from dataclasses import dataclass

from .schema import BalanceDef, IndicatorDef


@dataclass(frozen=True)
class BalanceRow:
    label: str
    indicator: str
    kind: str                      # supply / demand / supply_total / demand_total / balance / actual / residual
    adjustable: bool = False       # 單項可以加情境調整


@dataclass(frozen=True)
class BalanceSpec:
    definition: BalanceDef
    rows: tuple[BalanceRow, ...]
    balance: str

    @property
    def code(self) -> str:
        return self.definition.code


def compile_balance(b: BalanceDef) -> tuple[list[IndicatorDef], BalanceSpec]:
    prefix = f"BAL_{b.code}"
    group = f"平衡表：{b.name_zh}"
    indicators: list[IndicatorDef] = []

    def indicator(suffix: str, name: str, formula: str, hidden: bool = True) -> str:
        code = f"{prefix}_{suffix}"
        indicators.append(IndicatorDef(code=code, name_zh=f"{b.name_zh}｜{name}", module="balance", group=group,
                                       unit=b.unit, formula=formula, ffill_limit=b.ffill_limit,
                                       hidden=hidden, model=b.code))
        return code

    rows: list[BalanceRow] = []
    supply = [indicator(f"S_{line.code}", line.name_zh, line.value) for line in b.supply]
    rows += [BalanceRow(line.name_zh, code, "supply", True) for line, code in zip(b.supply, supply, strict=True)]
    supply_total = indicator("SUPPLY", "供應合計", " + ".join(supply))
    rows.append(BalanceRow("供應合計", supply_total, "supply_total"))

    demand = [indicator(f"D_{line.code}", line.name_zh, line.value) for line in b.demand]
    rows += [BalanceRow(line.name_zh, code, "demand", True) for line, code in zip(b.demand, demand, strict=True)]
    demand_total = indicator("DEMAND", "需求合計", " + ".join(demand))
    rows.append(BalanceRow("需求合計", demand_total, "demand_total"))

    balance = indicator("BALANCE", "平衡（隱含庫存變化）", f"{supply_total} - {demand_total}", hidden=False)
    rows.append(BalanceRow("平衡（供應 − 需求）", balance, "balance"))
    if b.actual_stock_change:
        actual = indicator("ACTUAL", "實際庫存變化", b.actual_stock_change)
        residual = indicator("RESIDUAL", "未解釋項", f"{balance} - {actual}", hidden=False)
        rows += [BalanceRow("實際庫存變化", actual, "actual"),
                 BalanceRow("未解釋項（隱含 − 實際）", residual, "residual")]
    return indicators, BalanceSpec(b, tuple(rows), balance)
