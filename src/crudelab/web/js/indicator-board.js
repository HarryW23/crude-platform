// 指標看板：按 group 分組的表格 + 點選後的詳情（走勢、追溯）。地域價差、煉廠、資金持倉幾個前台頁面共用。
import { api } from "./api.js";
import { lineChart } from "./charts.js";
import { adminLink } from "./site.js";
import { deltaCell, esc, num, statusTag } from "./ui.js";

const FREQ = { daily: "日", weekly: "周", monthly: "月", quarterly: "季" };
const unitName = (u) => (u === "1" ? "比例" : u);      // 無量綱在配置裡寫作 "1"

// 變化量按「期」算：日度指標是 1 / 5 / 20 個交易日，周度是 1 / 5 / 20 周，依此類推。
// 依賴期貨曲線的指標，變化量按「同一組合約」對比，換月不算進去；做不到的（遠月數據不夠）標 †。
const UNADJUSTED = "這段時間換過月，但遠月合約數據不夠，沒能按同一組合約對比：這個變化含換月跳動。";
const ROLL_NOTE = "依賴期貨曲線的指標按同一組合約對比，換月當天兩個合約之間的價差不算作變化。";
export function indicatorTable(list, { formula = false } = {}) {
  const freqs = new Set(list.map((i) => i.frequency));
  const u = freqs.size === 1 ? FREQ[[...freqs][0]] : "期";
  return `<table><tr><th>指標</th>${formula ? "<th>公式</th>" : ""}<th>日期</th><th class="n">最新值</th>
      <th class="n" title="${ROLL_NOTE}">${u === "日" ? "日變化" : "上" + u + "變化"}</th><th class="n" title="${ROLL_NOTE}">5 ${u}</th><th class="n" title="${ROLL_NOTE}">20 ${u}</th><th class="n" title="最新值在過去一年裡的百分位">一年分位</th>
      <th>單位</th><th>狀態</th></tr>
    ${list.map((i) => `<tr class="click" data-ind="${i.code}">
      <td>${esc(i.name_zh)}${freqs.size > 1 ? ` <span class="tag">${FREQ[i.frequency]}度</span>` : ""}<div class="mono muted">${i.code}</div></td>
      ${formula ? `<td class="mono">${esc(i.formula)}</td>` : ""}
      <td class="nw">${i.last_date ?? ""}${i.forecast_until ? `<div class="muted" title="之後的期間是預測值">預測至 ${i.forecast_until.slice(0, 7)}</div>` : ""}</td><td class="n">${i.status === "ok" ? num(i.last_value) : "—"}</td>
      ${["chg_1d", "chg_5d", "chg_20d"].map((k) => deltaCell(i[k], 2, (i.chg_unadjusted ?? []).includes(k) ? UNADJUSTED : "")).join("")}
      <td class="n">${i.percentile_1y == null ? "" : num(i.percentile_1y, 0) + "%"}</td>
      <td class="nw">${esc(unitName(i.unit))}</td><td>${statusTag(i)}</td></tr>`).join("")}</table>`;
}

function traceNode(n) {
  const value = n.value == null ? '<span class="tag warn">無值</span>' : `<b>${num(n.value, 4)}</b> ${esc(n.unit ?? "")}`;
  let line;
  if (n.kind === "indicator") {
    line = `<span class="tag">指標</span> ${esc(n.name_zh)} = ${value}<div class="mono muted">${n.code} = ${esc(n.formula)}</div>`;
  } else if (n.kind === "parameter") {
    line = `<span class="tag">參數</span> ${esc(n.name_zh)} <span class="mono muted">${n.code}</span> = ${value}
      ${n.verified ? "" : '<span class="tag warn">待核對</span>'}
      ${n.valid_from ? `<div class="muted">${n.valid_from === "1900-01-01" ? "自始生效" : n.valid_from + " 起生效"}</div>` : ""}`;
  } else {
    line = `<span class="tag">數據</span> ${esc(n.name_zh)} <span class="mono muted">${n.code}</span> = ${value}` +
      (n.source_code ? `<div class="muted">來源 ${esc(n.source_code)}${n.filename ? "，文件 " + esc(n.filename) : ""}${n.source_ref ? "，位置 " + esc(n.source_ref) : ""}</div>` : "");
  }
  return `<li>${line}${n.note ? `<div class="muted">※ ${esc(n.note)}</div>` : ""}
    ${n.children ? `<ul class="tree">${n.children.map(traceNode).join("")}</ul>` : ""}</li>`;
}

export async function showIndicator(box, summary) {
  box.innerHTML = '<div class="panel"><p class="muted">載入中…</p></div>';
  const panel = box.firstElementChild;
  const [data, rolls] = await Promise.all([api(`/indicators/${summary.code}/data`),
    summary.roll_adjusted ? api(`/indicators/${summary.code}/rolls`) : Promise.resolve([])]);
  panel.innerHTML = `<h2>${esc(summary.name_zh)} <span class="mono muted">${summary.code}</span></h2>
    <p class="sub mono">${esc(summary.formula)}${summary.notes ? `<br><span style="font-family:inherit">${esc(summary.notes)}</span>` : ""}</p>`;
  if (!data.length) {
    const why = summary.status === "missing_parameter"
      ? `缺參數：${esc(summary.missing_parameters.join("、"))}。到${adminLink("params", "參數")}錄入後即可計算。`
      : summary.status === "missing_data" ? `缺數據：${esc(summary.missing_series.join("、"))}。`
      : "各輸入序列的日期沒有重疊。";
    panel.insertAdjacentHTML("beforeend", `<p class="muted">目前算不出結果。${why}</p>`);
  } else {
    panel.append(lineChart(data, { unit: unitName(summary.unit), markers: rolls.map((r) => ({ date: r.date, label: `${r.curve_name}換月` })) }));
    if (rolls.length) panel.insertAdjacentHTML("beforeend", '<p class="muted">灰色豎虛線是換月日（新合約當上 M1 的第一天）。走勢圖畫的是連續合約的原始值，換月日的跳動在圖上保留；表格裡的變化量已剔除。</p>');
    const trace = await api(`/indicators/${summary.code}/trace`);
    panel.insertAdjacentHTML("beforeend",
      `<h3>計算追溯 <span class="muted" style="font-weight:400">${trace.date}</span></h3><ul class="tree root">${traceNode(trace.tree)}</ul>`);
  }
}

// 在 root 裡渲染分組看板。list 是 /indicators 的結果。
export function mountBoard(root, list, options = {}) {
  const groups = new Map();
  for (const i of list) {
    if (!groups.has(i.group)) groups.set(i.group, []);
    groups.get(i.group).push(i);
  }
  root.innerHTML = `<div class="split"><div>${[...groups].map(([name, items]) =>
    `<div class="panel"><h2>${esc(name)}</h2><div class="scroll">${indicatorTable(items, options)}</div></div>`).join("")}</div>
    <div class="detail"></div></div>`;
  const detail = root.querySelector(".detail");
  root.querySelectorAll("tr[data-ind]").forEach((tr) => tr.addEventListener("click", () => {
    root.querySelectorAll("tr.sel").forEach((r) => r.classList.remove("sel"));
    tr.classList.add("sel");
    showIndicator(detail, list.find((i) => i.code === tr.dataset.ind)).catch((e) => { detail.textContent = e.message; });
  }));
}
