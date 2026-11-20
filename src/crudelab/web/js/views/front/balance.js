import { api } from "../../api.js";
import { balanceBars, scatterChart, seasonalChart } from "../../charts.js";
import { mountBoard } from "../../indicator-board.js";
import { errorBox, esc, num, signed, statusTag, time, zoneLabel } from "../../ui.js";

const FREQ = { weekly: "周度", monthly: "月度", quarterly: "季度", daily: "日度" };
const state = { table: null, scenario: "", stock: null, relation: null };

const periodLabel = (iso, frequency) => frequency === "weekly" ? iso.slice(5) : iso.slice(2, 7);

export async function render(main) {
  const [tables, scenarios, relations, series, indicators] = await Promise.all([
    api("/balances"), api("/scenarios"), api("/relations"), api("/series"), api("/indicators?module=balance")]);
  const stocks = series.filter((s) => s.kind === "stock");
  state.table = tables.some((t) => t.code === sessionStorage.getItem("balance-table"))
    ? sessionStorage.getItem("balance-table") : tables[0]?.code;
  const handed = sessionStorage.getItem("balance-scenario");      // 從「地緣事件」跳過來時指定的情境
  if (handed) { state.scenario = handed; sessionStorage.removeItem("balance-scenario"); }
  if (!scenarios.some((s) => s.code === state.scenario)) state.scenario = "";
  state.stock ??= stocks.find((s) => s.n)?.code ?? stocks[0]?.code;
  state.relation ??= relations[0]?.code;

  main.innerHTML = `<h2>供需平衡</h2>
    <p class="sub">平衡表以機構數據為底稿。你自己的判斷不改數據，而是建一個「情境」，把調整加在單項上並寫明依據；數據和判斷始終分開，可以隨時對照。</p>
    <div class="row" style="margin-bottom:14px">
      <div class="tabs" id="tabs">${tables.map((t) => `<button data-table="${t.code}">${esc(t.name_zh)}</button>`).join("")}</div>
      <span style="flex:1"></span>
      <label>視圖 <select id="scenario"><option value="">基準（只看數據）</option>
        ${scenarios.map((s) => `<option value="${s.code}">情境：${esc(s.name_zh)}（${s.adjustments} 條調整）</option>`).join("")}</select></label>
      <button class="btn" id="new-scenario">新建情境</button></div>
    <div id="new-form"></div>
    <div id="table"></div>
    <div class="cols">
      <div class="panel"><h2>庫存季節性</h2><p class="sub">當前值對比前五年同期的區間和均值。</p>
        <select id="stock" aria-label="庫存序列">${stocks.map((s) => `<option value="${s.code}">${esc(s.name_zh)}${s.n ? "" : "（缺數據）"}</option>`).join("")}</select>
        <div id="seasonal" style="margin-top:10px"></div></div>
      <div class="panel"><h2>庫存與月差的關係</h2><p class="sub">用歷史樣本擬合的關係：按現在的庫存水平，月差通常在哪裡。描述性統計，不是因果。</p>
        <select id="relation" aria-label="關係模型">${relations.map((r) => `<option value="${r.code}">${esc(r.name_zh)}</option>`).join("")}</select>
        <div id="scatter" style="margin-top:10px"></div></div></div>
    <h2 style="margin-top:26px">供需指標</h2><p class="sub">庫存偏離、表觀需求、同比變化和機構口徑對照。</p><div id="board"></div>`;
  const $ = (id) => main.querySelector("#" + id);
  $("scenario").value = state.scenario;
  $("stock").value = state.stock;
  $("relation").value = state.relation;
  mountBoard($("board"), indicators.filter((i) => !i.model));

  const safe = (box, job) => job().catch((e) => { box.innerHTML = errorBox(e); });
  const drawTable = () => safe($("table"), () => renderTable($("table"), main));
  const drawSeasonal = () => safe($("seasonal"), async () => {
    const data = await api(`/seasonal/${state.stock}`);
    $("seasonal").replaceChildren(seasonalChart(data.points, { unit: data.unit, years: data.years }));
  });
  const drawRelation = () => safe($("scatter"), () => renderRelation($("scatter")));

  main.querySelectorAll("#tabs button").forEach((b) => {
    b.classList.toggle("on", b.dataset.table === state.table);
    b.addEventListener("click", () => {
      state.table = b.dataset.table;
      sessionStorage.setItem("balance-table", state.table);
      main.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
      drawTable();
    });
  });
  $("scenario").addEventListener("change", (ev) => { state.scenario = ev.target.value; drawTable(); });
  $("stock").addEventListener("change", (ev) => { state.stock = ev.target.value; drawSeasonal(); });
  $("relation").addEventListener("change", (ev) => { state.relation = ev.target.value; drawRelation(); });
  $("new-scenario").addEventListener("click", () => newScenarioForm($("new-form"), main));

  if (state.table) drawTable(); else $("table").innerHTML = '<div class="msg">還沒有定義平衡表。</div>';
  if (state.stock) drawSeasonal();
  if (state.relation) drawRelation();
}

