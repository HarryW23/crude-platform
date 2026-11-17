"""對話式查詢：用自然語言問，系統查數後回答。

規矩和其他用到大模型的地方一樣，而且在這裡最要緊：
  - 大模型不算數。它只能調用下面這些工具取數；「如果…會怎樣」也是調工具，由指標引擎和推演公式去算。
  - 工具都是唯讀的。問答不能改數據、參數、情境或事件。
  - 回答裡的每個數字都要能在工具返回的結果（或使用者的問題）裡找到，對不上的會被列出來。
  - 每一步調了什麼工具、拿到什麼結果，都原樣返回給頁面，使用者可以展開核對。

為了不綁定某一家的「工具調用」專有格式，這裡用最樸素的做法：每一步讓模型輸出一個 JSON，
要麼是 {"tool": 名字, "args": {...}}，要麼是 {"answer": 文字}。DeepSeek、GPT、Claude 都能用同一套。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import events as event_service
from .. import projection as projection_service
from .. import scenarios as scenario_service
from .. import signals as signal_service
from ..config import Config
from ..indicators import IndicatorEngine, RangedShift, Scenario
from ..models import Projection, ScenarioRow
from ..parameters import effective_versions
from ..refinery import breakdown
from .base import LLMError, LLMProvider
from .guard import unknown_numbers

MAX_STEPS = 8                 # 一個問題最多調幾次工具
MAX_RESULT_CHARS = 7000       # 單次工具結果給模型看的長度上限
MAX_HISTORY_TURNS = 6
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


@dataclass
class _Ctx:
    session: Session
    cfg: Config
    engine: IndicatorEngine


class ToolError(ValueError):
    """工具用法不對（代碼不存在、參數不合法）。訊息會交還給模型，讓它改正。"""


# ------------------------------ 工具 ------------------------------

def _r(value):
    """給模型看的數字保留 4 位小數，省字數。"""
    if isinstance(value, float):
        return None if pd.isna(value) else round(value, 4)
    if isinstance(value, dict):
        return {k: _r(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_r(v) for v in value]
    if isinstance(value, (date, pd.Timestamp)):
        return str(value)[:10]
    return value


def _points(values: pd.Series, last: int) -> list[dict]:
    return [{"date": ts.date().isoformat(), "value": float(v)} for ts, v in values.iloc[-last:].items()]


def _need(ctx: _Ctx, code: str) -> None:
    if not ctx.cfg.has(code):
        raise ToolError(f"{code} 既不是序列也不是指標。先用 search 找正確的代碼")


def _scenario_engine(ctx: _Ctx, scenario: str | None) -> IndicatorEngine:
    if not scenario:
        return ctx.engine
    try:
        return IndicatorEngine(ctx.session, ctx.cfg, scenario=scenario_service.load(ctx.session, ctx.cfg, scenario))
    except scenario_service.ScenarioError as exc:
        raise ToolError(str(exc)) from exc


def tool_search(ctx: _Ctx, query: str) -> dict:
    q, cfg = str(query).strip().lower(), ctx.cfg
    if not q:
        raise ToolError("query 不能為空")
    pools = [("指標", [(i.code, i.name_zh, i.unit) for i in cfg.indicators.values() if not i.hidden]),
             ("序列", [(s.code, s.name_zh, s.unit) for s in cfg.series.values()]),
             ("參數", [(p.key, p.name_zh, p.unit) for p in cfg.parameters.values()]),
             ("煉廠模型", [(r.code, r.definition.name_zh, r.definition.unit) for r in cfg.refineries.values()]),
             ("平衡表", [(b.code, b.definition.name_zh, b.definition.unit) for b in cfg.balances.values()])]
    hits = [{"kind": kind, "code": code, "name": name, "unit": unit}
            for kind, items in pools for code, name, unit in items if q in code.lower() or q in name.lower()]
    return {"matches": hits[:25], "total": len(hits)}


def tool_get_values(ctx: _Ctx, code: str, last: int = 10, scenario: str | None = None) -> dict:
    _need(ctx, code)
    engine = _scenario_engine(ctx, scenario)
    values = engine.values(code)
    out = {"code": code, "name": ctx.cfg.name_of(code), "unit": ctx.cfg.unit_of(code),
           "frequency": ctx.cfg.frequency_of(code), "points": _points(values, max(1, min(int(last), 60)))}
    if code in ctx.cfg.indicators:
        s = engine.summary(code)
        out.update({k: s[k] for k in ("formula", "status", "last_date", "last_value", "chg_1d", "chg_5d", "chg_20d",
                                      "percentile_1y", "forecast_until", "missing_series", "missing_parameters",
                                      "unverified_parameters")})
        out["points"] = [p for p in out["points"] if s["last_date"] is None or p["date"] <= s["last_date"]] or out["points"]
    elif values.empty:
        out["status"] = "missing_data"
    return out


def tool_list_indicators(ctx: _Ctx, module: str) -> dict:
    if module not in ("spread", "balance", "refinery"):
        raise ToolError("module 必須是 spread（地域價差）、balance（供需）或 refinery（煉廠）")
    rows = []
    for code, ind in ctx.cfg.indicators.items():
        if ind.module == module and not ind.hidden:
            s = ctx.engine.summary(code)
            rows.append({k: s[k] for k in ("code", "name_zh", "group", "unit", "status", "last_date", "last_value",
                                           "chg_1d", "chg_20d", "percentile_1y")})
    return {"indicators": rows}


def tool_get_balance(ctx: _Ctx, code: str, scenario: str | None = None) -> dict:
    if code not in ctx.cfg.balances:
        raise ToolError(f"沒有名為 {code} 的平衡表。可用的：{'、'.join(ctx.cfg.balances)}")
    spec, engine = ctx.cfg.balances[code], _scenario_engine(ctx, scenario)
    index = engine.compute(spec.balance).index
    today = pd.Timestamp(date.today())
    periods = index[index <= today][-4:].append(index[index > today][:6])
    return {"name": spec.definition.name_zh, "unit": spec.definition.unit, "note": "平衡 = 供應 − 需求，正為累庫",
            "periods": [{"date": ts.date().isoformat(), "forecast": bool(ts > today)} for ts in periods],
            "rows": [{"label": row.label, "indicator": row.indicator,
                      "values": [None if pd.isna(v) else float(v)
                                 for v in engine.compute(row.indicator).reindex(periods)]} for row in spec.rows]}


def tool_get_refinery(ctx: _Ctx, code: str) -> dict:
    if code not in ctx.cfg.refineries:
        raise ToolError(f"沒有名為 {code} 的煉廠模型。可用的：{'、'.join(ctx.cfg.refineries)}")
    spec = ctx.cfg.refineries[code]
    s = ctx.engine.summary(spec.margin)
    out = {"name": spec.definition.name_zh, "unit": spec.definition.unit, "notes": spec.definition.notes,
           **{k: s[k] for k in ("status", "last_date", "last_value", "chg_1d", "chg_20d", "missing_series",
                                "missing_parameters")},
           "unverified_assumptions": len(s["unverified_parameters"])}
    if s["last_date"]:
        detail = breakdown(ctx.engine, spec, date.fromisoformat(s["last_date"]))
        out["components"] = [{"label": c["label"], "sign": c["sign"], "value": c["value"]} for c in detail["components"]]
        out["products"] = [{"name": p["name_zh"], "yield": p["weight"], "price": p["price"], "value": p["value"]}
                           for p in detail["products"]]
    return out


def tool_get_signals(ctx: _Ctx) -> dict:
    result = signal_service.compute(ctx.session, ctx.engine)
    for module in result["modules"]:
        module["signals"] = [{k: s.get(k) for k in ("name_zh", "status", "value", "mean", "z", "direction", "bias",
                                                    "score")} for s in module["signals"]]
    return result


def tool_list_events(ctx: _Ctx) -> dict:
    return {"events": [{"id": e.id, "title": e.title, "category": e.category, "status": e.status, "bias": e.bias,
                        "started_on": e.started_on, "summary": e.summary, "scenario": e.scenario_code}
                       for e in event_service.list_events(ctx.session) if not e.draft]}


def tool_list_scenarios(ctx: _Ctx) -> dict:
    rows = ctx.session.scalars(select(ScenarioRow).where(ScenarioRow.archived_at.is_(None)))
    return {"scenarios": [{"code": s.code, "name": s.name_zh, "adjustments": [
        {"target": a.target, "delta": a.delta, "from": a.date_from, "to": a.date_to, "note": a.note}
        for a in scenario_service.adjustments(ctx.session, s.code)]} for s in rows]}


def tool_get_projection(ctx: _Ctx) -> dict:
    latest = ctx.session.scalar(select(Projection.id).where(Projection.archived_at.is_(None))
                                .order_by(Projection.id.desc()).limit(1))
    if latest is None:
        return {"status": "還沒有建立推演"}
    try:
        return projection_service.compute(ctx.session, ctx.cfg, latest)
    except projection_service.ProjectionError as exc:
        raise ToolError(str(exc)) from exc


def tool_get_parameter(ctx: _Ctx, key: str) -> dict:
    if key not in ctx.cfg.parameters:
        raise ToolError(f"沒有名為 {key} 的參數。先用 search 找正確的 key")
    p = ctx.cfg.parameters[key]
    return {"key": key, "name": p.name_zh, "unit": p.unit, "description": p.description,
            "versions": [{"valid_from": v.valid_from, "value": v.value, "note": v.note}
                         for v in effective_versions(ctx.session, key)] or "尚未提供數值"}


def tool_what_if(ctx: _Ctx, adjustments: list | None = None, parameters: dict | None = None,
                 watch: list | None = None, price_adjustment: float = 0.0) -> dict:
    """臨時假設：不保存。回傳被關注的指標在假設前後的值，以及（有推演設定時）對價格的影響。"""
    ranged = []
    for item in adjustments or []:
        try:
            ranged.append(RangedShift(item["target"], date.fromisoformat(str(item.get("date_from") or date.today())),
                                      date.fromisoformat(str(item["date_to"])) if item.get("date_to") else None,
                                      float(item["delta"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError("adjustments 的每一項需要 target、delta，可選 date_from、date_to（YYYY-MM-DD）") from exc
    try:
        scenario = Scenario(parameters={k: float(v) for k, v in (parameters or {}).items()}, ranged=tuple(ranged))
        engine = IndicatorEngine(ctx.session, ctx.cfg, scenario=scenario)
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolError(f"假設裡有不存在的名字或不合法的數值：{exc.args[0] if exc.args else exc}") from exc
    if not ranged and not parameters and not price_adjustment:
        raise ToolError("至少要給一項假設：adjustments、parameters 或 price_adjustment")

    codes = list(watch or ([ctx.cfg.projection.balance_indicator] if ctx.cfg.projection else []))
    today, compared = pd.Timestamp(date.today()), []
    for code in codes:
        _need(ctx, code)
        frame = pd.DataFrame({"base": ctx.engine.values(code), "scenario": engine.values(code)}).dropna()
        window = pd.concat([frame[frame.index <= today].iloc[-1:], frame[frame.index > today].iloc[:6]])
        compared.append({"code": code, "name": ctx.cfg.name_of(code), "unit": ctx.cfg.unit_of(code),
                         "rows": [{"date": ts.date().isoformat(), "forecast": bool(ts > today), "base": float(r.base),
                                   "scenario": float(r.scenario), "change": float(r.scenario - r.base)}
                                  for ts, r in window.iterrows()]})
    out: dict = {"note": "臨時試算，沒有保存", "compared": compared}
    if ctx.cfg.projection is not None:
        out["price"] = projection_service.adhoc(ctx.session, ctx.cfg, scenario, float(price_adjustment or 0))
        out["price"]["formula"] = "中樞 = 當前價格 − 敏感度 × 平衡變化 + 直接價格調整；low/high 是一倍標準差區間"
    return out


TOOLS: dict[str, tuple[Callable, str]] = {
    "search": (tool_search, 'search(query) — 按中文名或代碼的片段找指標、序列、參數、煉廠模型、平衡表。不確定代碼時先用它'),
    "get_values": (tool_get_values, 'get_values(code, last=10, scenario=null) — 一個序列或指標的最近幾個值；指標另附公式、狀態、變化、一年分位'),
    "list_indicators": (tool_list_indicators, 'list_indicators(module) — 一個模組的全部指標現況。module: spread / balance / refinery'),
    "get_balance": (tool_get_balance, 'get_balance(code, scenario=null) — 平衡表（最近 4 期 + 預測 6 期）。可指定已保存的情境'),
    "get_refinery": (tool_get_refinery, 'get_refinery(code) — 煉廠模型的毛利、拆解、產品明細'),
    "get_signals": (tool_get_signals, 'get_signals() — 四個模組的方向信號和綜合分數（−1 利空 … +1 利多）'),
    "list_events": (tool_list_events, 'list_events() — 事件庫裡已確認的地緣事件'),
    "list_scenarios": (tool_list_scenarios, 'list_scenarios() — 已保存的情境及其調整'),
    "get_projection": (tool_get_projection, 'get_projection() — 最新一個價格推演：各分支的機率、中樞、區間、機率加權價'),
    "get_parameter": (tool_get_parameter, 'get_parameter(key) — 一個參數的數值和生效日期'),
    "what_if": (tool_what_if, 'what_if(adjustments=[{target, delta, date_from, date_to}], parameters={key: value}, watch=[code], '
                              'price_adjustment=0) — 臨時假設後重算。adjustments 加在序列或指標上（常用平衡表的單項，如 '
                              'BAL_GLOBAL_S_OPEC）；回傳 watch 裡各指標假設前後的值，以及對價格中樞和區間的影響'),
}


# ------------------------------ 對話循環 ------------------------------

def _system_prompt() -> str:
    return f"""你是一個原油研究平台的查詢助手。使用者用自然語言提問，你通過調用工具取得數據，然後回答。今天是 {date.today()}。

