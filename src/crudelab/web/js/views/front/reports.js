import { api } from "../../api.js";
import { errorBox, esc, time } from "../../ui.js";

const state = { report: null };

// 把報告正文（由後端按固定模板生成的 Markdown：標題、段落、表格、粗體）排成 HTML。
function markdownToHtml(md) {
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  const out = [];
  const lines = md.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const heading = /^(#{1,4}) (.*)$/.exec(line);
    if (heading) { out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`); continue; }
    if (line.startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++].slice(1, -1).split("|").map((c) => c.trim()));
      i--;
      out.push(`<div class="scroll"><table><tr>${rows[0].map((c) => `<th>${inline(c)}</th>`).join("")}</tr>
        ${rows.slice(2).map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</table></div>`);
      continue;
    }
    if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  return out.join("\n");
}

export async function render(main) {
  const [reports, projections, llm] = await Promise.all([api("/reports"), api("/projections"), api("/llm")]);
  if (!reports.some((r) => r.id === state.report)) state.report = reports[0]?.id ?? null;
  main.innerHTML = `<div class="noprint"><h2>報告</h2>
    <p class="sub">報告是生成那一刻的快照：正文由系統按固定模板排出，每個數字都來自指標計算；之後數據或參數怎麼變，已生成的報告都不會變。大模型的解讀是可選的附加部分，單獨標明。</p>
    <div class="row" style="margin-bottom:14px"><select id="kind" aria-label="類型"><option value="daily">日報</option><option value="weekly">周報</option><option value="monthly">月報</option></select>
      <select id="proj" aria-label="推演"><option value="">推演：用最新建立的</option>${projections.map((p) => `<option value="${p.id}">推演：${esc(p.name_zh)}</option>`).join("")}</select>
      <button class="btn primary" id="make">生成報告</button><span id="msg"></span></div></div>
    <div class="split" style="grid-template-columns:320px minmax(0,1fr)">
      <div class="panel noprint"><h2>已生成</h2>${reports.length ? `<table>${reports.map((r) => `<tr class="click ${r.id === state.report ? "sel" : ""}" data-report="${r.id}">
        <td>${esc(r.title)}<div class="muted">${time(r.created_at)}${r.has_narrative ? " · 含解讀" : ""}</div></td></tr>`).join("")}</table>` : '<p class="muted">還沒有報告。</p>'}</div>
      <div id="view"></div></div>`;
  main.querySelector("#make").addEventListener("click", async () => {
    try {
      const r = await api("/reports", { json: { kind: main.querySelector("#kind").value, projection_id: Number(main.querySelector("#proj").value) || null } });
      state.report = r.id;
      await render(main);
    } catch (e) { main.querySelector("#msg").innerHTML = errorBox(e); }
  });
  main.querySelectorAll("tr[data-report]").forEach((tr) => tr.addEventListener("click", () => { state.report = Number(tr.dataset.report); render(main); }));
  if (state.report == null) return;

  const view = main.querySelector("#view");
  const r = await api(`/reports/${state.report}`);
  const meta = r.narrative_meta ?? {};
  view.innerHTML = `<div class="panel"><div class="row noprint" style="margin-bottom:8px">
      <a class="btn" style="text-decoration:none" href="/api/reports/${r.id}/markdown" download>下載 Markdown</a>
      <button class="btn" id="print">列印 / 存成 PDF</button>
      <button class="btn" id="narrate" ${llm.configured ? "" : "disabled"} title="${esc(llm.configured ? "" : llm.message)}">${r.narrative ? "重寫解讀" : "讓大模型寫解讀"}</button>
      <span class="tag ${llm.configured ? "ok" : "warn"}">${llm.configured ? esc(llm.provider + " / " + llm.model) : "大模型未配置"}</span><span id="nmsg"></span></div>
    <div class="report">${markdownToHtml(r.markdown)}
      ${r.narrative ? `<h2>解讀</h2>
        <p class="muted">由大模型（${esc(meta.provider)} / ${esc(meta.model)}）根據以上正文撰寫，不是系統計算的結果。</p>
        ${meta.guard_passed ? "" : `<div class="msg bad">數字核對未通過：解讀裡的這些數字在正文中找不到，請勿採信：${(meta.unknown_numbers ?? []).map(esc).join("、")}</div>`}
        <div class="narrative">${esc(r.narrative)}</div>` : ""}</div></div>`;
  view.querySelector("#print").addEventListener("click", () => window.print());
  view.querySelector("#narrate").addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    view.querySelector("#nmsg").textContent = "撰寫中…";
    try { await api(`/reports/${r.id}/narrative`, { method: "POST" }); await render(main); }
    catch (e) { view.querySelector("#nmsg").innerHTML = errorBox(e); ev.target.disabled = false; }
  });
}
