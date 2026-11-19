import { api } from "../../api.js";
import { lineChart, scatterChart } from "../../charts.js";
import { errorBox, esc, num, signed, time } from "../../ui.js";

const state = { run: null, horizon: "M1", focus: "OVERALL", timer: null };
const pct = (v, d = 1) => v == null ? "—" : `${num(v * 100, d)}%`;
const STATUS = { running: ["warn", "執行中"], done: ["ok", "完成"], failed: ["bad", "失敗"] };

// 一個信號在某個期限上的結果，給表格用
function cells(m, band) {
  if (!m || m.status === "no_data") return '<td class="n muted">—</td><td class="n muted">—</td><td class="n muted">—</td><td class="n muted">—</td>';
  const hit = m.hit_rate == null ? "—" : `<b>${pct(m.hit_rate)}</b><div class="muted">${pct(m.hit_low, 0)}–${pct(m.hit_high, 0)}</div>`;
  const edge = m.hit_rate != null && m.up_share != null ? m.hit_rate - Math.max(m.up_share, 1 - m.up_share) : null;
  const flag = m.status === "insufficient" ? '<span class="tag warn">樣本不足</span>' : m.significant ? '<span class="tag ok">顯著</span>' : '<span class="tag">不顯著</span>';
  return `<td class="n">${hit}</td>
    <td class="n" title="這些日子裡上漲的比例。一直看多的命中率就是它">${pct(m.up_share)}${edge == null ? "" : `<div class="${edge > 0 ? "up" : "down"}">${signed(edge * 100, 1)} 點</div>`}</td>
    <td class="n">${m.ic == null ? "—" : signed(m.ic)}<div class="muted">t ${m.ic_t == null ? "—" : signed(m.ic_t, 1)}</div></td>
    <td class="n">${num(m.n_eff, 0)}<div>${flag}</div></td>`;
}

export async function render(main) {
  clearTimeout(state.timer);
  const runs = await api("/backtests");
  if (!runs.some((r) => r.id === state.run)) state.run = runs[0]?.id ?? null;
  main.innerHTML = `<h2>回測</h2>
    <p class="sub">拿歷史檢驗方向信號和推演的價格區間：每個評估日算出當時的信號分數，看之後 1 周、1 個月、3 個月的價格走向是否一致。
      命中率要和「這段時間裡一直看多（或看空）」的命中率比，高出的部分才是信號的價值。</p>
    <div class="split" style="grid-template-columns:340px minmax(0,1fr)">
      <div>
        <div class="panel"><h2>新的回測</h2>
          <label class="ink2" style="display:block;margin:6px 0">模式 <select id="mode">
            <option value="observation">按數據日期（回測導入的歷史）</option>
            <option value="vintage">按入庫時間（嚴格，需要持續收集過的數據）</option></select></label>
          <div class="row"><label>從 <input type="date" id="start"></label><label>到 <input type="date" id="end"></label></div>
          <label class="ink2" style="display:block;margin:8px 0">每隔 <input class="num" id="step" style="width:56px" placeholder="自動" inputmode="numeric"> 個交易日評估一次</label>
          <button class="btn primary" id="go">開始回測</button><div id="msg"></div>
          <p class="muted" style="margin:10px 0 0">日期不填就用全部歷史。間隔不填：按數據日期每天評估，按入庫時間每 5 天。</p></div>
        <div class="panel"><h2>記錄</h2>${runs.length ? `<table>${runs.map((r) => `<tr class="click ${r.id === state.run ? "sel" : ""}" data-run="${r.id}">
          <td>#${r.id} <span class="tag ${STATUS[r.status][0]}">${STATUS[r.status][1]}</span> <span class="muted">${esc(r.mode_name)}</span>
          <div class="muted">${time(r.created_at)}${r.summary ? ` · ${r.summary.start} 至 ${r.summary.end}` : ""}</div>
          ${r.summary ? `<div class="ink2">綜合 1 個月命中率 ${pct(r.summary.hit_rate)}，IC ${r.summary.ic == null ? "—" : signed(r.summary.ic)}</div>` : ""}
          ${r.message ? `<div class="down">${esc(r.message)}</div>` : ""}</td></tr>`).join("")}</table>` : '<p class="muted">還沒有回測。</p>'}</div>
      </div>
      <div id="view"></div></div>`;
  const $ = (id) => main.querySelector("#" + id);
  $("go").addEventListener("click", async () => {
    const step = $("step").value.trim();
    try {
      const r = await api("/backtests", { json: { mode: $("mode").value, start: $("start").value || null, end: $("end").value || null,
                                                   step: step === "" ? null : Number(step) } });
      state.run = r.id;
      await render(main);
    } catch (e) { $("msg").innerHTML = errorBox(e); }
  });
  main.querySelectorAll("tr[data-run]").forEach((tr) => tr.addEventListener("click", () => { state.run = Number(tr.dataset.run); render(main); }));
  if (state.run == null) return;

  const run = await api(`/backtests/${state.run}`);
  const view = $("view");
  if (run.status === "running") {
    view.innerHTML = '<div class="panel"><p class="muted">回測執行中… 頁面會自動更新。</p></div>';
    state.timer = setTimeout(() => { if (document.body.contains(view)) render(main); }, 2000);
    return;
  }
  if (run.status === "failed") { view.innerHTML = `<div class="panel">${errorBox(new Error(run.message ?? "回測失敗"))}</div>`; return; }
  renderResults(view, run.results);
}

