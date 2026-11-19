"""指標引擎：按配置裡的公式計算指標，並能把任何一個結果逐層拆回原始數據點和參數版本。

計算規則：
  1. 取公式引用的每個名字的值：序列 → 數據點；參數 → 按生效日期的階梯函數；指標 → 遞歸計算
  2. 把所有輸入對齊到同一個日期索引（各「隨時間變化」的輸入的日期並集）
       序列缺值時最多沿用 max(指標的 ffill_limit, 序列的 valid_days) 天
       參數按生效日期取值，不限天數
  3. 逐日代入公式；任何一個輸入在當天沒有值，當天就沒有結果（不估算、不插值）
只引用參數的指標是「純參數指標」，它本身也是階梯函數，被引用時和參數一樣處理。

情境試算：建引擎時傳入 Scenario，可以臨時替換參數值、平移序列，不寫庫。這也是之後推演層的基礎。

一個引擎實例對應一次請求、一個 as_of 時點、一個情境，內部會緩存。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import contracts
from . import formula as fx
from .config import Config
from .ingest import load_points, load_points_cached, to_series
from .models import ImportBatch, Parameter, ParameterVersion
from .schema import EPOCH

MIN_OBS_FOR_PERCENTILE = 20


def _empty() -> pd.Series:
    """沒有結果時的回傳值。索引固定是日期型，調用方可以放心和日期比較。"""
    return pd.Series(dtype=float, index=pd.DatetimeIndex([]))


@dataclass(frozen=True)
class RangedShift:
    """在 [date_from, date_to] 之間給 target（序列或指標）加上 delta。date_to 為空 = 一直持續。"""
    target: str
    date_from: date
    date_to: date | None
    delta: float


@dataclass(frozen=True)
class Scenario:
    """假設情境，疊加在數據之上，不寫庫。

    parameters  參數 key → 替換後的值（全時段）
    shifts      序列或指標代碼 → 在其值上加的數（全時段）
    ranged      分時段的加減，見 RangedShift。保存的情境（scenarios.py）用的就是這個
    """
    parameters: dict[str, float] = field(default_factory=dict)
    shifts: dict[str, float] = field(default_factory=dict)
    ranged: tuple[RangedShift, ...] = ()

    def validate(self, cfg: Config) -> None:
        unknown = [k for k in self.parameters if k not in cfg.parameters]
        unknown += [k for k in (*self.shifts, *(r.target for r in self.ranged))
                    if k not in cfg.series and k not in cfg.indicators]
        if unknown:
            raise KeyError(f"情境裡有不存在的名字：{', '.join(unknown)}")


class IndicatorEngine:
    def __init__(self, session: Session, cfg: Config, as_of: datetime | None = None,
                 scenario: Scenario | None = None):
        self.session, self.cfg, self.as_of = session, cfg, as_of
        self.scenario = scenario or Scenario()
        self.scenario.validate(cfg)
        self._points: pd.DataFrame | None = None
        self._computed: dict[str, pd.Series] = {}
        self._step: set[str] = set(cfg.parameters)         # 階梯函數型的名字：參數 + 純參數指標
        self._deps: dict[str, tuple[list[str], list[str]]] = {}
        self._verified = {p.key: p.verified for p in session.scalars(select(Parameter))}
        self._versions = self._load_versions()
        self._pin: pd.Timestamp | None = None               # 見 pinned()
        self._raw_cache: dict[str, pd.Series] = {}           # 序列代碼 → 原始值（和固定合約視圖共用）
        self._views: dict[pd.Timestamp, IndicatorEngine] = {}

    # ------------------------------ 輸入 ------------------------------
    def _load_versions(self) -> dict[str, pd.DataFrame]:
        stmt = select(ParameterVersion).order_by(ParameterVersion.id)
        if self.as_of is not None:
            stmt = stmt.where(ParameterVersion.created_at <= self.as_of)
        effective: dict[str, dict[date, ParameterVersion]] = {}
        for v in self.session.scalars(stmt):
            effective.setdefault(v.key, {})[v.valid_from] = v       # 同一生效日期，id 大的覆蓋
        return {key: pd.DataFrame(
            [{"valid_from": pd.Timestamp(d), "value": v.value, "note": v.note} for d, v in sorted(rows.items())]
        ).set_index("valid_from") for key, rows in effective.items()}

    def parameter_values(self, key: str) -> pd.Series:
        if key in self.scenario.parameters:
            return pd.Series([float(self.scenario.parameters[key])], index=pd.DatetimeIndex([pd.Timestamp(EPOCH)]))
        frame = self._versions.get(key)
        return frame["value"].astype(float) if frame is not None else _empty()

    def _shifted(self, name: str, values: pd.Series) -> pd.Series:
        """套用情境裡針對 name 的加減。"""
        out = values + self.scenario.shifts.get(name, 0.0)
        for r in self.scenario.ranged:
            if r.target == name and len(out):
                mask = out.index >= pd.Timestamp(r.date_from)
                if r.date_to is not None:
                    mask &= out.index <= pd.Timestamp(r.date_to)
                out = out + np.where(mask, r.delta, 0.0)
        return out

    def series_values(self, code: str) -> pd.Series:
        """一條原始序列的值（已套用情境）。固定合約視圖（pinned）下，曲線序列換成錨定日那組合約的價格。"""
        if self._pin is not None:
            return self._shifted(code, contracts.pinned_series(self.cfg, code, self._pin, self._raw))
        return self._shifted(code, self._raw(code))

    def _raw(self, code: str) -> pd.Series:
        """一條序列的原始值。第一次調用時把所有數據點按序列分組一次，之後直接取（不再每次篩整張表）。"""
        if not self._raw_cache:
            points = self._all_points()
            for name, part in points.groupby("series_code", sort=False):
                self._raw_cache[str(name)] = to_series(part, str(name))
            self._raw_cache.setdefault("", pd.Series(dtype=float))      # 標記：已經分組過（數據為空時也不重來）
        cached = self._raw_cache.get(code)
        return cached.copy() if cached is not None else pd.Series(dtype=float, index=pd.DatetimeIndex([]), name=code)

    def pinned(self, anchor: pd.Timestamp) -> IndicatorEngine:
        """固定合約視圖：每條曲線的 Mn 都換成 anchor 那天的 Mn 合約在各天的價格（見 contracts.py）。

        共用已載入的數據，不重新查庫。用來算「同一組合約」的變化，換月不會造成跳動。
        """
        if self._pin is not None:
            raise ValueError("固定合約視圖不能再固定一次")
        if anchor not in self._views:
            view = copy.copy(self)            # 共用已載入的數據和分組緩存
            view._pin, view._computed, view._step, view._views = anchor, {}, set(self._step), {}
            self._views[anchor] = view
        return self._views[anchor]

    def adjusted_series(self, code: str) -> tuple[pd.Series, int]:
        """換月調整後的價格（比例調整，最新值等於原始值）和沒能調整的換月次數。非曲線序列原樣返回。"""
        return contracts.adjusted_prices(self.cfg, code, self.series_values)

    def view_values(self, code: str) -> pd.Series:
        """計價視角的值。固定合約視圖下，固定用錨定日那天的計價合約。"""
        values, _ = contracts.view_series(self.cfg, self.cfg.views[code], self._raw, anchor=self._pin)
        return self._shifted(code, values)

    def views_used(self, code: str) -> list[str]:
        """名字（指標）直接或間接用到的計價視角。"""
        if code in self.cfg.views:
            return [code]
        if code not in self.cfg.indicators:
            return []
        found: list[str] = []
        for name in fx.names(self.cfg.indicators[code].formula):
            found += self.views_used(name)
        return list(dict.fromkeys(found))

    def _direct_series(self, code: str) -> list[str]:
        """指標直接用到的序列（不含經計價視角間接用到的曲線月份：那些不按曲線換月，而按計價月份切換）。"""
        found: list[str] = []
        for name in fx.names(self.cfg.indicators[code].formula):
            if name in self.cfg.indicators:
                found += self._direct_series(name)
            elif name in self.cfg.series:
                found.append(name)
        return list(dict.fromkeys(found))

    def rolled_curves(self, code: str) -> list[str]:
        """名字（序列或指標）直接依賴的、有換月規則的曲線。"""
        names = self._direct_series(code) if code in self.cfg.indicators else [code]
        found = [m.curve.code for m in (contracts.member(self.cfg, n) for n in names) if m is not None]
        return list(dict.fromkeys(found))

    def values(self, name: str) -> pd.Series:
        """一個序列、計價視角或指標的值。"""
        if name in self.cfg.views:
            return self.view_values(name)
        return self.compute(name) if name in self.cfg.indicators else self.series_values(name)

    def _all_points(self) -> pd.DataFrame:
        if self._points is None:
            codes = sorted(self.cfg.series)
            self._points = (load_points(self.session, codes, as_of=self.as_of) if self.as_of is not None
                            else load_points_cached(self.session, codes))
        return self._points

    def dependencies(self, code: str) -> tuple[list[str], list[str]]:
        """一個指標最終依賴的全部（原始序列, 參數）。"""
        if code not in self._deps:
            series: list[str] = []
            params: list[str] = []
            for name in fx.names(self.cfg.indicators[code].formula):
                if name in self.cfg.indicators:
                    sub_series, sub_params = self.dependencies(name)
                    series += sub_series
                    params += sub_params
                elif name in self.cfg.series:
                    series.append(name)
                elif name in self.cfg.views:
                    view = self.cfg.views[name]
                    series += [f"{view.curve}_M{n}" for n in contracts.view_months_needed(self.cfg, view)]
                else:
                    params.append(name)
            self._deps[code] = (list(dict.fromkeys(series)), list(dict.fromkeys(params)))
        return self._deps[code]

    def missing_series(self, code: str) -> list[str]:
        have = set(self._all_points()["series_code"].unique())
        return [s for s in self.dependencies(code)[0] if s not in have]

    def missing_parameters(self, code: str) -> list[str]:
        return [p for p in self.dependencies(code)[1]
                if p not in self._versions and p not in self.scenario.parameters]

    def unverified_parameters(self, code: str) -> list[str]:
        return [p for p in self.dependencies(code)[1] if not self._verified.get(p, False)]

    # ------------------------------ 計算 ------------------------------
    def compute(self, code: str) -> pd.Series:
        if code in self._computed:
            return self._computed[code]
        if code not in self.cfg.indicators:
            raise KeyError(f"沒有名為 {code} 的指標")
        ind = self.cfg.indicators[code]
        env: dict[str, pd.Series] = {}
        tolerance: dict[str, int] = {}
        for name in fx.names(ind.formula):
            if name in self.cfg.indicators:
                env[name] = self.compute(name)
                tolerance[name] = ind.ffill_limit
            elif name in self.cfg.series:
                env[name] = self.series_values(name)
                tolerance[name] = max(ind.ffill_limit, self.cfg.series[name].resolved_valid_days)
            elif name in self.cfg.views:
                env[name] = self.view_values(name)
                tolerance[name] = ind.ffill_limit
            else:
                env[name] = self.parameter_values(name)

        dynamic = [name for name in env if name not in self._step]
        if not dynamic:
            self._step.add(code)
        if any(values.empty for values in env.values()):
            result = _empty()
        else:
            index = pd.DatetimeIndex([])
            for name in dynamic or env:
                index = index.union(env[name].index)
            aligned = {}
            for name, values in env.items():
                if name in self._step:
                    aligned[name] = values.reindex(index, method="ffill")
                elif name in self.cfg.series and self.cfg.series[name].within_period:
                    aligned[name] = _within_period(values, index, self.cfg.series[name].frequency)
                elif tolerance[name]:
                    aligned[name] = values.reindex(index, method="ffill",
                                                   tolerance=pd.Timedelta(days=tolerance[name]))
                else:
                    aligned[name] = values.reindex(index)
            raw = fx.evaluate(ind.formula, aligned)
            result = self._shifted(code, raw.replace([np.inf, -np.inf], np.nan).dropna())
        self._computed[code] = result.rename(code)
        return self._computed[code]

    def value_at(self, name: str, on: date) -> float | None:
        """某個指標或參數在某一天的值。階梯函數取當天生效的值。"""
        values = self.compute(name) if name in self.cfg.indicators else self.parameter_values(name)
        ts = pd.Timestamp(on)
        if name in self._step:
            past = values[values.index <= ts]
            return float(past.iloc[-1]) if len(past) else None
        return float(values[ts]) if ts in values.index else None

    def summary(self, code: str) -> dict:
        ind = self.cfg.indicators[code]
        every = self.compute(code)
        # 「最新值」只看已經發生的期間；日期在今天之後的值是預測，不算最新（as_of 視圖以 as_of 那天為今天）
        today = pd.Timestamp(self.as_of.date() if self.as_of else date.today())
        values = every[every.index <= today] if not every.empty and code not in self._step else every
        missing_series, missing_params = self.missing_series(code), self.missing_parameters(code)
        if len(every):
            status = "ok"
        elif missing_params:
            status = "missing_parameter"
        elif missing_series:
            status = "missing_data"
        else:
            status = "no_overlap"
        out = {"code": code, "name_zh": ind.name_zh, "module": ind.module, "group": ind.group, "unit": ind.unit,
               "formula": ind.formula, "notes": ind.notes, "model": ind.model, "hidden": ind.hidden,
               "frequency": self.cfg.frequency_of(code),
               "status": status, "missing_series": missing_series, "missing_parameters": missing_params,
               "unverified_parameters": self.unverified_parameters(code), "n": int(len(every)),
               "forecast_until": every.index[-1].date().isoformat() if len(every) > len(values) else None,
               "last_date": None, "last_value": None, "chg_1d": None, "chg_5d": None, "chg_20d": None,
               "roll_adjusted": bool(self.rolled_curves(code)), "chg_unadjusted": [],
               "percentile_1y": None}
        if len(values):
            last = float(values.iloc[-1])
            out["last_date"], out["last_value"] = values.index[-1].date().isoformat(), last
            out.update(self._changes(code, values))
            window = values[values.index >= values.index[-1] - pd.Timedelta(days=365)]
            if len(window) >= MIN_OBS_FOR_PERCENTILE:
                out["percentile_1y"] = float((window <= last).mean() * 100)
        return out

    def _changes(self, code: str, values: pd.Series) -> dict:
        """日 / 5 日 / 20 日變化。依賴期貨曲線的指標按「同一組合約」對比，換月不算進變化。

        同一組合約取不到（例如 M12 往回 20 天要 M13）時退回直接相減，並在 chg_unadjusted 裡標出是哪幾個。
        """
        last, end = float(values.iloc[-1]), values.index[-1]
        curves = self.rolled_curves(code)
        out: dict = {"roll_adjusted": bool(curves) or bool(self.views_used(code)), "chg_unadjusted": []}
        pinned = None
        for key, back in (("chg_1d", 1), ("chg_5d", 5), ("chg_20d", 20)):
            if len(values) <= back:
                continue
            start = values.index[-1 - back]
            out[key] = last - float(values.iloc[-1 - back])
            rolled = [c for c in curves
                      if contracts.rolls_between(self.cfg.curves[c].roll, start.date(), end.date())]
            rolled += [v for v in self.views_used(code)                 # 計價月份切換了（例如跨月）
                       if contracts.view_target(self.cfg.views[v], start.date())
                       != contracts.view_target(self.cfg.views[v], end.date())]
            if not rolled:
                continue
            if pinned is None:
                view = self.pinned(end)
                pinned = view.compute(code) if code in self.cfg.indicators else view.series_values(code)
            if start in pinned.index and end in pinned.index:
                out[key] = float(pinned[end]) - float(pinned[start])
            else:
                out["chg_unadjusted"].append(key)
        return out

    # ------------------------------ 追溯 ------------------------------
    def trace(self, name: str, on: date, _tolerance: int = 0) -> dict:
        """把某一天的值拆成一棵樹：指標 → 子指標 / 參數版本 / 原始數據點（含來源和所在格子）。"""
        if name in self.cfg.parameters:
            return self._trace_parameter(name, on)
        if name in self.cfg.series:
            return self._trace_series(name, on, _tolerance)
        if name in self.cfg.views:
            return self._trace_view(name, on)
        ind = self.cfg.indicators[name]
        node = {"kind": "indicator", "code": name, "name_zh": ind.name_zh, "unit": ind.unit,
                "formula": ind.formula, "value": self.value_at(name, on),
                "children": [self.trace(child, on, self._trace_tolerance(ind, child))
                             for child in fx.names(ind.formula)]}
        if fx.uses_window(ind.formula):
            node["note"] = "公式含 lag / ma / diff，結果還用到了更早日期的值，下面只列出當天的輸入"
        return node

    def _trace_parameter(self, key: str, on: date) -> dict:
        p = self.cfg.parameters[key]
        node = {"kind": "parameter", "code": key, "name_zh": p.name_zh, "unit": p.unit, "value": None,
                "verified": self._verified.get(key, False)}
        frame = self._versions.get(key)
        past = frame[frame.index <= pd.Timestamp(on)] if frame is not None else None
        if past is None or past.empty:
            node["note"] = "尚未提供數值" if frame is None else "這一天還沒有生效的版本"
            return node
        node.update(value=float(past["value"].iloc[-1]), valid_from=past.index[-1].date().isoformat(),
                    version_note=past["note"].iloc[-1])
        return node

    def _trace_tolerance(self, ind, child: str) -> int:
        if child in self.cfg.series:
            return max(ind.ffill_limit, self.cfg.series[child].resolved_valid_days)
        return ind.ffill_limit

    def _trace_view(self, code: str, on: date) -> dict:
        view = self.cfg.views[code]
        values, targets = contracts.view_series(self.cfg, view, self._raw)
        ts = pd.Timestamp(on)
        target = contracts.view_target(view, on)
        node = {"kind": "view", "code": code, "name_zh": view.name_zh, "unit": self.cfg.unit_of(code),
                "value": float(values[ts]) if ts in values.index else None,
                "contract": contracts.contract_label(target), "curve": view.curve}
        front = contracts.front_ordinal(self.cfg.curves[view.curve].roll, on)
        n = target - front + 1
        if n >= 1:
            node["children"] = [self._trace_series(f"{view.curve}_M{n}", on, 0)]
            node["note"] = f"計價月份 {contracts.contract_label(target)}，當天是曲線上的 M{n}"
        else:
            node["note"] = f"計價月份 {contracts.contract_label(target)} 的合約已到期，用它的最後結算價"
        return node

    def _trace_series(self, code: str, on: date, tolerance: int) -> dict:
        s = self.cfg.series[code]
        node = {"kind": "series", "code": code, "name_zh": s.name_zh, "unit": s.unit, "value": None}
        points = self._all_points()
        part = points[points["series_code"] == code]
        dates = pd.to_datetime(part["obs_date"])
        ts = pd.Timestamp(on)
        if s.within_period:                       # 只在所屬期間有效：取當天所在期間的那個值
            start = pd.Timestamp(_period_start(on, s.frequency))
            part = part[dates == start]
        else:
            part = part[(dates <= ts) & (dates >= ts - pd.Timedelta(days=tolerance))]
        if part.empty:
            node["note"] = "這一天沒有數據"
            return node
        row = part.sort_values("obs_date").iloc[-1]
        batch = self.session.get(ImportBatch, int(row["batch_id"])) if pd.notna(row["batch_id"]) else None
        node.update(value=float(row["value"]), obs_date=row["obs_date"].isoformat(), source_code=row["source_code"],
                    source_ref=row["source_ref"], batch_id=batch.id if batch else None,
                    filename=batch.filename if batch else None)
        if row["obs_date"] != on:
            node["note"] = f"當天無值，沿用 {row['obs_date'].isoformat()} 的值"
        return node


def _period_start(day: date, frequency: str) -> date:
    if frequency == "quarterly":
        return date(day.year, (day.month - 1) // 3 * 3 + 1, 1)
    return date(day.year, day.month, 1)


def _within_period(values: pd.Series, index: pd.DatetimeIndex, frequency: str) -> pd.Series:
    """只在所屬期間有效的序列（例如官價）對齊到日期索引：每天取它所在月 / 季那一期的值，不沿用到下一期。"""
    if values.empty:
        return pd.Series(np.nan, index=index)
    freq = "Q" if frequency == "quarterly" else "M"
    by_period = pd.Series(values.to_numpy(dtype=float), index=values.index.to_period(freq))
    by_period = by_period[~by_period.index.duplicated(keep="last")]
    return pd.Series(by_period.reindex(index.to_period(freq)).to_numpy(), index=index)
