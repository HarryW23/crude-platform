// 圖表。全部按容器的實際像素寬度繪製（寬度變了會重畫），所以文字大小不隨圖表縮放。
// 配色：
//   單序列走勢               --series-1
//   正負兩極（瀑布、平衡柱） --series-1 為正 / 加項，--series-2 為負 / 減項，小計與合計用中性灰
//   季節性                   當前值 --series-1；歷史區間與均值用中性灰（它們是背景參照，不是另一個類別）
//   散點                     歷史樣本中性灰，最新一點 --series-2，擬合線 --series-1
//   期貨曲線                 今天 --series-1 實線；1 周前 --series-2 虛線；1 個月前中性灰點線
// 數值一律同時用文字或提示標出，不只靠顏色區分。
import { esc, num } from "./ui.js";

// 建一個隨容器寬度重畫的圖表容器。draw(box, width) 負責填內容。
function responsive(draw, minWidth = 320) {
  const box = document.createElement("div");
  box.className = "chart";
  let last = 0;
  new ResizeObserver(() => {
    const width = Math.max(Math.floor(box.clientWidth), minWidth);
    if (width !== last) { last = width; draw(box, width); }
  }).observe(box);
  return box;
}

function empty(message) {
  const box = document.createElement("div");
  box.className = "chart";
  box.innerHTML = `<p class="muted">${esc(message)}</p>`;
  return box;
}

function scale(values, pad = 0.06) {
  let lo = Math.min(...values), hi = Math.max(...values);
  if (lo === hi) { lo -= 1; hi += 1; }
  const margin = (hi - lo) * pad;
  return [lo - margin, hi + margin];
}

// 數值範圍大時不顯示小數，避免軸標籤過長
const axisDigits = (lo, hi) => Math.max(Math.abs(lo), Math.abs(hi)) >= 200 ? 0 : 2;

function ticks(lo, hi, count = 4) {
  return Array.from({ length: count + 1 }, (_, i) => lo + (hi - lo) * i / count);
}

function placeTip(tip, box, x, y) {
  tip.hidden = false;
  tip.style.left = `${Math.min(Math.max(x - tip.offsetWidth / 2, 0), box.clientWidth - tip.offsetWidth)}px`;
  tip.style.top = `${Math.max(y - tip.offsetHeight - 10, 0)}px`;
}

