// 圖表。只有兩種：單序列走勢線、毛利拆解的瀑布條。
// 配色：走勢線只有一個序列，用 --series-1；瀑布條按正負兩極用 --series-1（加項）和 --series-2（減項），
// 小計和合計用中性灰。數值一律用文字標出，不只靠顏色區分。
import { esc, num } from "./ui.js";

const SVG = "http://www.w3.org/2000/svg";

// points: [{date, value}]。回傳一個可插入頁面的元素，帶十字線和提示。
export function lineChart(points, { unit = "", height = 240 } = {}) {
  const box = document.createElement("div");
  box.className = "chart";
  if (points.length < 2) {
    box.innerHTML = '<p class="muted">數據點不足，無法畫圖。</p>';
    return box;
  }
  const W = 1100, H = height, L = 58, R = 14, T = 12, B = 26;
  const values = points.map((p) => p.value);
  let lo = Math.min(...values), hi = Math.max(...values);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.06;
  lo -= pad; hi += pad;
  const x = (i) => L + (W - L - R) * i / (points.length - 1);
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join("");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => lo + (hi - lo) * f);
  const grid = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--hairline)"/>
    <text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${num(v)}</text>`).join("");
  const zero = lo < 0 && hi > 0
    ? `<line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" stroke="var(--axis)"/>` : "";
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="走勢圖，${points[0].date} 至 ${points.at(-1).date}">
      ${grid}${zero}
      <path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>
      <line class="cross" y1="${T}" y2="${H - B}" stroke="var(--axis)" visibility="hidden"/>
      <circle class="dot" r="4" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>
      <text x="${L}" y="${H - 7}">${points[0].date}</text>
      <text x="${W - R}" y="${H - 7}" text-anchor="end">${points.at(-1).date}</text>
      <text x="${W - R}" y="${T + 2}" text-anchor="end">${esc(unit)}</text>
      <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
    </svg><div class="tip" hidden></div>`;
  const svg = box.querySelector("svg"), cross = box.querySelector(".cross"), dot = box.querySelector(".dot");
  const tip = box.querySelector(".tip");
  const hide = () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true; };
  box.querySelector(".hit").addEventListener("pointermove", (ev) => {
    const rect = svg.getBoundingClientRect(), scale = rect.width / W;
    const i = Math.max(0, Math.min(points.length - 1,
      Math.round(((ev.clientX - rect.left) / scale - L) / (W - L - R) * (points.length - 1))));
    const p = points[i];
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(p.value)); dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.innerHTML = `${p.date}　<b>${num(p.value)}</b> ${esc(unit)}`;
    const left = x(i) * scale;
    tip.style.left = `${Math.min(Math.max(left - tip.offsetWidth / 2, 0), rect.width - tip.offsetWidth)}px`;
    tip.style.top = `${Math.max(y(p.value) * scale - tip.offsetHeight - 10, 0)}px`;
  });
  box.querySelector(".hit").addEventListener("pointerleave", hide);
  return box;
}

// components: [{label, sign, category, value}]。橫向瀑布：加項向右、減項向左，小計與合計從零起算。
export function waterfall(components, { unit = "" } = {}) {
  const box = document.createElement("div");
  box.className = "chart";
  if (components.some((c) => c.value == null)) {
    box.innerHTML = '<p class="muted">有項目缺值，無法畫拆解圖。缺的項目見下表。</p>';
    return box;
  }
  const rows = [];
  let running = 0;
  for (const c of components) {
    if (c.category === "subtotal" || c.category === "total") {
      rows.push({ ...c, from: 0, to: c.value, fill: "var(--neutral-bar)" });
      running = c.value;
    } else {
      const to = running + c.sign * c.value;
      rows.push({ ...c, from: running, to, fill: c.sign > 0 ? "var(--series-1)" : "var(--series-2)" });
      running = to;
    }
  }
  const W = 1100, rowH = 30, L = 170, R = 110, T = 6;
  const H = T + rows.length * rowH + 6;
  const lo = Math.min(0, ...rows.map((r) => Math.min(r.from, r.to)));
  const hi = Math.max(0, ...rows.map((r) => Math.max(r.from, r.to)));
  const x = (v) => L + (W - L - R) * (v - lo) / ((hi - lo) || 1);
  const bars = rows.map((r, i) => {
    const y0 = T + i * rowH + 6, a = Math.min(x(r.from), x(r.to)), w = Math.max(Math.abs(x(r.to) - x(r.from)), 1);
    const shown = (r.category === "subtotal" || r.category === "total") ? r.value : r.sign * r.value;
    const link = i < rows.length - 1 && r.category !== "total"
      ? `<line x1="${x(r.to)}" x2="${x(r.to)}" y1="${y0 + rowH - 12}" y2="${y0 + rowH}" stroke="var(--axis)"/>` : "";
    return `<g><title>${esc(r.label)}：${num(shown)} ${esc(unit)}</title>
      <text x="${L - 10}" y="${y0 + 13}" text-anchor="end" style="fill:var(--ink-2)">${esc(r.label)}</text>
      <rect x="${a}" y="${y0}" width="${w}" height="${rowH - 12}" rx="3" fill="${r.fill}"/>
      <text x="${Math.max(x(r.from), x(r.to)) + 8}" y="${y0 + 13}" style="fill:var(--ink)">${num(shown, 0)}</text>${link}</g>`;
  }).join("");
  box.innerHTML = `<div class="legend"><span><i style="background:var(--series-1)"></i>加項</span>
      <span><i style="background:var(--series-2)"></i>減項</span><span><i style="background:var(--neutral-bar)"></i>小計 / 毛利</span>
      <span>${esc(unit)}</span></div>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="毛利拆解">
      <line x1="${x(0)}" x2="${x(0)}" y1="${T}" y2="${H - 4}" stroke="var(--axis)"/>${bars}</svg>`;
  return box;
}
