import { api } from "../../api.js";
import { pairChart } from "../../charts.js";
import { adminLink } from "../../site.js";
import { errorBox, esc, num, signed } from "../../ui.js";

const state = { projection: null };
const pct = (v) => `${num(v * 100, 0)}%`;

function scoreBar(score) {
  if (score == null) return '<span class="muted">—</span>';
  const half = Math.abs(score) * 50;
  const style = score >= 0 ? `left:50%;width:${half}%;background:var(--series-1)` : `left:${50 - half}%;width:${half}%;background:var(--series-2)`;
  return `<span class="score" role="img" aria-label="分數 ${signed(score)}"><i style="${style}"></i></span> <span class="mono">${signed(score)}</span>`;
}

export async function render(main) {
  const [sig, projections, scenarios, fvs] = await Promise.all([api("/signals"), api("/projections"), api("/scenarios"),
                                                                api("/fair-value")]);
  if (!projections.some((p) => p.id === state.projection)) state.projection = projections.at(-1)?.id ?? null;

  main.innerHTML = `<h2>推演</h2>
    <p class="sub">兩個部分。方向信號回答「各模組現在處在歷史的什麼位置」；價格推演把你設定的幾個情境分支各自折成價格區間，再按機率加權。兩者都把依據攤開，不是黑盒預測。</p>
    <div class="panel"><h2>方向信號</h2><div class="cols"><div>
        <div class="row" style="margin:6px 0 12px"><span class="verdict">${esc(sig.overall.label)}</span>${scoreBar(sig.overall.score)}
          <span class="ink2">有結果的模組 ${sig.overall.modules_ok} / ${sig.overall.modules_total}</span></div>
        <table><tr><th>模組</th><th>判斷</th><th>分數（−1 利空 … +1 利多）</th><th class="n">權重</th><th class="n">有結果的信號</th></tr>
          ${sig.modules.map((m) => `<tr class="click" data-module="${m.module}"><td><b>${esc(m.name_zh)}</b></td><td>${esc(m.label)}</td><td>${scoreBar(m.score)}</td>
            <td class="n">${m.weight ?? "—"}</td><td class="n">${m.signals_ok} / ${m.signals_total}</td></tr>`).join("")}</table>
        <p class="muted" style="margin-top:8px">數據類信號：最新值相對回看期均值偏離幾個標準差（z），z ÷ 2 封頂後乘以方向。地緣：已確認且持續中事件的方向判斷之和 ÷ 4。點一行看明細。權重在${adminLink("params", "參數")}的「推演」組裡改。</p></div>
        <div id="signals"><p class="muted">點左邊任一模組，在這裡看它的信號明細。</p></div></div></div>
    <div id="fv"></div>
    <div class="panel"><h2>價格推演</h2>
        <div class="row" style="margin-bottom:10px">${projections.length ? `<select id="pick" aria-label="推演">${projections.map((p) => `<option value="${p.id}" ${p.id === state.projection ? "selected" : ""}>${esc(p.name_zh)}</option>`).join("")}</select>` : ""}
          <input id="new-name" placeholder="新推演的名稱" size="18">
          ${fvs.length ? `<label><input type="checkbox" id="new-drift"> 加上基本面回歸</label>` : ""}
          <button class="btn" id="new">新建推演</button></div>
        <div id="projection">${projections.length ? "" : '<p class="muted">還沒有推演。新建一個之後，加入情境分支並分配機率。</p>'}</div></div>`;
  const $ = (id) => main.querySelector("#" + id);

  main.querySelectorAll("tr[data-module]").forEach((tr) => tr.addEventListener("click", () => {
    main.querySelectorAll("tr[data-module]").forEach((r) => r.classList.toggle("sel", r === tr));
    const m = sig.modules.find((x) => x.module === tr.dataset.module);
    $("signals").innerHTML = `<h3>${esc(m.name_zh)}的信號</h3>` + (m.signals.length ? (m.module === "geo"
      ? `<table><tr><th>事件</th><th>開始</th><th class="n">方向判斷</th><th>分數</th></tr>${m.signals.map((s) =>
          `<tr><td>${esc(s.name_zh)}</td><td>${s.date}</td><td class="n">${signed(s.bias, 0)}</td><td>${scoreBar(s.score)}</td></tr>`).join("")}</table>`
      : `<div class="scroll"><table><tr><th>信號</th><th>日期</th><th class="n">最新值</th><th class="n">回看期均值</th><th class="n">z</th><th>方向</th><th>分數</th><th class="n">樣本</th></tr>
          ${m.signals.map((s) => `<tr><td>${esc(s.name_zh)}<div class="mono muted">${s.input}</div></td><td class="nw">${s.date ?? ""}</td>
            <td class="n">${num(s.value)}</td><td class="n">${num(s.mean)}</td><td class="n">${s.z == null ? "" : signed(s.z)}</td>
            <td class="nw">${s.direction > 0 ? "高 = 利多" : "高 = 利空"}</td>
            <td>${s.status === "ok" ? scoreBar(s.score) : `<span class="tag warn">${s.status === "missing" ? "缺數據" : "樣本不足"}</span>`}</td><td class="n">${s.n}</td></tr>`).join("")}</table></div>`)
      : '<p class="muted">目前沒有已確認且持續中的事件。</p>');
  }));
  $("new").addEventListener("click", async () => {
    try {
      state.projection = (await api("/projections", { json: { name_zh: $("new-name").value.trim() || "未命名推演",
        drift_model: $("new-drift")?.checked ? fvs[0].code : null } })).id;
      await render(main);
    }
    catch (e) { $("projection").innerHTML = errorBox(e); }
  });
  $("pick")?.addEventListener("change", (ev) => { state.projection = Number(ev.target.value); render(main); });
  renderFairValues($("fv"), fvs).catch((e) => { $("fv").innerHTML = errorBox(e); });
  if (state.projection != null) {
    try { await renderProjection($("projection"), scenarios, main, fvs); } catch (e) { $("projection").innerHTML = errorBox(e); }
  }
}

