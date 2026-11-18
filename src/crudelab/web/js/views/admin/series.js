import { api } from "../../api.js";
import { lineChart } from "../../charts.js";
import { errorBox, esc, MODULES, num, time, zoneLabel } from "../../ui.js";

export async function render(main) {
  const [rows, fresh] = await Promise.all([api("/series"), api("/freshness")]);
  const health = Object.fromEntries(fresh.rows.map((r) => [r.code, r]));
  const HEALTH = { stale: ["bad", "斷更"], never: ["bad", "沒有數據"], fresh: ["ok", "正常"], ignored: ["", "不檢查"] };
  const healthTag = (code) => {
    const h = health[code];
    if (!h) return '<span class="muted">—</span>';
    const [cls, text] = HEALTH[h.status];
    const tip = h.status === "ignored" ? "在 freshness.yaml 的 ignore 裡" : `${h.rule}${h.lag == null ? "" : `；目前 ${h.lag}`}`;
    return `<span class="tag ${cls}" title="${esc(tip)}">${text}</span>`;
  };
  main.innerHTML = `<div class="panel"><h2>序列字典</h2>
    <p class="sub">定義在 config/master/series/。點一行可以看數據、修訂歷史和手動錄入。</p>
    <div class="row" style="margin-bottom:10px"><input id="q" placeholder="搜尋代碼或名稱" size="24" aria-label="搜尋">
      <select id="mod" aria-label="模組"><option value="">全部模組</option>${Object.entries(MODULES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
      <label><input type="checkbox" id="only"> 只看有數據的</label>
      <label><input type="checkbox" id="stale"> 只看斷更的${fresh.problems ? `（${fresh.problems}）` : ""}</label></div>
    ${fresh.problems ? `<div class="msg warn">有 ${fresh.problems} 條序列超過期限沒有更新。判斷規則在 config/freshness.yaml，滑鼠停在「斷更」上可以看具體期限。</div>` : ""}
    <div id="detail"></div><div class="scroll"><table id="tbl"></table></div></div>`;
  const $ = (id) => main.querySelector("#" + id);

  const draw = () => {
    const q = $("q").value.toLowerCase(), mod = $("mod").value, only = $("only").checked, stale = $("stale").checked;
    const list = rows.filter((s) => (!q || (s.code + s.name_zh).toLowerCase().includes(q)) && (!mod || s.module === mod) && (!only || s.n)
      && (!stale || ["stale", "never"].includes(health[s.code]?.status)));
    $("tbl").innerHTML = `<tr><th>代碼</th><th>名稱</th><th>模組</th><th>類型</th><th>頻率</th><th>單位</th><th class="n">數據點</th><th>最新日期</th><th class="n">最新值</th><th>來源</th><th>更新狀態</th></tr>` +
      list.map((s) => `<tr class="click" data-code="${s.code}"><td class="mono">${s.code}</td><td>${esc(s.name_zh)}</td><td class="nw">${MODULES[s.module]}</td>
        <td>${s.kind}</td><td>${s.frequency}</td><td class="nw">${esc(s.unit)}</td>
        <td class="n">${s.n || '<span class="tag warn">缺數據</span>'}</td><td class="nw">${s.last_date ?? ""}</td>
        <td class="n">${s.n ? num(s.last_value, 4) : ""}</td><td>${esc(s.last_source ?? "")}</td><td>${healthTag(s.code)}</td></tr>`).join("");
    main.querySelectorAll("#tbl tr.click").forEach((tr) => tr.addEventListener("click", () => detail(tr.dataset.code)));
  };

  const detail = async (code) => {
    const s = rows.find((r) => r.code === code);
    const [data, revisions] = await Promise.all([api(`/series/${code}/data`), api(`/series/${code}/revisions`)]);
    const box = $("detail");
    box.innerHTML = `<div class="panel inset"><h2>${esc(s.name_zh)} <span class="mono muted">${code}</span></h2>
      <p class="sub">${esc(s.unit)} · ${s.frequency}${s.benchmark ? " · 基準 " + esc(s.benchmark) : ""}</p><div id="chart"></div>
      <div class="row" style="margin-top:10px"><b>手動錄入</b><input type="date" id="md" aria-label="日期">
        <input id="mv" class="num" placeholder="數值" inputmode="decimal" aria-label="數值"><button class="btn" id="msave">保存</button>
        <span id="mmsg" class="muted">手動錄入的值優先於其他來源；改寫已有的值時，舊值會留在修訂歷史裡。</span></div>
      ${revisions.length ? `<h3>修訂歷史</h3><div class="scroll"><table><tr><th>數據日期</th><th>來源</th><th class="n">舊值</th><th>被取代的時間（${zoneLabel()}）</th><th>操作者</th></tr>
        ${revisions.map((r) => `<tr><td>${r.date}</td><td>${esc(r.source)}</td><td class="n">${num(r.old_value, 4)}</td><td>${time(r.replaced_at)}</td><td>${esc(r.replaced_by)}</td></tr>`).join("")}</table></div>` : ""}</div>`;
    if (data.length) box.querySelector("#chart").append(lineChart(data, { unit: s.unit }));
    else box.querySelector("#chart").innerHTML = '<p class="muted">這條序列還沒有數據。</p>';
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    box.querySelector("#msave").addEventListener("click", async () => {
      const d = box.querySelector("#md").value, v = Number(box.querySelector("#mv").value), msg = box.querySelector("#mmsg");
      if (!d || box.querySelector("#mv").value.trim() === "" || !Number.isFinite(v)) { msg.textContent = "請填日期和數值。"; return; }
      try {
        await api("/observations", { json: { points: [{ series_code: code, obs_date: d, value: v }] } });
        await render(main);
      } catch (e) { msg.innerHTML = errorBox(e); }
    });
  };

  ["q", "mod", "only", "stale"].forEach((id) => $(id).addEventListener("input", draw));
  draw();
}
