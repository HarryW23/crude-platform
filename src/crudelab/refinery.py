"""煉廠模型編譯器：把聲明式的模型配置（config/refineries/*.yaml）編譯成參數和指標。

模型本身不帶任何計算代碼。編譯之後它就是一組普通指標，由同一個指標引擎計算，
所以量綱檢查、參數生效日期、計算追溯對煉廠模型全部自動適用。

命名規則（M = 模型代碼）：
    參數  RM_{M}_Y_{產品}   收率            RM_{M}_S_{原料}   原料配比        RM_{M}_C_{成本}   成本項
    指標  RM_{M}_REV_{產品} 單項產品收入    RM_{M}_REVENUE    產品總收入
          RM_{M}_FEED_{原料} 單項原料成本   RM_{M}_FEED       原料總成本
          RM_{M}_CT 消費稅   RM_{M}_VAT 增值稅   RM_{M}_SURCHARGE 附加（僅有 tax 設定時）
          RM_{M}_GROSS      產品收入 − 原料成本（僅有稅或成本項時單獨列出）
          RM_{M}_MARGIN     最終毛利

含稅模型的算法（產品價、原料成本都是含增值稅價）：
    增值稅 = (產品收入 − 原料成本) / (1 + 稅率) × 稅率
    消費稅 = Σ 收率 × 從量稅額
    附加   = (增值稅 + 消費稅) × 附加稅率
    毛利   = 產品收入 − 原料成本 − 消費稅 − 增值稅 − 附加 − 各成本項
簡化之處：產品收入低於原料成本時增值稅為負（視為進項留抵當期全額抵扣）；加工環節的其他進項不計。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .schema import IndicatorDef, ParameterDef, RefineryModelDef


@dataclass(frozen=True)
class Component:
    """毛利拆解裡的一行。sign=+1 是加項，-1 是減項。"""
    label: str
    ref: str                       # 指標代碼或參數 key
    ref_kind: str                  # "indicator" / "parameter"
    sign: int
    category: str                  # revenue / feed / tax / cost / subtotal / total


@dataclass(frozen=True)
class Line:
    """產品或原料的一行明細。"""
    code: str
    name_zh: str
    weight_param: str              # 收率或配比的參數 key
    price_formula: str
    value_indicator: str           # 收率 × 價格 的指標代碼
    consumption_tax: str | None = None


@dataclass(frozen=True)
class RefinerySpec:
    definition: RefineryModelDef
    margin: str
    revenue: str
    feed: str
    products: tuple[Line, ...]
    feeds: tuple[Line, ...]
    components: tuple[Component, ...]

    @property
    def code(self) -> str:
        return self.definition.code


def compile_model(m: RefineryModelDef) -> tuple[list[ParameterDef], list[IndicatorDef], RefinerySpec]:
    prefix = f"RM_{m.code}"
    group = f"模型：{m.name_zh}"
    params: list[ParameterDef] = []
    indicators: list[IndicatorDef] = []

    def param(key: str, name: str, unit: str, value: float | None, description: str | None = None) -> str:
        params.append(ParameterDef(key=key, name_zh=name, unit=unit, group=group, value=value,
                                   description=description, verified=False, model=m.code))
        return key

    def indicator(suffix: str, name: str, formula: str, hidden: bool = True) -> str:
        code = f"{prefix}_{suffix}"
        indicators.append(IndicatorDef(code=code, name_zh=f"{m.name_zh}｜{name}", module="refinery", group=group,
                                       unit=m.unit, formula=formula, ffill_limit=m.ffill_limit,
                                       hidden=hidden, model=m.code))
        return code

    products = []
    for p in m.products:
        weight = param(f"{prefix}_Y_{p.code}", f"{p.name_zh}收率", "1", p.yield_)
        value = indicator(f"REV_{p.code}", f"{p.name_zh}收入", f"{weight} * ({p.price})")
        products.append(Line(p.code, p.name_zh, weight, p.price, value, p.consumption_tax))
    feeds = []
    for f in m.feedstock:
        weight = param(f"{prefix}_S_{f.code}", f"{f.name_zh}配比", "1", f.share)
        value = indicator(f"FEED_{f.code}", f"{f.name_zh}成本", f"{weight} * ({f.price})")
        feeds.append(Line(f.code, f.name_zh, weight, f.price, value))

    revenue = indicator("REVENUE", "產品總收入", " + ".join(line.value_indicator for line in products))
    feed = indicator("FEED", "原料總成本", " + ".join(line.value_indicator for line in feeds))
    components = [Component("產品總收入", revenue, "indicator", +1, "revenue"),
                  Component("原料總成本", feed, "indicator", -1, "feed")]
    deductions: list[str] = []

    if m.tax is not None:
        taxed = [line for line in products if line.consumption_tax]
        base = []
        if taxed:
            ct = indicator("CT", "消費稅",
                           " + ".join(f"{line.weight_param} * {line.consumption_tax}" for line in taxed))
            components.append(Component("消費稅", ct, "indicator", -1, "tax"))
            deductions.append(ct)
            base.append(ct)
        vat = indicator("VAT", "增值稅", f"({revenue} - {feed}) / (1 + {m.tax.vat_rate}) * {m.tax.vat_rate}")
        components.append(Component("增值稅", vat, "indicator", -1, "tax"))
        deductions.append(vat)
        base.append(vat)
        surcharge = indicator("SURCHARGE", "城建及教育附加", f"({' + '.join(base)}) * {m.tax.surcharge_rate}")
        components.append(Component("城建及教育附加", surcharge, "indicator", -1, "tax"))
        deductions.append(surcharge)

    for c in m.costs:
        key = param(f"{prefix}_C_{c.code}", c.name_zh, m.unit, c.value, c.description)
        components.append(Component(c.name_zh, key, "parameter", -1, "cost"))
        deductions.append(key)

    if deductions:
        gross = indicator("GROSS", "產品收入 − 原料成本", f"{revenue} - {feed}", hidden=False)
        components.insert(2, Component("產品收入 − 原料成本", gross, "indicator", +1, "subtotal"))
    margin = indicator("MARGIN", "毛利", " - ".join([revenue, feed, *deductions]), hidden=False)
    components.append(Component("毛利", margin, "indicator", +1, "total"))

    return params, indicators, RefinerySpec(m, margin, revenue, feed, tuple(products), tuple(feeds), tuple(components))


class _Engine(Protocol):
    def value_at(self, name: str, on: date) -> float | None: ...


def breakdown(engine: _Engine, spec: RefinerySpec, on: date) -> dict:
    """某一天的毛利拆解：逐項加減項，以及產品、原料明細（權重 × 價格 = 金額）。缺值的項目值為 None。"""

    def lines(items: tuple[Line, ...]) -> list[dict]:
        out = []
        for line in items:
            weight, value = engine.value_at(line.weight_param, on), engine.value_at(line.value_indicator, on)
            tax = engine.value_at(line.consumption_tax, on) if line.consumption_tax else None
            out.append({"code": line.code, "name_zh": line.name_zh, "weight_param": line.weight_param,
                        "weight": weight, "price_formula": line.price_formula, "value": value,
                        "price": value / weight if value is not None and weight else None,
                        "indicator": line.value_indicator, "consumption_tax": tax})
        return out

    return {"date": on.isoformat(),
            "components": [{"label": c.label, "ref": c.ref, "ref_kind": c.ref_kind, "sign": c.sign,
                            "category": c.category, "value": engine.value_at(c.ref, on)} for c in spec.components],
            "products": lines(spec.products), "feeds": lines(spec.feeds)}