規則：
- 你不知道任何行情或數據，一切數字都必須來自工具的返回結果。不要用你自己記得的價格、庫存、產量。
- 不要自己做算術。需要「如果…會怎樣」時調用 what_if，讓系統去算；需要差值、變化時用工具結果裡已有的字段。
- 工具結果裡標明缺數據、缺參數、樣本不足、待核對的地方，要如實告訴使用者，不要繞過或猜測。
- 你只能查詢和試算，不能修改任何數據、參數、情境或事件。使用者要求修改時，告訴他去哪個頁面操作。
- 使用者的假設裡沒有給數量時（例如只說「霍爾木茲中斷」而沒說影響多少），不要自己編一個數量：請他給出，或說明你用的是他在問題裡給的數字。
- 回答用繁體中文，簡潔，先給結論再給依據。提到的每個數字都要說明是哪個指標、哪一天。

每一步只輸出一個 JSON 對象，不要輸出別的文字：
- 需要數據時：{{"tool": "工具名", "args": {{...}}}}
- 可以回答時：{{"answer": "回答的文字"}}

可用的工具：
{chr(10).join("- " + doc for _, doc in TOOLS.values())}"""


def _parse(raw: str) -> dict | None:
    try:
        value = json.loads(_FENCE.sub("", raw.strip()))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def answer(provider: LLMProvider, session: Session, cfg: Config, question: str,
           history: list[dict] | None = None) -> dict:
    """回答一個問題。回傳 {answer, steps, unknown_numbers, guard_passed, provider, model}。

    steps 是完整的取數過程：每一步調了什麼工具、參數是什麼、結果是什麼。
    """
    ctx = _Ctx(session, cfg, IndicatorEngine(session, cfg))
    turns = [t for t in (history or []) if t.get("role") in ("user", "assistant") and t.get("content")]
    transcript = "".join(f"{'使用者' if t['role'] == 'user' else '助手'}：{t['content']}\n"
                         for t in turns[-2 * MAX_HISTORY_TURNS:])
    transcript += f"使用者：{question}\n"
    steps: list[dict] = []
    system = _system_prompt()

    for _ in range(MAX_STEPS + 1):
        raw = provider.complete(system, transcript + "\n請輸出下一步的 JSON。", json=True, max_tokens=2000)
        reply = _parse(raw)
        if reply is None:                       # 不是 JSON：當作直接回答（照樣要過數字核對）
            reply = {"answer": raw.strip()}
        if "answer" in reply:
            text = str(reply["answer"]).strip()
            reference = question + transcript + json.dumps([s["result"] for s in steps], ensure_ascii=False, default=str)
            unknown = unknown_numbers(text, reference)
            return {"answer": text, "steps": steps, "unknown_numbers": unknown, "guard_passed": not unknown,
                    "provider": provider.name, "model": provider.model}
        name, args = reply.get("tool"), reply.get("args") or {}
        if len(steps) >= MAX_STEPS:
            break
        if name not in TOOLS or not isinstance(args, dict):
            result = {"error": f"沒有名為 {name} 的工具，或 args 不是對象。可用的工具：{'、'.join(TOOLS)}"}
        else:
            try:
                result = _r(TOOLS[name][0](ctx, **args))
            except ToolError as exc:
                result = {"error": str(exc)}
            except TypeError as exc:            # 參數名不對或缺參數
                result = {"error": f"參數不對：{exc}"}
        steps.append({"tool": name, "args": args, "result": result})
        shown = json.dumps(result, ensure_ascii=False, default=str)
        if len(shown) > MAX_RESULT_CHARS:
            shown = shown[:MAX_RESULT_CHARS] + "…（結果過長，已截斷；請用更具體的查詢）"
        transcript += f"你調用了 {name}({json.dumps(args, ensure_ascii=False, default=str)})，結果：{shown}\n"
    raise LLMError(f"查了 {MAX_STEPS} 步仍沒有得出回答，已停止。請把問題問得更具體一些")
