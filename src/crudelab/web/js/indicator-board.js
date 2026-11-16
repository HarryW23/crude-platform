// 指標看板：按 group 分組的表格 + 點選後的詳情（走勢、追溯）。地域價差和煉廠兩個前台頁面共用。
import { api } from "./api.js";
import { lineChart } from "./charts.js";
import { deltaCell, esc, num, statusTag } from "./ui.js";

export function indicatorTable(list, { formula = false } = {}) {
  return `<table><tr><th>指標</th>${formula ? "<th>公式</th>" : ""}<th>日期</th><th class="n">最新值</th>
      <th class="n">日變化</th><th class="n">5 日</th><th class="n">20 日</th><th class="n" title="最新值在過去一年裡的百分位">一年分位</th>
      <th>單位</th><th>狀態</th></tr>
    ${list.map((i) => `<tr class="click" data-ind="${i.code}">
      <td>${esc(i.name_zh)}<div class="mono muted">${i.code}</div></td>
      ${formula ? `<td class="mono">${esc(i.formula)}</td>` : ""}
      <td class="nw">${i.last_date ?? ""}</td><td class="n">${i.status === "ok" ? num(i.last_value) : "—"}</td>
      ${deltaCell(i.chg_1d)}${deltaCell(i.chg_5d)}${deltaCell(i.chg_20d)}
      <td class="n">${i.percentile_1y == null ? "" : num(i.percentile_1y, 0) + "%"}</td>
      <td class="nw">${esc(i.unit)}</td><td>${statusTag(i)}</td></tr>`).join("")}</table>`;
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
  box.innerHTML = '<div class="panel inset"><p class="muted">載入中…</p></div>';
  const panel = box.firstElementChild;
  const data = await api(`/indicators/${summary.code}/data`);
  panel.innerHTML = `<h2>${esc(summary.name_zh)} <span class="mono muted">${summary.code}</span></h2>
    <p class="sub mono">${esc(summary.formula)}${summary.notes ? `<br><span style="font-family:inherit">${esc(summary.notes)}</span>` : ""}</p>`;
  if (!data.length) {
    const why = summary.status === "missing_parameter" ? `缺參數：${summary.missing_parameters.join("、")}。到「參數」頁錄入後即可計算。`
      : summary.status === "missing_data" ? `缺數據：${summary.missing_series.join("、")}。`
      : "各輸入序列的日期沒有重疊。";
    panel.append(Object.assign(document.createElement("p"), { className: "muted", textContent: "目前算不出結果。" + why }));
  } else {
    panel.append(lineChart(data, { unit: summary.unit }));
    const trace = await api(`/indicators/${summary.code}/trace`);
    panel.insertAdjacentHTML("beforeend",
      `<h3>計算追溯 <span class="muted" style="font-weight:400">${trace.date}</span></h3><ul class="tree root">${traceNode(trace.tree)}</ul>`);
  }
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// 在 root 裡渲染分組看板。list 是 /indicators 的結果。
export function mountBoard(root, list, options = {}) {
  const groups = new Map();
  for (const i of list) {
    if (!groups.has(i.group)) groups.set(i.group, []);
    groups.get(i.group).push(i);
  }
  root.innerHTML = '<div class="detail"></div>' + [...groups].map(([name, items]) =>
    `<div class="panel"><h2>${esc(name)}</h2><div class="scroll">${indicatorTable(items, options)}</div></div>`).join("");
  const detail = root.querySelector(".detail");
  root.querySelectorAll("tr[data-ind]").forEach((tr) => tr.addEventListener("click", () => {
    root.querySelectorAll("tr.sel").forEach((r) => r.classList.remove("sel"));
    tr.classList.add("sel");
    showIndicator(detail, list.find((i) => i.code === tr.dataset.ind)).catch((e) => { detail.textContent = e.message; });
  }));
}