// ------------------------------ 走勢線 ------------------------------
// points: [{date, value}]；markers: [{date, label}] 畫成豎虛線（例如換月日），提示框裡也會寫出來
export function lineChart(points, { unit = "", height = 240, markers = [] } = {}) {
  if (points.length < 2) return empty("數據點不足，無法畫圖。");
  return responsive((box, W) => {
    const H = height, L = 66, R = 14, T = 14, B = 26;
    const [lo, hi] = scale(points.map((p) => p.value)), d = axisDigits(lo, hi);
    const x = (i) => L + (W - L - R) * i / (points.length - 1);
    const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join("");
    const at = Object.fromEntries(points.map((p, i) => [p.date, i]));
    const marks = {};
    for (const m of markers) {                 // 標記日當天沒有數據點時，標在之後的第一個數據點上
      const hit = points.find((p) => p.date >= m.date);
      if (hit && (hit.date === m.date || at[hit.date] > 0)) (marks[hit.date] ??= []).push(m.label);
    }
    box.innerHTML = `<svg width="${W}" height="${H}" role="img" aria-label="走勢圖，${points[0].date} 至 ${points.at(-1).date}">
        ${Object.keys(marks).map((d) => `<line x1="${x(at[d])}" x2="${x(at[d])}" y1="${T}" y2="${H - B}" stroke="var(--neutral-bar)" stroke-dasharray="2 3" opacity="0.6"/>`).join("")}
        ${ticks(lo, hi).map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--hairline)"/>
          <text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${num(v, d)}</text>`).join("")}
        ${lo < 0 && hi > 0 ? `<line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" stroke="var(--axis)"/>` : ""}
        <path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>
        <line class="cross" y1="${T}" y2="${H - B}" stroke="var(--axis)" visibility="hidden"/>
        <circle class="dot" r="4" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>
        <text x="${L}" y="${H - 7}">${points[0].date}</text>
        <text x="${W - R}" y="${H - 7}" text-anchor="end">${points.at(-1).date}</text>
        <text x="${W - R}" y="${T - 3}" text-anchor="end">${esc(unit)}</text>
        <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
      </svg><div class="tip" hidden></div>`;
    const cross = box.querySelector(".cross"), dot = box.querySelector(".dot"), tip = box.querySelector(".tip");
    const hit = box.querySelector(".hit");
    hit.addEventListener("pointermove", (ev) => {
      const px = ev.clientX - box.getBoundingClientRect().left;
      const i = Math.max(0, Math.min(points.length - 1, Math.round((px - L) / (W - L - R) * (points.length - 1))));
      const p = points[i];
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(p.value)); dot.setAttribute("visibility", "visible");
      tip.innerHTML = `${p.date}　<b>${num(p.value)}</b> ${esc(unit)}` +
        (marks[p.date] ? `<br><span class="muted">${esc(marks[p.date].join("、"))}</span>` : "");
      placeTip(tip, box, x(i), y(p.value));
    });
    hit.addEventListener("pointerleave", () => {
      cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true;
    });
  });
}

// ------------------------------ 瀑布（毛利拆解） ------------------------------
// components: [{label, sign, category, value}]。加項向右、減項向左，小計與合計從零起算。
export function waterfall(components, { unit = "" } = {}) {
  if (components.some((c) => c.value == null)) return empty("有項目缺值，無法畫拆解圖。缺的項目見下表。");
  const rows = [];
  let running = 0;
  for (const c of components) {
    const total = c.category === "subtotal" || c.category === "total";
    const to = total ? c.value : running + c.sign * c.value;
    rows.push({ ...c, from: total ? 0 : running, to, shown: total ? c.value : c.sign * c.value,
                fill: total ? "var(--neutral-bar)" : c.sign > 0 ? "var(--series-1)" : "var(--series-2)" });
    running = to;
  }
  const lo = Math.min(0, ...rows.map((r) => Math.min(r.from, r.to)));
  const hi = Math.max(0, ...rows.map((r) => Math.max(r.from, r.to)));
  return responsive((box, W) => {
    const rowH = 30, L = 150, R = 78, T = 4, H = T + rows.length * rowH + 4;
    const x = (v) => L + (W - L - R) * (v - lo) / ((hi - lo) || 1);
    box.innerHTML = `<div class="legend"><span><i style="background:var(--series-1)"></i>加項</span>
        <span><i style="background:var(--series-2)"></i>減項</span>
        <span><i style="background:var(--neutral-bar)"></i>小計 / 毛利</span><span>${esc(unit)}</span></div>
      <svg width="${W}" height="${H}" role="img" aria-label="毛利拆解">
        <line x1="${x(0)}" x2="${x(0)}" y1="${T}" y2="${H - 4}" stroke="var(--axis)"/>
        ${rows.map((r, i) => {
          const y0 = T + i * rowH + 6, a = Math.min(x(r.from), x(r.to)), w = Math.max(Math.abs(x(r.to) - x(r.from)), 1);
          return `<g><title>${esc(r.label)}：${num(r.shown)} ${esc(unit)}</title>
            <text x="${L - 10}" y="${y0 + 13}" text-anchor="end" style="fill:var(--ink-2)">${esc(r.label)}</text>
            <rect x="${a}" y="${y0}" width="${w}" height="${rowH - 12}" rx="3" fill="${r.fill}"/>
            <text x="${a + w + 8}" y="${y0 + 13}" style="fill:var(--ink)">${num(r.shown, 0)}</text>
            ${i < rows.length - 1 && r.category !== "total" ? `<line x1="${x(r.to)}" x2="${x(r.to)}" y1="${y0 + rowH - 12}" y2="${y0 + rowH}" stroke="var(--axis)"/>` : ""}</g>`;
        }).join("")}</svg>`;
  });
}

// ------------------------------ 正負柱（各期平衡） ------------------------------
// bars: [{label, value, base, forecast}]。base 與 value 不同時畫一條細線標出基準位置。
export function balanceBars(bars, { unit = "", height = 220 } = {}) {
  const usable = bars.filter((b) => b.value != null);
  if (!usable.length) return empty("沒有可以畫的期間。");
  return responsive((box, W) => {
    const H = height, L = 66, R = 10, T = 22, B = 38;
    const all = usable.flatMap((b) => [b.value, b.base ?? b.value, 0]);
    const [lo, hi] = scale(all, 0.1), d = axisDigits(lo, hi);
    const slot = (W - L - R) / bars.length, bw = Math.min(Math.max(slot - 6, 4), 46);
    const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    const firstForecast = bars.findIndex((b) => b.forecast);
    box.innerHTML = `<div class="legend"><span><i style="background:var(--series-1)"></i>累庫（供應 > 需求）</span>
        <span><i style="background:var(--series-2)"></i>去庫</span>
        ${bars.some((b) => b.base != null && b.base !== b.value) ? '<span><i style="background:var(--ink);height:2px;vertical-align:3px"></i>基準</span>' : ""}
        <span>${esc(unit)}</span></div>
      <svg width="${W}" height="${H}" role="img" aria-label="各期平衡">
        ${firstForecast > 0 ? `<rect x="${L + slot * firstForecast}" y="${T - 14}" width="${W - R - L - slot * firstForecast}" height="${H - T - B + 14}" fill="var(--soft)"/>
          <text x="${L + slot * firstForecast + 6}" y="${T - 3}">預測期</text>` : ""}
        ${ticks(lo, hi).map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--hairline)"/>
          <text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${num(v, d)}</text>`).join("")}
        <line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" stroke="var(--axis)"/>
        ${bars.map((b, i) => {
          const cx = L + slot * (i + 0.5);
          const label = i % Math.ceil(bars.length / Math.max(Math.floor((W - L - R) / 62), 1)) === 0
            ? `<text x="${cx}" y="${H - B + 16}" text-anchor="middle">${esc(b.label)}</text>` : "";
          if (b.value == null) return label;
          const top = Math.min(y(b.value), y(0)), h = Math.max(Math.abs(y(b.value) - y(0)), 1);
          return `<g><title>${esc(b.label)}：${num(b.value)} ${esc(unit)}${b.base != null && b.base !== b.value ? `（基準 ${num(b.base)}）` : ""}</title>
            <rect x="${cx - bw / 2}" y="${top}" width="${bw}" height="${h}" rx="3" fill="${b.value >= 0 ? "var(--series-1)" : "var(--series-2)"}"/>
            ${b.base != null && b.base !== b.value ? `<line x1="${cx - bw / 2 - 2}" x2="${cx + bw / 2 + 2}" y1="${y(b.base)}" y2="${y(b.base)}" stroke="var(--ink)" stroke-width="2"/>` : ""}</g>${label}`;
        }).join("")}</svg>`;
  });
}