const FV_STATUS = { ok: ["ok", "可作為漂移"], weak: ["warn", "解釋力弱，不給漂移"], insufficient: ["warn", "樣本不足"], missing: ["warn", "無法計算"] };

async function renderFairValues(box, fvs) {
  if (!fvs.length) return;
  box.innerHTML = fvs.map((f) => `<div class="panel" data-fv="${f.code}"><div class="row"><h2 style="margin:0">${esc(f.name_zh)}</h2>
      <span class="tag ${FV_STATUS[f.status][0]}">${FV_STATUS[f.status][1]}</span><span class="muted">${f.date ?? ""}</span></div>
    <p class="sub" style="margin-top:6px">按曲線結構和庫存，${esc(f.target_name)} 大致應在哪；現在偏離了多少，偏離通常多快消退。每一天的公允價值都只用當時之前 ${Math.round(f.window_days / 365)} 年的數據擬合。${f.notes ? esc(f.notes) : ""}</p>
    ${f.reasons.length ? `<div class="msg warn">${f.reasons.map(esc).join("；")}。</div>` : ""}
    ${f.fair == null ? "" : `<div class="cols"><div>
      <div class="stat"><div>當前價格<b>${num(f.price)}</b></div><div>公允價值<b>${num(f.fair)}</b></div>
        <div>偏離<b class="${f.deviation_pct > 0 ? "up" : "down"}">${signed(f.deviation_pct * 100, 1)}%</b></div>
        <div>偏離 ÷ 典型偏離<b>${f.z == null ? "—" : signed(f.z, 1)}</b></div>
        <div>R²<b>${num(f.r2, 2)}</b></div><div>偏離半衰期<b>${f.half_life == null ? "—" : num(f.half_life, 0) + " 天"}</b></div></div>
      <h3>哪些因子把公允價值推高或壓低</h3><table><tr><th>因子</th><th class="n">當前值</th><th class="n">係數</th><th class="n">對公允價值的貢獻</th></tr>
        ${f.factors.map((x) => `<tr><td>${esc(x.name_zh)}<div class="mono muted">${x.input}</div></td><td class="n">${num(x.value)} <span class="muted">${esc(x.unit)}</span></td>
          <td class="n">${x.coefficient == null ? "—" : x.coefficient.toPrecision(3)}</td>
          <td class="n ${x.contribution_pct > 0 ? "up" : x.contribution_pct < 0 ? "down" : ""}">${x.contribution_pct == null ? "—" : signed(x.contribution_pct * 100, 1) + "%"}</td></tr>`).join("")}</table>
      <p class="muted">貢獻 = 係數 ×（因子當前值 − 它在擬合窗口裡的均值），相對於窗口的平均水平。</p>
      <h3>給推演的漂移</h3><table><tr><th>期限</th><th class="n">預計消退的偏離</th></tr>
        ${Object.values(f.drift).map((d) => `<tr><td>${esc(d.name_zh)}</td><td class="n">${d.effect == null ? '<span class="muted">不給</span>' : `${signed(d.effect)} ${esc(f.unit)}`}</td></tr>`).join("")}</table>
      <p class="muted">假設因子在期限內不變：回答的是「基本面不變的話，價格會往哪邊回」。新建推演時勾選「加上基本面回歸」才會計入。是否真的有用，看「回測」頁。</p>
    </div><div><h3>價格與公允價值</h3><div class="fv-chart"></div></div></div>`}</div>`).join("");
  for (const f of fvs) {
    if (f.fair == null) continue;
    const detail = await api(`/fair-value/${f.code}?days=500`);
    const rows = detail.history.filter((h) => h.price != null);
    box.querySelector(`[data-fv="${f.code}"] .fv-chart`).append(pairChart([
      { name: f.target_name, color: "var(--series-1)", points: rows.map((h) => ({ date: h.date, value: h.price })) },
      { name: "公允價值", color: "var(--series-2)", points: rows.filter((h) => h.fair != null).map((h) => ({ date: h.date, value: h.fair })) },
    ], { unit: f.unit, height: 300 }));
  }
}

