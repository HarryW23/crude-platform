import { api } from "../../api.js";
import { mountBoard } from "../../indicator-board.js";
import { curveChart } from "../../charts.js";
import { esc, num } from "../../ui.js";

export async function render(main) {
  const [list, curves] = await Promise.all([api("/indicators?module=spread"), api("/curves")]);
  main.innerHTML = `<h2>地域價差</h2>
    <p class="sub">基準價差、月差結構、各油種現貨絕對價、運費、到岸成本與套利窗口。點任一行看走勢和計算追溯。
    升貼水與官價的計價期用 M1 或當日現貨近似。</p>
    <div class="panel" id="curves"></div><div id="board"></div>`;
  mountCurves(main.querySelector("#curves"), curves);
  mountBoard(main.querySelector("#board"), list);
}

function mountCurves(panel, curves) {
  if (!curves.length) { panel.hidden = true; return; }
  let current = (curves.find((c) => c.status === "ok") ?? curves[0]).code;
  const draw = () => {
    const c = curves.find((x) => x.code === current);
    panel.innerHTML = `<h2>期貨曲線</h2>
      <p class="sub">各合約月份的價格，對比 1 周前、1 個月前（按 M1 的交易日往回數）。滑鼠移到某個月份可以看到它當天對應的具體合約。</p>
      <div class="tabs">${curves.map((x) => `<button data-code="${x.code}" class="${x.code === current ? "on" : ""}">${esc(x.name_zh)}</button>`).join("")}</div>
      <div id="curve-body"></div>`;
    panel.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => { current = b.dataset.code; draw(); }));
    const body = panel.querySelector("#curve-body");
    if (c.status !== "ok" || !c.snapshots.length) { body.innerHTML = '<p class="muted">這條曲線還沒有數據。</p>'; return; }
    const shapes = c.snapshots.map((s) => s.shape), now = shapes[0], unit = esc(c.unit);
    const change = (k) => (shapes[k]?.spread ?? null) === null || now.spread === null ? "" :
      `<span class="muted">（較${esc(c.snapshots[k].label)} ${now.spread - shapes[k].spread >= 0 ? "+" : ""}${num(now.spread - shapes[k].spread)}）</span>`;
    body.innerHTML = `<div class="stat">
        <div>結構<b>${esc(now.structure ?? "—")}</b></div>
        <div>M1 − M${now.back_month ?? "?"}<b>${now.spread === null ? "—" : `${num(now.spread)} ${unit}`}</b>${change(1)}</div>
        <div>蝶式 M1 − 2×M6 + M12<b>${now.fly === null ? "—" : `${num(now.fly)} ${unit}`}</b></div>
        <div>有數據的月份<b>${c.months_with_data} / ${c.months.length}</b></div>
      </div>`;
    body.append(curveChart(c.snapshots, c.months, { unit: c.unit }));
    const front = c.snapshots[0].points[0]?.contract;
    const rolled = c.snapshots.filter((s) => s.rolls_since > 0);
    body.insertAdjacentHTML("beforeend", `<p class="muted">${front ? `今天的 M1 是 ${front} 合約。` : "這條曲線沒有換月規則，不知道 M1 對應哪個合約。"}
      ${rolled.length ? `${esc(rolled[0].label)}以來換過 ${rolled[0].rolls_since} 次月：同一個位置（例如 M1）兩天對應的是不同合約。` : ""}
      ${c.roll_rule ? `換月規則：${esc(c.roll_rule)}${c.roll_verified ? "" : '<span class="tag warn">待核對</span>'}` : ""}</p>`);
    if (c.months_with_data < c.months.length) {
      body.insertAdjacentHTML("beforeend", `<p class="muted">缺的月份：${c.snapshots[0].points.filter((p) => p.value === null).map((p) => "M" + p.month).join("、")}。在後台導入對應合約即可補上。</p>`);
    }
  };
  draw();
}