function renderResults(view, r) {
  if (!r.horizons.some((h) => h.code === state.horizon)) state.horizon = r.horizons[0].code;
  const h = r.horizons.find((x) => x.code === state.horizon);
  const MODULE = Object.fromEntries(r.modules.map((m) => [m.module, m.name_zh]));
  const head = `<tr><th>信號</th><th class="n">命中率<div class="muted" style="font-weight:400">95% 區間</div></th>
    <th class="n">上漲比例<div class="muted" style="font-weight:400">命中率高出</div></th><th class="n">IC</th><th class="n">有效樣本</th></tr>`;
  const row = (code, name, sub, m, extra = "") => `<tr class="click ${state.focus === code ? "sel" : ""}" data-focus="${code}">
    <td>${name}${extra}<div class="muted">${sub}</div></td>${cells(m)}</tr>`;
  const groups = r.modules.map((mod) => `<tr><td colspan="5" class="ink2" style="font-size:12px;padding-top:14px">${esc(mod.name_zh)}</td></tr>` +
    r.signals.filter((s) => s.module === mod.module).map((s) => row(s.code, esc(s.name_zh), `${esc(s.input_name)} · ${s.direction > 0 ? "高 = 利多" : "高 = 利空"}`,
      s.by_horizon[h.code], (s.approx ? ' <span class="tag warn" title="用到修訂後的月度數據，結果可能偏樂觀">近似</span>' : "")
        + (s.vintage_exact ? ' <span class="tag ok" title="每個評估日用當時已發佈的那一期機構報告">按期還原</span>' : "") + trendTag(s))).join("") +
    row(`MODULE_${mod.module}`, `<b>${esc(mod.name_zh)}模組</b>`, "模組內加權", mod.by_horizon[h.code])).join("");
  const band = r.band.by_horizon;

  view.innerHTML = `<div class="panel">
    <div class="row"><h2 style="margin:0">#${state.run}　${esc(r.anchor_name)}，${r.start} 至 ${r.end}</h2><span style="flex:1"></span>
      <div class="tabs">${r.horizons.map((x) => `<button data-h="${x.code}" class="${x.code === h.code ? "on" : ""}">看之後 ${esc(x.name_zh)}</button>`).join("")}</div></div>
    <p class="sub" style="margin-top:6px">${esc(r.mode_name)}，${r.dates} 個評估日，每 ${r.step} 個交易日一次。分數絕對值小於 ${r.neutral_band} 的日子算中性，不計入命中率。
      「顯著」指 IC 在有效樣本下 |t| > 2；有效樣本少於 ${r.min_effective_samples} 的不下結論。</p>
    <div class="cols"><div><div class="scroll"><table>${head}
      ${row("OVERALL", "<b>綜合</b>", "三個數據模組加權；地緣不參與", r.overall.by_horizon[h.code])}${groups}</table></div></div>
      <div><h3 id="focus-title"></h3><div id="focus"></div>
        <h3>推演的價格區間</h3><p class="sub">基準分支的一倍標準差區間，實際價格落在區間內的比例。理想值約 ${pct(r.band.expected)}：明顯偏低說明區間太窄，偏高說明太寬。</p>
        <table><tr><th>期限</th><th class="n">落在區間內</th><th class="n">高於上沿</th><th class="n">低於下沿</th><th class="n">樣本</th></tr>
          ${r.horizons.map((x) => `<tr><td>${esc(x.name_zh)}</td><td class="n"><b>${pct(band[x.code].coverage)}</b></td><td class="n">${pct(band[x.code].above)}</td>
            <td class="n">${pct(band[x.code].below)}</td><td class="n">${band[x.code].n}</td></tr>`).join("")}</table>
        <h3>綜合分數與價格</h3><div id="series"></div></div></div>
    ${(r.fair_values ?? []).map((fv) => `<h3>${esc(fv.name_zh)}${fv.approx ? ' <span class="tag warn">近似</span>' : ""}</h3>
      <p class="sub">回測期間：能給漂移的 ${fv.coverage.ok} 天，解釋力弱 ${fv.coverage.weak} 天，無法計算 ${fv.coverage.unavailable} 天。
        「誤差減少」是加了漂移後推演中樞的平均誤差比不加時少了多少，負數表示加了反而更差。</p>
      <div class="cols"><div><table>${head.replace("<th>信號</th>", "<th>偏離作為信號</th>")}
        ${row(`FV:${fv.code}`, "價格高於公允價值 → 看空", "分數 = −偏離 ÷ 典型偏離 ÷ 2", fv.by_horizon[h.code])}</table></div>
      <div><table><tr><th>加漂移後</th>${r.horizons.map((x) => `<th class="n">${esc(x.name_zh)}</th>`).join("")}</tr>
        <tr><td>誤差減少</td>${r.horizons.map((x) => { const d = fv.drift[x.code]; return `<td class="n ${d.improvement > 0 ? "up" : d.improvement < 0 ? "down" : ""}"><b>${d.improvement == null ? "—" : signed(d.improvement * 100, 1) + "%"}</b>${d.significant ? '<div><span class="tag ok">顯著</span></div>' : ""}</td>`; }).join("")}</tr>
        <tr><td>方向命中率</td>${r.horizons.map((x) => `<td class="n">${pct(fv.drift[x.code].hit_rate)}</td>`).join("")}</tr>
        <tr><td>有效樣本</td>${r.horizons.map((x) => `<td class="n">${num(fv.drift[x.code].n_eff, 0)}${fv.drift[x.code].status === "insufficient" ? '<div><span class="tag warn">樣本不足</span></div>' : ""}</td>`).join("")}</tr></table></div></div>`).join("")}
    ${calibrationPanel(r)}
    <h3>需要知道的局限</h3><ul class="ink2">${r.caveats.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>`;

  view.querySelectorAll("[data-h]").forEach((b) => b.addEventListener("click", () => { state.horizon = b.dataset.h; renderResults(view, r); }));
  view.querySelectorAll("tr[data-focus]").forEach((tr) => tr.addEventListener("click", () => { state.focus = tr.dataset.focus; renderResults(view, r); }));

  const focusKey = state.focus.startsWith("MODULE_") ? null : state.focus;
  const p = focusKey && r.points[focusKey] ? r.points[focusKey][h.code] : null;
  const label = focusKey === "OVERALL" ? "綜合分數" : focusKey?.startsWith("FV:")
    ? (r.fair_values ?? []).find((f) => `FV:${f.code}` === focusKey)?.name_zh + "的偏離" : r.signals.find((s) => s.code === focusKey)?.name_zh;
  view.querySelector("#focus-title").textContent = p ? `${label}：分數與之後 ${h.name_zh} 的收益` : "點左邊任一信號看散點圖";
  if (p && p.points.length >= 2) {
    view.querySelector("#focus").append(scatterChart({
      code: `bt-${focusKey}-${h.code}`, status: p.slope == null ? "insufficient" : "ok", slope: p.slope, intercept: p.intercept,
      points: p.points.map(([date, x, y]) => ({ date, x, y })),
      x: { name_zh: "信號分數", unit: "−1 … +1" }, y: { name_zh: `之後 ${h.name_zh} 的收益`, unit: "%" } }, { height: 280 }));
    view.querySelector("#focus").insertAdjacentHTML("beforeend", '<p class="muted">每個點是一個評估日。點越沿著向右上的斜線分佈，信號越有用；一團散沙說明沒有關係。</p>');
  } else if (p) {
    view.querySelector("#focus").innerHTML = '<p class="muted">這個信號在回測區間裡沒有可評估的點（缺數據或歷史太短）。</p>';
  }
  const s = r.series;
  const price = s.dates.map((d, i) => ({ date: d, value: s.price[i] })).filter((x) => x.value != null);
  const overall = s.dates.map((d, i) => ({ date: d, value: s.overall[i] })).filter((x) => x.value != null);
  const box = view.querySelector("#series");
  box.append(lineChart(price, { unit: "價格", height: 150 }));
  if (overall.length >= 2) box.append(lineChart(overall, { unit: "綜合分數", height: 130 }));
}

