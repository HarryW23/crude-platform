"""指標引擎：公式 + 參數 → 時間序列，並且能把任何一個結果逐層拆回原始數據點。

公式裡的名字只有三種來源：序列代碼、參數 key、其他指標代碼。
公式用 Python 的語法樹解析，只放行四則運算和白名單函數，不會執行任意代碼。
"""
from __future__ import annotations

import ast
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .ingest import load_points
from .models import ImportBatch, Parameter, Series

FUNCTIONS = {
    "lag": lambda x, n: x.shift(int(n)),
    "ma": lambda x, n: x.rolling(int(n)).mean(),
    "diff": lambda x, n=1: x.diff(int(n)),
    "abs": lambda x: abs(x),
}
_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
           ast.Div: lambda a, b: a / b, ast.Pow: lambda a, b: a ** b}


class FormulaError(ValueError):
    pass


def parse_formula(formula: str) -> ast.Expression:
    try:
        tree = ast.parse(str(formula), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"公式語法錯誤：{formula}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Name, ast.Load, ast.operator, ast.unaryop)):
            ok = not isinstance(node, (ast.operator, ast.unaryop)) or isinstance(node, (*_BINOPS, ast.USub, ast.UAdd))
        elif isinstance(node, (ast.BinOp, ast.UnaryOp)):
            ok = True
        elif isinstance(node, ast.Constant):
            ok = isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
        elif isinstance(node, ast.Call):
            ok = isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords
        else:
            ok = False
        if not ok:
            raise FormulaError(f"公式裡有不允許的寫法：{formula}")
    return tree


def formula_names(formula: str) -> list[str]:
    """公式引用的名字（去重、保持出現順序、不含函數名）。"""
    tree = parse_formula(formula)
    funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    names = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in funcs]
    names.sort(key=lambda n: (n.lineno, n.col_offset))
    return list(dict.fromkeys(n.id for n in names))


def validate_definitions(indicators: list[dict], series_codes: set[str], param_keys: set[str]) -> list[str]:
    """配置載入時調用：檢查公式語法、引用是否存在、有沒有循環引用。回傳錯誤列表。"""
    errors: list[str] = []
    defs = {d["code"]: d for d in indicators if d.get("code")}
    deps: dict[str, list[str]] = {}
    for code, d in defs.items():
        try:
            deps[code] = formula_names(d.get("formula", ""))
        except FormulaError as exc:
            errors.append(f"指標 {code}：{exc}")
            continue
        unknown = [n for n in deps[code] if n not in defs and n not in series_codes and n not in param_keys]
        if unknown:
            errors.append(f"指標 {code}：公式引用了不存在的名字 {', '.join(unknown)}")
        if not any(n in defs or n in series_codes for n in deps[code]):
            errors.append(f"指標 {code}：公式至少要引用一條序列或一個指標")

    state: dict[str, int] = {}

    def visit(code: str, path: list[str]):
        if state.get(code) == 2:
            return
        if state.get(code) == 1:
            errors.append("指標循環引用：" + " → ".join([*path[path.index(code):], code]))
            return
        state[code] = 1
        for n in deps.get(code, []):
            if n in defs:
                visit(n, [*path, code])
        state[code] = 2

    for code in defs:
        visit(code, [])
    return errors


def _evaluate(node: ast.AST, env: dict):
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, env)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, env)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        return _BINOPS[type(node.op)](_evaluate(node.left, env), _evaluate(node.right, env))
    if isinstance(node, ast.Call):
        args = [_evaluate(a, env) for a in node.args]
        if node.func.id != "abs" and not isinstance(args[0], pd.Series):
            raise FormulaError(f"{node.func.id}() 的第一個參數必須是序列或指標")
        return FUNCTIONS[node.func.id](*args)
    raise FormulaError("無法計算的公式節點")