function newScenarioForm(box, main) {
  box.innerHTML = `<div class="panel inset"><div class="row">
    <label>代碼 <input id="sc-code" size="14" placeholder="MY_VIEW" style="text-transform:uppercase"></label>
    <label>名稱 <input id="sc-name" size="18" placeholder="我的判斷"></label>
    <label>說明 <input id="sc-desc" size="34" placeholder="這個情境的核心假設"></label>
    <button class="btn primary" id="sc-save">建立</button><button class="btn" id="sc-cancel">取消</button></div><div id="sc-msg"></div></div>`;
  box.querySelector("#sc-cancel").addEventListener("click", () => { box.innerHTML = ""; });
  box.querySelector("#sc-save").addEventListener("click", async () => {
    const code = box.querySelector("#sc-code").value.trim().toUpperCase();
    try {
      await api("/scenarios", { json: { code, name_zh: box.querySelector("#sc-name").value.trim() || code,
                                        description: box.querySelector("#sc-desc").value.trim() || null } });
      state.scenario = code;
      await render(main);
    } catch (e) { box.querySelector("#sc-msg").innerHTML = errorBox(e); }
  });
}

async function renderTable(box, main) {
  const query = state.scenario ? `&scenario=${state.scenario}` : "";
  const past = 12, future = 12;
  const t = await api(`/balances/${state.table}?past=${past}&future=${future}${query}`);
  const balance = t.rows.find((r) => r.kind === "balance");
  const changed = (r, i) => r.values[i] != null && r.base_values[i] != null && Math.abs(r.values[i] - r.base_values[i]) > 1e-9;
  const digits = t.unit === "kb/d" ? 0 : 2;
  const cls = { supply_total: "sum", demand_total: "sum", balance: "bal" };
  const section = { supply: "供應", demand: "需求" };
  let lastSection = null;

  const body = t.rows.map((r) => {
    let head = "";
    if (section[r.kind] && section[r.kind] !== lastSection) {
      lastSection = section[r.kind];
      head = `<tr><td colspan="${t.periods.length + 1}" class="ink2" style="font-size:12px;padding-top:12px">${lastSection}</td></tr>`;
    }
    return head + `<tr class="${cls[r.kind] ?? ""}"><td>${esc(r.label)}</td>${r.values.map((v, i) => {
      const adj = changed(r, i);
      return `<td class="${t.periods[i].forecast ? "fc " : ""}${adj ? "adj" : ""}" ${adj ? `title="基準 ${num(r.base_values[i], digits)}，調整 ${signed(v - r.base_values[i], digits)}"` : ""}>${v == null ? '<span class="muted">—</span>' : num(v, digits)}</td>`;
    }).join("")}</tr>`;
  }).join("");

  box.innerHTML = `<div class="panel"><h2>${esc(t.name_zh)} <span class="tag">${FREQ[t.frequency]}</span> <span class="tag">${esc(t.unit)}</span>
      ${state.scenario ? '<span class="tag warn">情境視圖</span>' : ""}</h2>
    <p class="sub">${esc(t.notes ?? "")}</p>
    ${t.balance.status === "ok" ? "" : `<div class="msg">${statusTag(t.balance)}</div>`}
    ${t.periods.length ? `<div class="scroll"><table class="periods"><tr><th></th>${t.periods.map((p) =>
        `<th class="${p.forecast ? "fc" : ""}" title="${p.date}">${periodLabel(p.date, t.frequency)}${p.forecast ? "<br>預測" : ""}</th>`).join("")}</tr>${body}</table></div>
      <p class="muted" style="margin:8px 0 0">平衡 = 供應 − 需求，正數表示累庫。灰底是預測期。${state.scenario ? "底部有橙線的格子被情境調整過，滑鼠停留可看基準值。" : ""}某一項缺數據時，合計和平衡在那一期留空。</p>`
      : '<p class="muted">這張表還沒有任何數據。</p>'}</div>
    <div class="cols"><div class="panel"><h2>各期平衡</h2><div id="bars"></div></div>
      <div class="panel" id="adjust"></div></div>`;
  if (t.periods.length) {
    box.querySelector("#bars").append(balanceBars(t.periods.map((p, i) => ({
      label: periodLabel(p.date, t.frequency), value: balance.values[i], base: balance.base_values[i], forecast: p.forecast,
    })), { unit: t.unit }));
  }
  await renderAdjustments(box.querySelector("#adjust"), t, main);
}

