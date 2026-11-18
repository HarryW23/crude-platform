"""報告：把某一刻各模組的狀態收集成一份結構化的事實（facts），再按固定模板排成正文。

正文完全由代碼排版，不經過大模型，所以正文裡的每個數字都來自指標引擎。
大模型的解讀是可選的附加部分，單獨存放，並經過數字核對（llm/guard.py）。
報告一旦生成就是快照，之後數據或參數怎麼變都不影響它。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import projection as projection_service
from . import signals as signal_service
from .config import Config
from .indicators import IndicatorEngine
from .ingest import latest_market_date
from .models import Parameter, Projection, Report, utcnow

KINDS = {"daily": "日報", "weekly": "周報", "monthly": "月報"}
EVENT_STATUS = {"rumor": "傳聞", "confirmed": "已確認", "ongoing": "持續中"}
STATUS_TEXT = {"missing_data": "缺數據", "missing_parameter": "缺參數", "no_overlap": "輸入日期對不上"}


class ReportError(ValueError):
    pass


def build_facts(session: Session, cfg: Config, kind: str, projection_id: int | None = None) -> dict:
    engine = IndicatorEngine(session, cfg)
    summaries = {code: engine.summary(code) for code, ind in cfg.indicators.items() if not ind.hidden}
    groups: dict[str, dict[str, list[dict]]] = {}
    for s in summaries.values():
        if s["model"]:
            continue
        groups.setdefault(s["module"], {}).setdefault(s["group"], []).append(
            {k: s[k] for k in ("code", "name_zh", "unit", "status", "last_date", "last_value", "chg_1d", "chg_5d",
                               "chg_20d", "percentile_1y", "frequency", "missing_series", "missing_parameters")})

    balances = []
    for spec in cfg.balances.values():
        s = summaries[spec.balance]
        values = engine.compute(spec.balance)
        today = date.today().isoformat()
        future = [{"date": ts.date().isoformat(), "value": float(v)} for ts, v in values.items()
                  if ts.date().isoformat() > today][:6]
        balances.append({"code": spec.code, "name_zh": spec.definition.name_zh, "unit": spec.definition.unit,
                         "status": s["status"], "last_date": s["last_date"], "last_value": s["last_value"],
                         "missing_series": s["missing_series"], "forecast": future})

    refineries = [{"code": spec.code, "name_zh": spec.definition.name_zh, "unit": spec.definition.unit,
                   **{k: summaries[spec.margin][k] for k in ("status", "last_date", "last_value", "chg_1d", "chg_20d",
                                                             "missing_series", "missing_parameters")},
                   "unverified": len(summaries[spec.margin]["unverified_parameters"])}
                  for spec in cfg.refineries.values()]

    events = [{"id": e.id, "title": e.title, "category": e.category, "status": e.status, "bias": e.bias,
               "started_on": str(e.started_on) if e.started_on else None, "summary": e.summary,
               "scenario_code": e.scenario_code} for e in signal_service.active_events(session)]

    from . import fair_value
    from .projection import horizons_of
    fair_values = [fair_value.describe(cfg, fair_value.current(engine, cfg, code), horizons_of(cfg))
                   for code in cfg.fair_values]

    if projection_id is None:
        projection_id = session.scalar(select(func.max(Projection.id)).where(Projection.archived_at.is_(None)))
    projection = None
    if projection_id is not None and cfg.projection is not None:
        try:
            projection = projection_service.compute(session, cfg, projection_id)
        except projection_service.ProjectionError as exc:
            raise ReportError(str(exc)) from exc

    params = list(session.scalars(select(Parameter).where(Parameter.active)))
    ok = sum(s["status"] == "ok" for s in summaries.values())
    return {"kind": kind, "kind_name": KINDS[kind], "generated_at": utcnow().isoformat(timespec="seconds"),
            "data_date": str(latest_market_date(session, cfg.market_series) or "") or None,
            "coverage": {"indicators_ok": ok, "indicators_total": len(summaries),
                         "parameters_unverified": sum(not p.verified for p in params),
                         "parameters_total": len(params)},
            "signals": signal_service.compute(session, engine), "indicators": groups, "balances": balances,
            "refineries": refineries, "events": events, "projection": projection, "fair_values": fair_values}


# ------------------------------ 排版 ------------------------------

def _n(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def _s(value: float | None, digits: int = 2) -> str:
    return "" if value is None else f"{value:+,.{digits}f}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|",
            *("| " + " | ".join(row) + " |" for row in rows), ""]


def _indicator_section(lines: list[str], title: str, groups: dict[str, list[dict]], level: int = 2) -> None:
    """一個模組的指標：按分組列表，算不出來的指標列在表後並說明原因。level 是標題層級。"""
    lines += [f"{'#' * level} {title}", ""]
    if not groups:
        lines += ["這個模組還沒有指標。", ""]
        return
    for group, items in groups.items():
        ready = [i for i in items if i["status"] == "ok"]
        lines += [f"{'#' * (level + 1)} {group}", ""]
        if ready:
            lines += _table(["指標", "日期", "最新值", "上期變化", "20 期變化", "一年分位", "單位"],
                            [[i["name_zh"], i["last_date"], _n(i["last_value"]), _s(i["chg_1d"]), _s(i["chg_20d"]),
                              "" if i["percentile_1y"] is None else f"{i['percentile_1y']:.0f}%", i["unit"]]
                             for i in ready])
        blocked = [i for i in items if i["status"] != "ok"]
        if blocked:
            def why(i: dict) -> str:
                missing = i["missing_parameters"] or i["missing_series"]
                return f"{i['name_zh']}（{STATUS_TEXT[i['status']]}{'：' + '、'.join(missing) if missing else ''}）"
            lines += ["無法計算：" + "；".join(why(i) for i in blocked), ""]


def render_markdown(facts: dict) -> str:
    c, sig = facts["coverage"], facts["signals"]
    lines = [f"# 原油市場{facts['kind_name']}", "",
             f"數據截至 {facts['data_date'] or '（無數據）'}，生成於 {facts['generated_at']} UTC。", "",
             f"可計算的指標 {c['indicators_ok']} / {c['indicators_total']}；"
             f"待核對的參數 {c['parameters_unverified']} / {c['parameters_total']}。"
             + ("含待核對參數的結果僅供參考。" if c["parameters_unverified"] else ""), ""]

    lines += ["## 方向信號", "",
              f"綜合：**{sig['overall']['label']}**"
              + (f"（{sig['overall']['score']:+.2f}，分數範圍 −1 至 +1）"
                 if sig["overall"]["score"] is not None else "")
              + f"，有結果的模組 {sig['overall']['modules_ok']} / {sig['overall']['modules_total']}。", ""]
    lines += _table(["模組", "判斷", "分數", "權重", "有結果的信號"],
                    [[m["name_zh"], m["label"], "—" if m["score"] is None else f"{m['score']:+.2f}",
                      "—" if m["weight"] is None else f"{m['weight']:g}", f"{m['signals_ok']} / {m['signals_total']}"]
                     for m in sig["modules"]])
    lines += ["信號衡量的是各指標相對自身歷史的位置，不是預測。", ""]

    lines += ["## 基本面公允價值", ""]
    for fv in facts.get("fair_values", []):
        if fv["fair"] is None:
            lines += [f"{fv['name_zh']}：無法計算（{'；'.join(fv['reasons']) or '沒有數據'}）。", ""]
            continue
        lines += [f"{fv['name_zh']}（{fv['date']}）：{fv['target_name']} {_n(fv['price'])}，公允價值 {_n(fv['fair'])}，"
                  f"偏離 {_s((fv['deviation_pct'] or 0) * 100, 1)}%"
                  + (f"，偏離的半衰期約 {fv['half_life']:.0f} 個交易日" if fv["half_life"] else "")
                  + f"，R² {_n(fv['r2'])}。", ""]
        lines += _table(["因子", "當前值", "對公允價值的貢獻"],
                        [[f["name_zh"], _n(f["value"]), f"{_s((f['contribution_pct'] or 0) * 100, 1)}%"]
                         for f in fv["factors"]])
        if fv["status"] != "ok":
            lines += [f"不作為推演的漂移：{'；'.join(fv['reasons'])}。", ""]
    if not facts.get("fair_values"):
        lines += ["沒有定義公允價值模型。", ""]

    _indicator_section(lines, "地域價差", facts["indicators"].get("spread", {}))

    lines += ["## 供需平衡", ""]
    for b in facts["balances"]:
        if b["status"] != "ok":
            lines += [f"{b['name_zh']}：{STATUS_TEXT.get(b['status'], b['status'])}"
                      + (f"（{'、'.join(b['missing_series'])}）" if b["missing_series"] else "") + "。", ""]
            continue
        lines += [f"{b['name_zh']}：最新一期（{b['last_date']}）平衡 {_s(b['last_value'])} {b['unit']}"
                  "（正為累庫）。", ""]
        if b["forecast"]:
            lines += _table(["預測期", *[f["date"][:7] for f in b["forecast"]]],
                            [[f"平衡（{b['unit']}）", *[_s(f["value"]) for f in b["forecast"]]]])
    _indicator_section(lines, "供需指標", facts["indicators"].get("balance", {}), level=3)

    lines += ["## 煉廠盈利", ""]
    lines += _table(["模型", "日期", "毛利", "上期變化", "20 期變化", "單位", "備註"],
                    [[r["name_zh"], r["last_date"] or "", _n(r["last_value"]) if r["status"] == "ok" else "—",
                      _s(r["chg_1d"]), _s(r["chg_20d"]), r["unit"],
                      (f"含 {r['unverified']} 項待核對假設" if r["unverified"] else "") if r["status"] == "ok"
                      else STATUS_TEXT[r["status"]]] for r in facts["refineries"]])
    _indicator_section(lines, "裂解與價差", facts["indicators"].get("refinery", {}), level=3)

    lines += ["## 地緣事件", ""]
    if facts["events"]:
        lines += _table(["事件", "狀態", "開始", "方向判斷", "量化影響"],
                        [[e["title"], EVENT_STATUS.get(e["status"], e["status"]), e["started_on"] or "",
                          f"{e['bias']:+d}", f"情境 {e['scenario_code']}" if e["scenario_code"] else "未量化"]
                         for e in facts["events"]])
    else:
        lines += ["目前沒有已確認且仍在進行的事件。", ""]

    lines += ["## 價格推演", ""]
    p = facts["projection"]
    if p is None:
        lines += ["還沒有建立推演。", ""]
    else:
        a = p["anchor"]
        lines += [f"{p['name_zh']}。{a['name_zh']} 當前 {_n(a['price'])} {a['unit']}（{a['date'] or '無數據'}）。", ""]
        lines += [f"注意：{problem}。" for problem in p["problems"]] + ([""] if p["problems"] else [])
        header = ["分支", "機率", *[h["name_zh"] for h in p["horizons"]]]
        rows = []
        for case in p["cases"]:
            cells = []
            for h in p["horizons"]:
                cell = case["horizons"][h["code"]]
                if cell["central"] is None:
                    cells.append("無法計算")
                elif cell["low"] is None:
                    cells.append(_n(cell["central"]))
                else:
                    cells.append(f"{_n(cell['central'])}（{_n(cell['low'])} – {_n(cell['high'])}）")
            rows.append([case["label"], f"{case['probability']:.0%}", *cells])
        if any(v is not None for v in p["expected"].values()):
            rows.append(["機率加權", "", *[_n(p["expected"][h["code"]]) for h in p["horizons"]]])
        lines += _table(header, rows)
        lines += ["括號內是按歷史波動給出的一倍標準差區間，不是置信保證。"
                  "各分支的中樞 = 當前價格 + 基本面回歸 + 供需效應 + 直接價格調整；機率和敏感度是人為設定。"
                  + ("" if not p.get("drift") or not p["drift"].get("effects")
                     else f"本推演使用 {p['drift']['name_zh']} 的基本面回歸。"), ""]
    return "\n".join(lines).rstrip() + "\n"


def generate(session: Session, cfg: Config, kind: str, projection_id: int | None = None,
             actor: str = "local") -> Report:
    if kind not in KINDS:
        raise ReportError(f"報告類型必須是：{'、'.join(KINDS)}")
    facts = build_facts(session, cfg, kind, projection_id)
    row = Report(kind=kind, title=f"原油市場{KINDS[kind]} {facts['data_date'] or date.today().isoformat()}",
                 data_date=date.fromisoformat(facts["data_date"]) if facts["data_date"] else None,
                 facts=facts, markdown=render_markdown(facts), created_by=actor)
    session.add(row)
    session.flush()
    return row