class IndicatorEngine:
    """一次請求建一個。內部會緩存已讀的序列和已算的指標。"""

    def __init__(self, session: Session, indicators: list[dict]):
        self.session = session
        self.defs = {d["code"]: d for d in indicators}
        self.params = {p.key: p for p in session.scalars(select(Parameter))}
        self.series = {s.code: s for s in session.scalars(select(Series))}
        self._points: pd.DataFrame | None = None
        self._computed: dict[str, pd.Series] = {}

    # ---- 讀數 ----
    def _all_points(self) -> pd.DataFrame:
        if self._points is None:
            leaves = sorted({leaf for code in self.defs for leaf in self.leaf_series(code)})
            self._points = load_points(self.session, leaves)
        return self._points

    def _series_values(self, code: str) -> pd.Series:
        df = self._all_points()
        part = df[df["series_code"] == code]
        return pd.Series(part["value"].to_numpy(dtype=float),
                         index=pd.DatetimeIndex(pd.to_datetime(part["obs_date"])), name=code).sort_index()

    def leaf_series(self, code: str) -> list[str]:
        """一個指標最終依賴的全部原始序列。"""
        out: list[str] = []
        for name in formula_names(self.defs[code]["formula"]):
            if name in self.defs:
                out.extend(self.leaf_series(name))
            elif name in self.series:
                out.append(name)
        return list(dict.fromkeys(out))

    def missing_inputs(self, code: str) -> list[str]:
        """完全沒有數據的輸入序列。非空 = 這個指標算不出來，前台應顯示「缺數據」。"""
        have = set(self._all_points()["series_code"].unique())
        return [s for s in self.leaf_series(code) if s not in have]

    # ---- 計算 ----
    def compute(self, code: str) -> pd.Series:
        if code in self._computed:
            return self._computed[code]
        if code not in self.defs:
            raise KeyError(f"沒有名為 {code} 的指標")
        definition = self.defs[code]
        tolerance = pd.Timedelta(days=int(definition.get("ffill_limit", 0) or 0))
        env: dict = {}
        index = pd.DatetimeIndex([])
        for name in formula_names(definition["formula"]):
            if name in self.defs:
                env[name] = self.compute(name)
            elif name in self.series:
                env[name] = self._series_values(name)
            else:
                env[name] = float(self.params[name].value)
                continue
            index = index.union(env[name].index)
        empty = pd.Series(dtype=float, name=code)
        if any(isinstance(v, pd.Series) and v.empty for v in env.values()):
            result = empty
        else:
            for name, value in env.items():
                if isinstance(value, pd.Series):
                    env[name] = (value.reindex(index, method="ffill", tolerance=tolerance)
                                 if tolerance.days else value.reindex(index))
            raw = _evaluate(parse_formula(definition["formula"]), env)
            result = (raw.replace([np.inf, -np.inf], np.nan).dropna().rename(code)
                      if isinstance(raw, pd.Series) else empty)
        self._computed[code] = result
        return result

    def summary(self, code: str) -> dict:
        d = self.defs[code]
        missing = self.missing_inputs(code)
        values = self.compute(code)
        out = {"code": code, "name_zh": d.get("name_zh", code), "module": d.get("module", "common"),
               "unit": d.get("unit"), "formula": d["formula"], "notes": d.get("notes"),
               "ffill_limit": int(d.get("ffill_limit", 0) or 0), "inputs": self.leaf_series(code),
               "missing_inputs": missing, "n": int(len(values)),
               "status": "ok" if len(values) else ("missing_data" if missing else "no_overlap"),
               "last_date": None, "last_value": None, "change": None}
        if len(values):
            out["last_date"] = values.index[-1].date().isoformat()
            out["last_value"] = float(values.iloc[-1])
            if len(values) > 1:
                out["change"] = float(values.iloc[-1] - values.iloc[-2])
        return out

    # ---- 追溯 ----
    def trace(self, code: str, on: date, _tolerance_days: int = 0) -> dict:
        """把某一天的指標值拆成一棵樹：指標 → 子指標 / 參數 / 原始數據點（含來源和所在格子）。"""
        ts = pd.Timestamp(on)
        if code in self.params:
            p = self.params[code]
            return {"kind": "parameter", "code": code, "name_zh": p.name_zh, "value": float(p.value), "unit": p.unit}
        if code in self.series and code not in self.defs:
            s = self.series[code]
            node = {"kind": "series", "code": code, "name_zh": s.name_zh, "unit": s.unit_code, "value": None}
            df = self._all_points()
            part = df[(df["series_code"] == code) & (pd.to_datetime(df["obs_date"]) <= ts)
                      & (pd.to_datetime(df["obs_date"]) >= ts - pd.Timedelta(days=_tolerance_days))]
            if part.empty:
                node["note"] = "這一天沒有數據"
                return node
            row = part.sort_values("obs_date").iloc[-1]
            batch = self.session.get(ImportBatch, int(row["batch_id"])) if pd.notna(row["batch_id"]) else None
            node.update(value=float(row["value"]), obs_date=row["obs_date"].isoformat(),
                        source_code=row["source_code"], source_ref=row["source_ref"],
                        batch_id=int(row["batch_id"]) if batch else None, filename=batch.filename if batch else None)
            if row["obs_date"] != on:
                node["note"] = f"當天無值，沿用 {row['obs_date'].isoformat()} 的值"
            return node
        d = self.defs[code]
        values = self.compute(code)
        tolerance = int(d.get("ffill_limit", 0) or 0)
        node = {"kind": "indicator", "code": code, "name_zh": d.get("name_zh", code), "unit": d.get("unit"),
                "formula": d["formula"], "value": float(values[ts]) if ts in values.index else None,
                "children": [self.trace(n, on, tolerance) for n in formula_names(d["formula"])]}
        if any(isinstance(n, ast.Call) for n in ast.walk(parse_formula(d["formula"]))):
            node["note"] = "公式含 lag / ma / diff，結果還用到了更早日期的值，下面只列出當天的輸入"
        return node
