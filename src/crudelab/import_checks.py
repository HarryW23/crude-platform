"""導入前對數據的檢查（imports.preview_file / commit_batch 用）。

  screen               明顯不能用的點直接去掉：絕對價格正好是 0、data_from 之前、bad_days 裡的
  scale_warnings       單位核對：和平台實際採用的值差 20 倍以上
  consistency_warnings 歷史一致性：重新導入同一段歷史時，舊的日子普遍差 10% 以上（篩選條件和上次不一樣）
  quality_warnings     重複報價（repeats.py）、單日錯價（spikes.py）
後兩類以外都只提醒、不擋導入；單位和一致性兩種在收件夾自動導入時不收（見 jobs._import_folder）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ingest import Obs
from .models import Series
from .schema import PRICE_KINDS
from .settings import Settings

LEVEL_KINDS = ("futures", "swap", "spot", "freight", "fx", "stock", "flow", "position")   # 數值本身是量級的類型
SCALE_FACTOR = 20             # 新數據的中位數和庫裡已有數據差這麼多倍以上，多半是單位錯了


def _align_current_period(rows: dict, observations: list[Obs]) -> tuple[list[Obs], list[str]]:
    """「當月」這類 RIC（lseg.period = current）每天給一個數：歸到所屬期間的第一天，一個期間留一個數
    （取這個期間裡最後一天的；期間中途改過的，說明裡寫出來）。其他序列原樣返回。"""
    from dataclasses import replace

    def start(day: date, frequency: str) -> date:
        month = day.month if frequency == "monthly" else 3 * ((day.month - 1) // 3) + 1
        return date(day.year, month, 1)

    current = {code for code, row in rows.items()
               if row.within_period and (row.external_ids or {}).get("lseg_period") == "current"}
    if not current:
        return observations, []
    out: list[Obs] = []
    latest: dict[tuple[str, date], list[Obs]] = {}
    for o in observations:
        if o.series_code not in current:
            out.append(o)
            continue
        key = (o.series_code, start(o.obs_date, rows[o.series_code].frequency))
        latest.setdefault(key, []).append(o)
    notes: list[str] = []
    changed: dict[str, list[str]] = {}
    for (code, period), points in sorted(latest.items()):
        points.sort(key=lambda o: o.obs_date)
        out.append(replace(points[-1], obs_date=period))
        if len({p.value for p in points}) > 1:
            changed.setdefault(code, []).append(f"{period:%Y-%m}")
    periods: dict[str, int] = {}
    for c, _ in latest:
        periods[c] = periods.get(c, 0) + 1
    for code, n in sorted(periods.items()):
        note = f"{code}：每天的「當期」數歸到所屬期間的第一天，共 {n} 期"
        if changed.get(code):
            note += f"；{'、'.join(changed[code][:4])} 期間中途數字變過，取的是期間裡最後一天的"
        notes.append(note)
    return out, notes


def screen(session: Session, observations: list[Obs]) -> tuple[list[Obs], list[str]]:
    """去掉文件裡明顯不能用的數據點，回傳（留下的，說明）。預覽和入庫用同一個規則，預覽裡會寫明去掉了什麼：
    - 序列的 data_from 之前的（合約改過規格，見 SeriesDef.data_from）
    - 序列的 bad_days 裡的：核對過是錯的日子
    - 絕對價格正好是 0 的：路透在沒有報價的日子會填 0（例如阿曼期貨 2022 年 6 月連續十幾天是 0）。
      升貼水、官價、比率是 0 是正常的，不去掉；WTI 2020-04-20 的負價也不受影響"""
    codes = {o.series_code for o in observations}
    rows = {r.code: r for r in session.scalars(select(Series).where(Series.code.in_(codes)))} if codes else {}
    early: dict[str, int] = {}
    zero: dict[str, list[date]] = {}
    bad: dict[str, list[date]] = {}
    kept = []
    observations, aligned = _align_current_period(rows, observations)
    for o in observations:
        row = rows.get(o.series_code)
        if row is not None and row.data_from is not None and o.obs_date < row.data_from:
            early[o.series_code] = early.get(o.series_code, 0) + 1
        elif row is not None and row.bad_days and o.obs_date.isoformat() in row.bad_days:
            bad.setdefault(o.series_code, []).append(o.obs_date)
        elif row is not None and row.kind in PRICE_KINDS and o.value == 0:
            zero.setdefault(o.series_code, []).append(o.obs_date)
        else:
            kept.append(o)
    notes = aligned + [f"{code}：{rows[code].data_from} 之前的 {n} 個數據點不導入"
                       "（這條序列只用這天之後的數據，見配置裡的 data_from）" for code, n in sorted(early.items())]
    notes += [f"{code}：{'、'.join(str(d) for d in sorted(days))} 核對過是錯價，不導入（見配置裡的 bad_days）"
              for code, days in sorted(bad.items())]
    for code, days in sorted(zero.items()):
        shown = "、".join(str(d) for d in sorted(days)[:5]) + (f" 等 {len(days)} 天" if len(days) > 5 else "")
        notes.append(f"{code}（{rows[code].name_zh}）有 {len(days)} 個值正好是 0，不導入：{shown}。"
                     "價格不會是 0，這是數據商沒有報價的日子；那幾天當作沒有數據")
    return kept, notes




CONSISTENCY_LIMIT = 0.10      # 和庫裡同一批日子的數普遍相差超過 10%：多半是篩選條件不一樣，不是修訂
RECENT_PERIODS = 6            # 最近幾期本來就會修訂（Kpler 流量、EIA、月報），不算進一致性核對


def consistency_warnings(session: Session, observations: list[Obs]) -> list[str]:
    """重新導入同一段歷史時，舊的日子應該和庫裡一樣（修訂只動最近幾期）。普遍差了 10% 以上，
    多半是下載時篩選條件不一樣：商品選了全部油品而不是原油、區域不對、單位是千噸不是千桶、浮倉天數門檻改了……
    這種錯單位核對（差 20 倍才提醒）攔不住。比的是平台實際採用的值，最近 RECENT_PERIODS 期不比。"""
    from collections import defaultdict
    from statistics import median

    from .ingest import load_points
    new: dict[str, dict] = defaultdict(dict)
    for o in observations:
        new[o.series_code][o.obs_date] = o.value
    rows = {r.code: r for r in session.scalars(select(Series).where(Series.code.in_(list(new))))} if new else {}
    out = []
    for code, points in sorted(new.items()):
        row = rows.get(code)
        if row is None or row.kind not in LEVEL_KINDS:
            continue
        effective = load_points(session, [code])
        old = dict(zip(effective["obs_date"], effective["value"], strict=True))
        common = sorted(d for d in points if d in old and old[d] != 0)[:-RECENT_PERIODS]
        if len(common) < 6:
            continue
        gap = median(abs(points[d] / old[d] - 1) for d in common)
        if gap > CONSISTENCY_LIMIT:
            out.append(f"{code}（{row.name_zh}）：和庫裡已有的 {len(common)} 個相同日子比，數字普遍相差 {gap:.0%}"
                       f"（{common[0]} 到 {common[-1]}；最近 {RECENT_PERIODS} 期本來會修訂，沒算）。"
                       "舊的日子不該變這麼多，多半是下載時的篩選條件和上次不一樣（商品、區域、單位、拆分方式）。"
                       "確認之前不要導入")
    return out


def _amount(x: float) -> str:
    return f"{x:,.0f}" if x >= 100 else f"{x:.4g}"


def scale_warnings(session: Session, observations: list[Obs]) -> list[str]:
    """單位核對：新數據和庫裡這條序列已有的數據（其他來源的也算）差了幾十倍以上時提醒。
    典型是單位錯了：Kpler 插件給的是桶而不是千桶、噸而不是千噸，差 1000 倍；美分和美元差 100 倍。
    比的是平台實際採用的值（同一天有幾個來源時取優先級最高的，和查價一致）：低優先級的演示數據等不能當比較基準。
    比的是同一批日子：兩邊都有的日期上逐日相比取中位數；沒有重疊（例如只導新的一段）時，
    比兩邊各自最近 60 個點。不能拿一大段歷史去比最近的水平：運費這種一年裡能漲十幾倍的，會誤報。
    升貼水、官價、比率會在 0 附近正負來回，不查。"""
    from collections import defaultdict
    from statistics import median

    from .ingest import load_points
    new: dict[str, dict] = defaultdict(dict)
    for o in observations:
        new[o.series_code][o.obs_date] = abs(o.value)
    rows = {r.code: r for r in session.scalars(select(Series).where(Series.code.in_(list(new))))} if new else {}
    out = []
    for code, points in sorted(new.items()):
        row = rows.get(code)
        if row is None or row.kind not in LEVEL_KINDS or len(points) < 3:
            continue
        effective = load_points(session, [code])               # 每天一個值：優先級最高的來源
        old = {d: abs(v) for d, v in zip(effective["obs_date"], effective["value"], strict=True)}
        common = [d for d in points if d in old and old[d] > 0 and points[d] > 0]
        if len(common) >= 3:
            ratio = median(points[d] / old[d] for d in common)
            a, b = median(points[d] for d in common), median(old[d] for d in common)
        else:
            recent = [old[d] for d in sorted(old)[-60:]]
            if len(recent) < 3:
                continue
            a, b = median(points[d] for d in sorted(points)[-60:]), median(recent)
            if a <= 0 or b <= 0:
                continue
            ratio = a / b
        if ratio >= SCALE_FACTOR or ratio <= 1 / SCALE_FACTOR:
            times = f"{ratio:,.0f} 倍" if ratio >= 1 else f"1/{1 / ratio:,.0f}"
            out.append(f"{code}（{row.name_zh}）這批數據的中位數是 {_amount(a)}，庫裡已有的是 {_amount(b)}，"
                       f"差了約 {times}：單位多半不對（這條序列的單位是 {row.unit_code}）。確認之前不要導入")
    return out


def quality_warnings(settings: Settings, observations: list[Obs]) -> list[str]:
    """文件裡的數據質量提醒（不擋導入）：
    - 很長一段相同的值：數據商在沿用舊值（repeats.py，規則見 config/freshness.yaml 的 repeats）
    - 單日錯價：跳上去又回來、同一天其他序列沒一起跳（spikes.py）"""
    if not observations:
        return []
    from . import repeats, spikes
    from .config import load_config
    cfg = load_config(settings.config_dir)
    by_series: dict[str, list[tuple[date, float]]] = {}
    for o in observations:
        by_series.setdefault(o.series_code, []).append((o.obs_date, o.value))
    daily = {c: p for c, p in by_series.items() if c in cfg.series and cfg.series[c].frequency == "daily"}
    kinds = {c: cfg.series[c].kind for c in daily}
    names = {c: cfg.series[c].name_zh for c in daily}
    commodity = {c: cfg.series[c].commodity for c in daily}
    return repeats.import_warnings(cfg, by_series) + spikes.import_warnings(
        kinds, names, daily, commodity, cfg.freshness.spikes.confirmed)