async function renderAdjustments(box, t, main) {
  if (!state.scenario) {
    box.innerHTML = `<h2>情境調整</h2><p class="sub">目前是基準視圖，只顯示數據。要加入自己的判斷（例如「OPEC+ 提前增產」「需求不及預期」），先在右上角選一個情境或新建一個。</p>`;
    return;
  }
  const rows = await api(`/scenarios/${state.scenario}/adjustments`);
  const targets = t.rows.filter((r) => r.adjustable);
  box.innerHTML = `<h2>情境調整</h2>
    <p class="sub">在某段時間裡給某一項加減一個量。調整不改動數據，撤銷後記錄仍保留。</p>
    <div class="row"><select id="adj-target" aria-label="調整哪一項">${targets.map((r) => `<option value="${r.indicator}">${esc(r.label)}</option>`).join("")}</select>
      <label>從 <input type="date" id="adj-from"></label><label>到 <input type="date" id="adj-to" title="不填 = 一直持續"></label>
      <label>加減 <input class="num" id="adj-delta" inputmode="decimal" placeholder="例如 -0.3"></label><span class="ink2">${esc(t.unit)}</span></div>
    <div class="row" style="margin-top:8px"><input id="adj-note" placeholder="依據：為什麼這樣調" style="flex:1;min-width:200px">
      <button class="btn primary" id="adj-add">加入調整</button></div><div id="adj-msg"></div>
    ${rows.length ? `<div class="scroll" style="margin-top:12px"><table><tr><th>項目</th><th>時段</th><th class="n">加減</th><th>依據</th><th>錄入（${zoneLabel()}）</th><th></th></tr>
      ${rows.map((a) => `<tr><td>${esc(a.target_name ?? a.target)}${a.orphaned ? ' <span class="tag warn">目標已不存在</span>' : ""}<div class="mono muted">${a.target}</div></td>
        <td class="nw">${a.date_from} → ${a.date_to ?? "持續"}</td><td class="n">${signed(a.delta)} ${esc(a.unit ?? "")}</td>
        <td>${esc(a.note ?? "")}</td><td class="nw">${time(a.created_at)}</td>
        <td><button class="btn" data-remove="${a.id}">撤銷</button></td></tr>`).join("")}</table></div>`
      : '<p class="muted" style="margin-top:12px">這個情境還沒有調整。</p>'}
    <div class="row" style="margin-top:12px"><button class="btn" id="sc-archive">歸檔這個情境</button></div>`;
  const firstForecast = t.periods.find((p) => p.forecast) ?? t.periods.at(-1);
  if (firstForecast) box.querySelector("#adj-from").value = firstForecast.date;
  const fail = (e) => { box.querySelector("#adj-msg").innerHTML = errorBox(e); };
  box.querySelector("#adj-add").addEventListener("click", async () => {
    const raw = box.querySelector("#adj-delta").value.trim(), delta = Number(raw), from = box.querySelector("#adj-from").value;
    if (!from || raw === "" || !Number.isFinite(delta)) { fail(new Error("請填開始日期和加減的數量。")); return; }
    try {
      await api(`/scenarios/${state.scenario}/adjustments`, { json: {
        target: box.querySelector("#adj-target").value, date_from: from, date_to: box.querySelector("#adj-to").value || null,
        delta, note: box.querySelector("#adj-note").value.trim() || null } });
      await render(main);
    } catch (e) { fail(e); }
  });
  box.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/adjustments/${b.dataset.remove}`, { method: "DELETE" }); await render(main); } catch (e) { fail(e); }
  }));
  box.querySelector("#sc-archive").addEventListener("click", async () => {
    try { await api(`/scenarios/${state.scenario}`, { method: "DELETE" }); state.scenario = ""; await render(main); } catch (e) { fail(e); }
  });
}

// 斜率可能很小（例如 每千桶庫存 0.00002 美元）：按有效數字顯示，不按固定小數位
const sig = (v) => v == null ? "—" : `${v > 0 ? "+" : ""}${Number(v.toPrecision(3))}`;

async function renderRelation(box) {
  const fit = await api(`/relations/${state.relation}`);
  box.innerHTML = `<p class="sub" style="margin-bottom:6px">${esc(fit.notes ?? "")}</p>`;
  if (fit.status === "ok") {
    const l = fit.latest;
    box.insertAdjacentHTML("beforeend", `<div class="stat">
      <div>樣本數<b>${fit.n}</b></div><div>R²<b>${num(fit.r2, 2)}</b></div>
      <div>最新實際值<b>${num(l.y)}</b></div><div>按關係應在<b>${num(l.fitted)}</b></div>
      <div>偏離<b>${signed(l.residual)}</b></div><div>偏離 ÷ 殘差標準差<b>${l.residual_std ? signed(l.residual / l.residual_std, 1) : "—"}</b></div></div>
      ${fit.r2 < 0.3 ? '<div class="msg warn">擬合優度低（R² 小於 0.3），這條關係目前解釋力弱，不宜當結論用。</div>' : ""}
      <p class="muted">寫法：${{ ols: "最小二乘", huber: "穩健回歸（少數異常值帶不動整條線）" }[fit.method]}，${fit.shape === "hinge"
        ? `帶轉折點：x ≤ ${num(fit.hinge)} 時斜率 ${sig(fit.slope)}，以上 ${sig(fit.slope_above)}` : `直線，斜率 ${sig(fit.slope)}`}${fit.lag_days ? `；x 領先 y ${fit.lag_days} 天` : ""}。
        ${fit.stability?.pieces ? `把樣本按時間切成 ${fit.stability.pieces} 段分別擬合，斜率同向的比例 ${num(fit.stability.same_sign * 100, 0)}%${fit.stability.same_sign < 1 ? "：<b>關係方向不穩定</b>" : ""}。` : ""}
        這條關係在回測裡的樣本外表現，見「回測」頁。</p>`);
  } else {
    box.insertAdjacentHTML("beforeend", `<div class="msg">樣本不足，沒有擬合：目前只有 ${fit.n} 個可用樣本，至少需要 ${fit.min_points} 個。${fit.n ? "下面只列出已有的點。" : "y 和 x 還沒有重疊的日期。"}</div>`);
  }
  if (fit.points.length >= 2) box.append(scatterChart(fit));
}
