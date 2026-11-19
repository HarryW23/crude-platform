import { api } from "../../api.js";
import { errorBox, esc, num, time, zoneLabel } from "../../ui.js";

const LAYOUT = { wide: "直排（一列日期）", wide_transposed: "橫排（日期在一行）", long: "長表", unrecognized: "無法識別，已跳過" };

export async function render(main) {
  const [sources, series] = await Promise.all([api("/sources"), api("/series")]);
  const options = '<option value="">（不導入）</option>' +
    series.map((s) => `<option value="${s.code}">${s.code}　${esc(s.name_zh)}</option>`).join("");
  main.innerHTML = `
    <div class="panel"><h2>上傳 Excel / CSV</h2>
      <p class="sub">不要求固定模板。系統會自己找日期和表頭，認得出的列自動對應；認不出的由你指定一次，之後同樣的表頭會自動識別。原文件會原樣保存。</p>
      <div class="row"><input type="file" id="file" accept=".xlsx,.xlsm,.xls,.csv,.txt">
        <label>數據源 <select id="src">${sources.filter((s) => s.kind === "excel").map((s) => `<option value="${s.code}">${esc(s.name)}</option>`).join("")}</select></label>
        <button class="btn primary" id="go">預覽</button></div>
      <div id="out"></div></div>
    <div class="panel"><h2>導入記錄</h2><div class="scroll" id="hist"></div></div>`;
  const out = main.querySelector("#out");

  const history = async () => {
    const rows = await api("/imports");
    main.querySelector("#hist").innerHTML = rows.length
      ? `<table><tr><th>批次</th><th>文件</th><th>數據源</th><th>時間（${zoneLabel()}）</th><th class="n">新增</th><th class="n">更新</th><th class="n">未對應的列</th></tr>
        ${rows.map((b) => `<tr><td>${b.id}</td><td>${esc(b.filename)}</td><td>${esc(b.source_code)}</td><td class="nw">${time(b.created_at)}</td>
          <td class="n">${b.stats.inserted}</td><td class="n">${b.stats.updated}</td><td class="n">${b.stats.unmapped_columns}</td></tr>`).join("")}</table>`
      : '<p class="muted">還沒有導入過文件。</p>';
  };
  history();

  main.querySelector("#go").addEventListener("click", async () => {
    const file = main.querySelector("#file").files[0];
    if (!file) { out.innerHTML = '<div class="msg warn">請先選擇文件。</div>'; return; }
    const form = new FormData();
    form.append("file", file);
    form.append("source_code", main.querySelector("#src").value);
    out.innerHTML = '<p class="muted">解析中…</p>';
    let preview;
    try { preview = await api("/imports/preview", { method: "POST", body: form }); }
    catch (e) { out.innerHTML = errorBox(e); return; }

    out.innerHTML = preview.warnings.map((w) => `<div class="msg warn">${esc(w)}</div>`).join("") +
      preview.sheets.map((s) => `<h3>${esc(s.name)} <span class="tag ${s.layout === "unrecognized" ? "warn" : ""}">${LAYOUT[s.layout]}</span></h3>
        ${s.note ? `<p class="sub">${esc(s.note)}</p>` : ""}
        ${s.columns.length ? `<div class="scroll"><table><tr><th>位置</th><th>表頭</th><th>對應序列</th><th class="n">有效值</th><th class="n">跳過</th><th>日期範圍</th><th class="n">最新值</th></tr>
          ${s.columns.map((c) => `<tr><td class="nw">${esc(c.ref)}</td><td>${esc(c.label)}${c.note ? `<div class="muted">${esc(c.note)}</div>` : ""}</td>
            <td><select data-key="${esc(c.key)}" data-auto="${c.series_code ?? ""}">${options}</select>${c.series_code ? "" : ' <span class="tag warn">未識別</span>'}</td>
            <td class="n">${c.n_values}</td><td class="n">${c.n_skipped || ""}</td><td class="nw">${c.first_date ?? ""} → ${c.last_date ?? ""}</td>
            <td class="n">${num(c.last_value, 4)}</td></tr>`).join("")}</table></div>` : ""}`).join("") +
      `<div class="row" style="margin-top:14px"><button class="btn primary" id="commit">確認入庫</button>
        <label><input type="checkbox" id="learn" checked> 記住我指定的對應</label></div>`;
    out.querySelectorAll("select[data-key]").forEach((s) => { s.value = s.dataset.auto; });

    out.querySelector("#commit").addEventListener("click", async (ev) => {
      ev.target.disabled = true;
      const mapping = {};
      out.querySelectorAll("select[data-key]").forEach((s) => { if (s.value !== s.dataset.auto) mapping[s.dataset.key] = s.value || null; });
      try {
        const r = await api(`/imports/${preview.batch_id}/commit`, { json: { mapping, learn: out.querySelector("#learn").checked } });
        out.innerHTML = `<div class="msg ok">入庫完成：新增 ${r.inserted}，更新 ${r.updated}，未變 ${r.unchanged}。${r.learned_aliases ? `記住了 ${r.learned_aliases} 個新對應。` : ""}</div>` +
          (r.unmapped.length ? `<div class="msg">沒有導入的列：${r.unmapped.map(esc).join("；")}</div>` : "");
        history();
      } catch (e) {
        ev.target.disabled = false;
        out.insertAdjacentHTML("afterbegin", errorBox(e));
      }
    });
  });
}
