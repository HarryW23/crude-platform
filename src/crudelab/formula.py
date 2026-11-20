"""公式語言：解析、取名字、量綱推導、求值。純函數，不碰數據庫。

語法是 Python 表達式的一個很小的子集：
    數字、名字、+ - * / **、括號，以及 FUNCTIONS 裡的函數。
名字可以是 序列代碼 / 參數 key / 指標代碼。用語法樹白名單解析，不會執行任意代碼。
"""
from __future__ import annotations

import ast
from collections.abc import Callable

import pandas as pd

from . import analytics, units
from .units import DIMENSIONLESS, Dim

# 函數表：新增函數時在這裡加一行，並在 _UNIT_RULES 裡說明它對單位的影響。
FUNCTIONS: dict[str, Callable] = {
    "lag": lambda x, n: x.shift(int(n)),             # 滯後 n 期
    "ma": lambda x, n: x.rolling(int(n)).mean(),     # n 期均值
    "diff": lambda x, n=1: x.diff(int(n)),           # n 期變化
    "abs": lambda x: abs(x),
    "yoy": lambda x: x - analytics.year_ago(x),      # 同比變化：減去一年前同期的值
    "seasonal_avg": lambda x, n: analytics.seasonal_stats(x, int(n))["avg"],   # 前 n 年同期均值（n 年不齊則無值）
    "mtd": lambda x: _month_to_date_mean(x),         # 當月 1 日到當天的平均（月底那天 = 整月均價）
}
WINDOW_FUNCTIONS = {"lag", "ma", "diff", "yoy", "seasonal_avg", "mtd"}    # 會用到更早日期的值
# 每個函數的參數個數（最少, 最多），以及哪些位置必須是正整數常數（期數、年數）
ARITY = {"lag": (2, 2), "ma": (2, 2), "diff": (1, 2), "abs": (1, 1), "yoy": (1, 1), "seasonal_avg": (2, 2),
         "mtd": (1, 1)}
COUNT_ARGS = {"lag": {1}, "ma": {1}, "diff": {1}, "seasonal_avg": {1}}
_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
           ast.Div: lambda a, b: a / b, ast.Pow: lambda a, b: a ** b}


class FormulaError(ValueError):
    pass


def _month_to_date_mean(x: pd.Series) -> pd.Series:
    """每天的值 = 同一個自然月裡、到這一天為止所有有值日子的平均。只用當天及以前的數據。"""
    x = x.dropna()
    if x.empty:
        return x
    months = x.index.to_period("M")
    return x.groupby(months).cumsum() / x.groupby(months).cumcount().add(1)


def _func(node: ast.Call) -> str:
    """函數名（parse 已保證只能是 FUNCTIONS 裡的名字，不能是 a.b() 之類）。"""
    assert isinstance(node.func, ast.Name)
    return node.func.id


def _number(node: ast.Constant) -> float:
    """常數的值（parse 已保證只能是整數或小數，不能是字串、布爾值）。"""
    assert isinstance(node.value, (int, float))
    return float(node.value)


def parse(formula: str) -> ast.Expression:
    try:
        tree = ast.parse(str(formula), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"公式語法錯誤：{formula}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp)):
            ok = True
        elif isinstance(node, (ast.operator, ast.unaryop)):
            ok = isinstance(node, (*_BINOPS, ast.USub, ast.UAdd))
        elif isinstance(node, ast.Constant):
            ok = isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
        elif isinstance(node, ast.Call):
            ok = isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords
            if ok:
                _check_call(node, formula)
        else:
            ok = False
        if not ok:
            raise FormulaError(f"公式裡有不允許的寫法：{formula}")
    return tree


def _check_call(node: ast.Call, formula: str) -> None:
    name = _func(node)
    low, high = ARITY[name]
    if not low <= len(node.args) <= high:
        expected = str(low) if low == high else f"{low} 到 {high}"
        raise FormulaError(f"{name}() 要 {expected} 個參數，寫了 {len(node.args)} 個：{formula}")
    for i in COUNT_ARGS.get(name, ()):
        if i < len(node.args):
            arg = node.args[i]
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, int) and not isinstance(arg.value, bool)
                    and arg.value >= 1):
                raise FormulaError(f"{name}() 的第 {i + 1} 個參數必須是正整數（期數或年數）：{formula}")


def names(formula: str) -> list[str]:
    """公式引用的名字：去重、按出現順序、不含函數名。"""
    tree = parse(formula)
    function_nodes = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    found = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in function_nodes]
    found.sort(key=lambda n: (n.lineno, n.col_offset))
    return list(dict.fromkeys(n.id for n in found))


def uses_window(formula: str) -> bool:
    return any(isinstance(n, ast.Call) and _func(n) in WINDOW_FUNCTIONS for n in ast.walk(parse(formula)))


def infer_unit(formula: str, unit_of: Callable[[str], Dim]) -> Dim:
    """推導公式結果的量綱。加減兩邊量綱必須相同；乘除則量綱相乘除。"""

    def visit(node: ast.AST) -> Dim:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return DIMENSIONLESS
        if isinstance(node, ast.Name):
            return unit_of(node.id)
        if isinstance(node, ast.UnaryOp):
            return visit(node.operand)
        if isinstance(node, ast.Call):
            return visit(node.args[0])              # 目前所有函數都不改變單位
        assert isinstance(node, ast.BinOp)
        left = visit(node.left)
        if isinstance(node.op, ast.Pow):
            exponent = node.right
            if isinstance(exponent, ast.UnaryOp) and isinstance(exponent.operand, ast.Constant):
                value = -_number(exponent.operand) if isinstance(exponent.op, ast.USub) else _number(exponent.operand)
            elif isinstance(exponent, ast.Constant):
                value = _number(exponent)
            else:
                raise FormulaError("** 的指數必須是整數常數")
            if value != int(value):
                raise FormulaError("** 的指數必須是整數常數")
            return units.power(left, int(value))
        right = visit(node.right)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            if left != right:
                raise FormulaError(f"相加減的兩邊單位不同：{units.format_dim(left)} 與 {units.format_dim(right)}"
                                   f"（{ast.unparse(node)}）")
            return left
        return units.multiply(left, right) if isinstance(node.op, ast.Mult) else units.divide(left, right)

    return visit(parse(formula))


def evaluate(formula: str, env: dict):
    """env：名字 → pandas Series（已對齊到同一個日期索引）或 float。"""

    def visit(node: ast.AST):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return _number(node)
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.UnaryOp):
            value = visit(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            return _BINOPS[type(node.op)](visit(node.left), visit(node.right))
        assert isinstance(node, ast.Call)
        args = [visit(a) for a in node.args]
        name = _func(node)
        if name in WINDOW_FUNCTIONS and not isinstance(args[0], pd.Series):
            raise FormulaError(f"{name}() 的第一個參數必須是序列或指標")
        return FUNCTIONS[name](*args)

    return visit(parse(formula))