// ------------------------------ 季節性區間 ------------------------------
// points: [{date, value, min, max, avg}]；min / max / avg 可能為 null（湊不齊年數）。
export function seasonalChart(points, { unit = "", years = 5, height = 260 } = {}) {
  const usable = points.filter((p) => p.value != null);
  if (usable.length < 2) return empty("數據點不足，無法畫圖。");
  const banded = usable.filter((p) => p.avg != null);
  return responsive((box, W) => {
    const H = height, L = 66, R = 14, T = 14, B = 26;
    const [lo, hi] = scale(usable.flatMap((p) => p.avg == null ? [p.value] : [p.value, p.min, p.max]));
    const x = (i) => L + (W - L - R) * i / (usable.length - 1);
    const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    const line = (key, rows) => rows.map((p, k) => `${k ? "L" : "M"}${x(usable.indexOf(p)).toFixed(1)},${y(p[key]).toFixed(1)}`).join("");
    const band = banded.length > 1
      ? `M${banded.map((p) => `${x(usable.indexOf(p)).toFixed(1)},${y(p.max).toFixed(1)}`).join("L")}L${[...banded].reverse().map((p) => `${x(usable.indexOf(p)).toFixed(1)},${y(p.min).toFixed(1)}`).join("L")}Z` : "";
    box.innerHTML = `<div class="legend"><span><i style="background:var(--series-1)"></i>當前</span>
        <span><i style="background:var(--muted)"></i>前 ${years} 年同期均值</span>
        <span><i style="background:var(--band)"></i>前 ${years} 年同期區間</span><span>${esc(unit)}</span></div>
      <svg width="${W}" height="${H}" role="img" aria-label="季節性走勢">
        ${ticks(lo, hi).map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--hairline)"/>
          <text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${num(v, 0)}</text>`).join("")}
        ${band ? `<path d="${band}" fill="var(--band)"/>
          <path d="${line("avg", banded)}" fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="5 4"/>` : ""}
        <path d="${line("value", usable)}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>
        <line class="cross" y1="${T}" y2="${H - B}" stroke="var(--axis)" visibility="hidden"/>
        <circle class="dot" r="4" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>
        <text x="${L}" y="${H - 7}">${usable[0].date}</text><text x="${W - R}" y="${H - 7}" text-anchor="end">${usable.at(-1).date}</text>
        <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
      </svg><div class="tip" hidden></div>
      ${banded.length ? "" : `<p class="muted">歷史不足 ${years} 年，無法給出同期區間。</p>`}`;
    const cross = box.querySelector(".cross"), dot = box.querySelector(".dot"), tip = box.querySelector(".tip");
    const hit = box.querySelector(".hit"), svgTop = () => box.querySelector("svg").getBoundingClientRect().top - box.getBoundingClientRect().top;
    hit.addEventListener("pointermove", (ev) => {
      const px = ev.clientX - box.getBoundingClientRect().left;
      const i = Math.max(0, Math.min(usable.length - 1, Math.round((px - L) / (W - L - R) * (usable.length - 1))));
      const p = usable[i];
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(p.value)); dot.setAttribute("visibility", "visible");
      tip.innerHTML = `${p.date}　<b>${num(p.value, 0)}</b>` + (p.avg == null ? "" :
        `　均值 ${num(p.avg, 0)}　區間 ${num(p.min, 0)} – ${num(p.max, 0)}`);
      placeTip(tip, box, x(i), svgTop() + y(p.value));
    });
    hit.addEventListener("pointerleave", () => {
      cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true;
    });
  });
}

// ------------------------------ 散點 + 擬合線 ------------------------------
// fit: /relations/{code} 的結果。
export function scatterChart(fit, { height = 300 } = {}) {
  const pts = fit.points;
  if (pts.length < 2) return empty("樣本不足，無法畫圖。");
  return responsive((box, W) => {
    const H = height, L = 60, R = 16, T = 14, B = 40;
    const [xlo, xhi] = scale(pts.map((p) => p.x)), [ylo, yhi] = scale(pts.map((p) => p.y));
    const x = (v) => L + (W - L - R) * (v - xlo) / (xhi - xlo);
    const y = (v) => T + (H - T - B) * (1 - (v - ylo) / (yhi - ylo));
    const last = pts.at(-1);
    const fitLine = fit.status === "ok"
      ? `<line x1="${x(xlo)}" y1="${y(fit.intercept + fit.slope * xlo)}" x2="${x(xhi)}" y2="${y(fit.intercept + fit.slope * xhi)}" stroke="var(--series-1)" stroke-width="2"/>` : "";
    box.innerHTML = `<div class="legend"><span><i style="background:var(--muted);border-radius:50%"></i>歷史樣本</span>
        <span><i style="background:var(--series-2);border-radius:50%"></i>最新（${last.date}）</span>
        ${fit.status === "ok" ? '<span><i style="background:var(--series-1);height:2px;vertical-align:3px"></i>擬合線</span>' : ""}</div>
      <svg width="${W}" height="${H}" role="img" aria-label="散點圖">
        <defs><clipPath id="clip-${fit.code}"><rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}"/></clipPath></defs>
        ${ticks(ylo, yhi).map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--hairline)"/>
          <text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${num(v)}</text>`).join("")}
        ${ticks(xlo, xhi).map((v) => `<text x="${x(v)}" y="${H - B + 16}" text-anchor="middle">${num(v, 0)}</text>`).join("")}
        <text x="${(L + W - R) / 2}" y="${H - 6}" text-anchor="middle">${esc(fit.x.name_zh)}（${esc(fit.x.unit)}）</text>
        <text x="${L}" y="${T - 3}">${esc(fit.y.name_zh)}（${esc(fit.y.unit)}）</text>
        <g clip-path="url(#clip-${fit.code})">${fitLine}</g>
        ${pts.slice(0, -1).map((p) => `<circle cx="${x(p.x)}" cy="${y(p.y)}" r="3.5" fill="var(--muted)" fill-opacity=".55"><title>${p.date}：x ${num(p.x, 0)}，y ${num(p.y)}</title></circle>`).join("")}
        <circle cx="${x(last.x)}" cy="${y(last.y)}" r="6" fill="var(--series-2)" stroke="var(--surface)" stroke-width="2"><title>${last.date}：x ${num(last.x, 0)}，y ${num(last.y)}</title></circle>
      </svg>`;
  });
}