async function renderProjection(box, scenarios, main, fvs) {
  const p = await api(`/projections/${state.projection}`);
  const a = p.anchor, reload = () => render(main);
  const cell = (c) => c.central == null ? `<span class="tag warn" title="${esc(c.reason ?? "")}">無法計算</span>`
    : `<b>${num(c.central)}</b>${c.low == null ? "" : `<div class="muted">${num(c.low)} – ${num(c.high)}</div>`}`;
  box.innerHTML = `
    <div class="stat"><div>${esc(a.name_zh)}（${a.date ?? "無數據"}）<b>${num(a.price)}</b></div>
      <div>年化波動率<b>${p.volatility.annualized == null ? "—" : pct(p.volatility.annualized)}</b></div>
      <div>平衡敏感度<b>${p.sensitivity.value == null ? '<span class="tag warn">未設定</span>' : num(p.sensitivity.value)}</b></div>
      <div>機率合計<b class="${Math.abs(p.total_probability - 1) < 1e-6 ? "" : "down"}">${pct(p.total_probability)}</b></div>
      ${fvs.length ? `<div>基本面回歸<b><label style="font-weight:400;font-size:14px"><input type="checkbox" id="drift" ${p.drift_model ? "checked" : ""}> ${p.drift_model ? esc(p.drift?.name_zh ?? p.drift_model) : "不加"}</label></b></div>` : ""}</div>
    ${p.drift?.effects && Object.keys(p.drift.effects).length ? `<div class="msg">已計入基本面回歸（${esc(p.drift.name_zh)}，偏離 ${signed(p.drift.deviation_pct * 100, 1)}%，半衰期約 ${num(p.drift.half_life, 0)} 個交易日）：${p.horizons.map((h) => `${esc(h.name_zh)} ${signed(p.drift.effects[h.code])}`).join("，")}。各分支相同。</div>` : ""}
    ${p.problems.map((x) => `<div class="msg warn">${esc(x)}。</div>`).join("")}
    ${p.sensitivity.value == null ? `<div class="msg">平衡敏感度（${p.sensitivity.parameter}）還沒有設定：全球平衡每多過剩 1 ${esc(p.balance.unit)}，${esc(a.name_zh)} 下跌多少 ${esc(a.unit)}。這是推演的核心假設，請到${adminLink("params", "參數")}給出；在那之前，帶供需調整的分支算不出價格。</div>` : ""}
    <div class="scroll"><table><tr><th>分支</th><th class="n">機率</th><th class="n">直接價格調整</th><th class="n">平衡變化（1 個月）</th>
      ${p.horizons.map((h) => `<th class="n">${esc(h.name_zh)}</th>`).join("")}<th></th></tr>
      ${p.cases.map((c) => `<tr><td style="min-width:220px"><b>${esc(c.label)}</b><div class="muted">${c.scenario_code ? "情境 " + c.scenario_code : "基準（不加調整）"}</div>${c.note ? `<div class="ink2">${esc(c.note)}</div>` : ""}</td>
        <td class="n"><input class="num" style="width:64px" data-prob="${c.id}" value="${num(c.probability * 100, 0)}" inputmode="decimal" aria-label="機率"> %</td>
        <td class="n"><input class="num" style="width:70px" data-adj="${c.id}" value="${c.price_adjustment}" inputmode="decimal" aria-label="直接價格調整"></td>
        <td class="n">${c.horizons.M1?.balance_change == null ? "—" : signed(c.horizons.M1.balance_change)}</td>
        ${p.horizons.map((h) => `<td class="n">${cell(c.horizons[h.code])}</td>`).join("")}
        <td class="nw"><button class="btn" data-save="${c.id}">保存</button> <button class="btn" data-remove="${c.id}">刪除</button></td></tr>`).join("")}
      <tr class="total"><td>機率加權</td><td></td><td></td><td></td>${p.horizons.map((h) => `<td class="n">${num(p.expected[h.code])}</td>`).join("")}<td></td></tr></table></div>
    <p class="muted" style="margin:8px 0">每格上面是中樞，下面是一倍標準差區間（${esc(a.unit)}）。中樞 = 當前價格 + 基本面回歸 − 敏感度 × 平衡變化 + 直接價格調整；區間 = ± 當前價格 × 日波動率 × √交易日數。不加基本面回歸時，基準分支的中樞就是當前價格。區間只反映歷史波動的大小，不是保證。</p>
    <h3>加入分支</h3>
    <div class="row"><input id="c-label" placeholder="分支名稱" size="16">
      <select id="c-scenario" aria-label="情境"><option value="">基準（不加調整）</option>${scenarios.map((s) => `<option value="${s.code}">${esc(s.name_zh)}（${s.adjustments} 條調整）</option>`).join("")}</select>
      <label>機率 <input class="num" style="width:64px" id="c-prob" inputmode="decimal"> %</label>
      <label>直接價格調整 <input class="num" style="width:70px" id="c-adj" value="0" inputmode="decimal"></label>
      <input id="c-note" placeholder="依據" style="flex:1;min-width:140px"><button class="btn primary" id="c-add">加入</button></div>
    <p class="muted" style="margin-top:6px">情境來自「供需平衡」裡建的情境，或「地緣事件」套用預案生成的情境。直接價格調整用來放不經過供需的判斷，例如風險溢價。</p><div id="c-msg"></div>`;
  const fail = (e) => { box.querySelector("#c-msg").innerHTML = errorBox(e); };
  box.querySelector("#drift")?.addEventListener("change", async (ev) => {
    try { await api(`/projections/${state.projection}`, { method: "PATCH", json: { drift_model: ev.target.checked ? fvs[0].code : null } }); await reload(); }
    catch (e) { fail(e); }
  });
  box.querySelectorAll("[data-save]").forEach((b) => b.addEventListener("click", async () => {
    const id = b.dataset.save, prob = Number(box.querySelector(`[data-prob="${id}"]`).value), adj = Number(box.querySelector(`[data-adj="${id}"]`).value);
    if (!Number.isFinite(prob) || !Number.isFinite(adj)) { fail(new Error("請填有效的數字。")); return; }
    try { await api(`/projection-cases/${id}`, { method: "PATCH", json: { probability: prob / 100, price_adjustment: adj } }); await reload(); } catch (e) { fail(e); }
  }));
  box.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/projection-cases/${b.dataset.remove}`, { method: "DELETE" }); await reload(); } catch (e) { fail(e); }
  }));
  box.querySelector("#c-add").addEventListener("click", async () => {
    const prob = Number(box.querySelector("#c-prob").value), adj = Number(box.querySelector("#c-adj").value);
    if (box.querySelector("#c-prob").value.trim() === "" || !Number.isFinite(prob) || !Number.isFinite(adj)) { fail(new Error("請填機率。")); return; }
    try {
      await api(`/projections/${state.projection}/cases`, { json: { label: box.querySelector("#c-label").value.trim() || "未命名分支",
        scenario_code: box.querySelector("#c-scenario").value || null, probability: prob / 100, price_adjustment: adj,
        note: box.querySelector("#c-note").value.trim() || null } });
      await reload();
    } catch (e) { fail(e); }
  });
}