// 輸入有明顯趨勢（回看期內斜率 |t| > 2）時提示：z 分數會長期偏一邊，可以考慮在信號配置裡加 detrend: linear
function trendTag(s) {
  if (s.detrend === "linear") return ' <span class="tag" title="和回看期的線性趨勢比，不是和均值比">已去趨勢</span>';
  if (s.trend_t == null || Math.abs(s.trend_t) <= 2) return "";
  return ` <span class="tag warn" title="最近一個評估日：回看期內斜率 t = ${num(s.trend_t, 1)}；回測期間 ${pct(s.trending_share, 0)} 的日子 |t| > 2。隨機遊走的價格也常這樣，要結合經濟含義判斷">有趨勢</span>`;
}

const VERDICT = { effective: ["ok", "有預測力"], reversed: ["bad", "方向相反"], weak: ["", "不顯著"], insufficient: ["warn", "樣本不足"] };

function calibrationPanel(r) {
  const c = r.calibration;
  if (!c) return "";
  const tag = (v) => `<span class="tag ${VERDICT[v][0]}">${VERDICT[v][1]}</span>`;
  const line = (name, x) => `<tr><td>${name}</td><td class="n">${num(x.current, 2)}</td>
    <td class="n ${x.proposed > x.current ? "up" : x.proposed < x.current ? "down" : ""}"><b>${num(x.proposed, 2)}</b></td>
    <td class="n">${x.stats.ic == null ? "—" : signed(x.stats.ic)}<div class="muted">t ${x.stats.ic_t == null ? "—" : signed(x.stats.ic_t, 1)}</div></td>
    <td>${tag(x.verdict)}<div class="muted">${esc(x.reason)}</div></td></tr>`;
  const w = c.walk_forward;
  const wf = w ? `<p class="sub">逐步前推檢驗（從 ${w.start} 起，每月初只用當時已實現的收益重算權重，共 ${w.refits} 次）：
      現在的權重 IC <b>${w.current.ic == null ? "—" : signed(w.current.ic)}</b>、命中率 ${pct(w.current.hit_rate)}；
      按校準權重 IC <b>${w.calibrated.ic == null ? "—" : signed(w.calibrated.ic)}</b>、命中率 ${pct(w.calibrated.hit_rate)}。
      ${w.better ? '<span class="tag ok">校準後更好</span>' : '<span class="tag warn">校準後沒有更好：不建議採用</span>'}</p>`
    : '<p class="sub">回測區間太短，沒有做逐步前推檢驗。</p>';
  return `<h3>權重校準（看之後 ${esc(c.horizon_name)} 的預測力）</h3>
    <p class="sub">IC 顯著為正（t ≥ 2）的按 IC 給權重（IC ${c.ic_reference} ≈ 權重 1）；不顯著或方向相反的建議為 0（不參與合成，方向相反的請核對方向，不會自動翻轉）；樣本不足的保留現在的權重。
      建議值用到了全部回測樣本，所以要看下面的逐步前推檢驗：它只用每個時點之前的數據。採用在後台「參數」頁。</p>
    ${wf}
    <div class="cols"><div class="scroll"><table><tr><th>信號</th><th class="n">現在</th><th class="n">建議</th><th class="n">IC</th><th>結論</th></tr>
      ${c.signals.map((x) => line(esc(x.name_zh), x)).join("")}</table></div>
    <div class="scroll"><table><tr><th>模組</th><th class="n">現在</th><th class="n">建議</th><th class="n">IC</th><th>結論</th></tr>
      ${c.modules.map((x) => line(`<b>${esc(x.name_zh)}</b>`, x)).join("")}</table></div></div>`;
}