// ------------------------------ 兩條線（價格與公允價值） ------------------------------
// lines: [{name, color, points: [{date, value}]}]，各條線的日期相同或是子集。第一條畫實線，其餘虛線。
export function pairChart(lines, { unit = "", height = 260 } = {}) {
  const dates = [...new Set(lines.flatMap((l) => l.points.map((p) => p.date)))].sort();
  if (dates.length < 2) return empty("數據點不足，無法畫圖。");
  const at = Object.fromEntries(dates.map((d, i) => [d, i]));
  return responsive((box, W) => {
    const H = height, L = 66, R = 14, T = 14, B = 26;
    const [lo, hi] = scale(lines.flatMap((l) => l.points.map((p) => p.value)));
    const x = (i) => L + (W - L - R) * i / (dates.length - 1);
    const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    const path = (pts) => {
      let out = "", gap = true;
      for (const p of pts) { out += `${gap ? "M" : "L"}${x(at[p.date]).toFixed(1)},${y(p.value).toFixed(1)}`; gap = false; }
      return out;
    };
    box.innerHTML = `<div class="legend">${lines.map((l, k) => `<span><i style="background:${l.color};${k ? "height:2px;vertical-align:3px" : ""}"></i>${esc(l.name)}</span>`).join("")}<span>${esc(unit)}</span></div>
      <svg width="${W}" height="${H}" role="img" aria-label="${esc(lines.map((l) => l.name).join("與"))}">
        ${ticks(lo, hi).map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--hairline)"/>
          <text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${num(v)}</text>`).join("")}
        ${lines.map((l, k) => `<path d="${path(l.points)}" fill="none" stroke="${l.color}" stroke-width="2" ${k ? 'stroke-dasharray="6 4"' : ""}/>`).join("")}
        <line class="cross" y1="${T}" y2="${H - B}" stroke="var(--axis)" visibility="hidden"/>
        <text x="${L}" y="${H - 7}">${dates[0]}</text><text x="${W - R}" y="${H - 7}" text-anchor="end">${dates.at(-1)}</text>
        <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
      </svg><div class="tip" hidden></div>`;
    const cross = box.querySelector(".cross"), tip = box.querySelector(".tip"), hit = box.querySelector(".hit");
    const svgTop = () => box.querySelector("svg").getBoundingClientRect().top - box.getBoundingClientRect().top;
    const byDate = lines.map((l) => Object.fromEntries(l.points.map((p) => [p.date, p.value])));
    hit.addEventListener("pointermove", (ev) => {
      const px = ev.clientX - box.getBoundingClientRect().left;
      const i = Math.max(0, Math.min(dates.length - 1, Math.round((px - L) / (W - L - R) * (dates.length - 1))));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      tip.innerHTML = `${dates[i]}` + lines.map((l, k) => `　${esc(l.name)} <b>${num(byDate[k][dates[i]])}</b>`).join("");
      placeTip(tip, box, x(i), svgTop() + T + 20);
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
  });
}

// ------------------------------ 期貨曲線 ------------------------------
// snapshots: [{label, date, points: [{month, value|null}]}]。橫軸是合約月份。
// 缺的月份不畫點、不插值；線只把有數據的點按月份順序連起來，每個有數據的點都畫出來，看得出哪裡是缺口。
const CURVE_STYLE = [
  { color: "var(--series-1)", dash: "", width: 2.5 },
  { color: "var(--series-2)", dash: "6 4", width: 2 },
  { color: "var(--neutral-bar)", dash: "2 4", width: 2 },
];

export function curveChart(snapshots, months, { unit = "", height = 280 } = {}) {
  const values = snapshots.flatMap((s) => s.points.map((p) => p.value)).filter((v) => v !== null);
  if (values.length < 2 || months.length < 2) return empty("數據點不足，無法畫曲線。");
  return responsive((box, W) => {
    const H = height, L = 66, R = 14, T = 14, B = 26;
    const [lo, hi] = scale(values), d = axisDigits(lo, hi);
    const x = (i) => L + (W - L - R) * i / (months.length - 1);
    const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    const idx = Object.fromEntries(months.map((m, i) => [m, i]));
    const path = (pts) => {
      const have = pts.filter((p) => p.value !== null);
      return have.map((p, i) => `${i ? "L" : "M"}${x(idx[p.month]).toFixed(1)},${y(p.value).toFixed(1)}`).join("");
    };
    const style = (k) => CURVE_STYLE[Math.min(k, CURVE_STYLE.length - 1)];
    box.innerHTML = `<div class="legend">${snapshots.map((s, k) => `<span><i style="background:${style(k).color};${k ? "height:2px;vertical-align:3px" : ""}"></i>${esc(s.label)}（${s.date}）</span>`).join("")}<span>${esc(unit)}</span></div>
      <svg width="${W}" height="${H}" role="img" aria-label="期貨曲線，${esc(snapshots.map((s) => s.label).join("、"))}">
        ${ticks(lo, hi).map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--hairline)"/>
          <text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${num(v, d)}</text>`).join("")}
        ${months.map((m, i) => `<text x="${x(i)}" y="${H - 7}" text-anchor="middle">M${m}</text>`).join("")}
        ${snapshots.map((s, k) => `<path d="${path(s.points)}" fill="none" stroke="${style(k).color}" stroke-width="${style(k).width}" stroke-dasharray="${style(k).dash}" stroke-linejoin="round"/>`).reverse().join("")}
        ${snapshots.map((s, k) => s.points.filter((p) => p.value !== null).map((p) => `<circle cx="${x(idx[p.month])}" cy="${y(p.value)}" r="${k ? 2.5 : 3.5}" fill="${style(k).color}" stroke="var(--surface)" stroke-width="1.5"/>`).join("")).reverse().join("")}
        <line class="cross" y1="${T}" y2="${H - B}" stroke="var(--axis)" visibility="hidden"/>
        <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
      </svg><div class="tip" hidden></div>`;
    const cross = box.querySelector(".cross"), tip = box.querySelector(".tip"), hit = box.querySelector(".hit");
    const svgTop = () => box.querySelector("svg").getBoundingClientRect().top - box.getBoundingClientRect().top;
    hit.addEventListener("pointermove", (ev) => {
      const px = ev.clientX - box.getBoundingClientRect().left;
      const i = Math.max(0, Math.min(months.length - 1, Math.round((px - L) / (W - L - R) * (months.length - 1))));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      tip.innerHTML = `<b>M${months[i]}</b>` + snapshots.map((s) => {
        const pt = s.points.find((p) => p.month === months[i]), v = pt?.value ?? null;
        const contract = pt?.contract ? ` <span class="muted">${pt.contract}</span>` : "";
        return `<br>${esc(s.label)}${contract}　${v === null ? '<span class="muted">無數據</span>' : `<b>${num(v)}</b>`}`;
      }).join("");
      const anchor = snapshots[0].points.find((p) => p.month === months[i])?.value ?? null;
      placeTip(tip, box, x(i), svgTop() + (anchor === null ? (H - B) / 2 : y(anchor)));
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
  });
}
