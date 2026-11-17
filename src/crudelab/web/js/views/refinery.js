import { api } from "../api.js";
import { lineChart, waterfall } from "../charts.js";
import { mountBoard } from "../indicator-board.js";
import { errorBox, esc, num, signed, statusTag } from "../ui.js";

const SIMPLE = /^[A-Z][A-Z0-9_]*$/;     // 價格直接就是一條序列或一個指標時，才能在試算裡直接加減
const KIND = { benchmark: "國際基準", fuel: "國內燃料型", integrated: "國內一體化" };

export async function render(main) {
  const [models, indicators] = await Promise.all([api("/refineries"), api("/indicators?module=refinery")]);
  main.innerHTML = `<h2>煉廠盈利</h2>
    <p class="sub">整廠毛利模型（收率 × 產品價 − 原料成本 − 稅費 − 加工費）和單品裂解價差。模型的收率、成本都是參數，可以在下面直接試算，不會保存。</p>
    <div class="tiles" id="models">${models.map((m) => `<button class="tile" data-model="${m.code}">
      <div class="k">${KIND[m.kind]}</div><div><b>${esc(m.name_zh)}</b></div>
      <div class="v">${m.margin.status === "ok" ? num(m.margin.last_value) : "—"}</div>
      <div class="d">${esc(m.unit)}${m.margin.chg_1d == null ? "" : "　日變化 " + signed(m.margin.chg_1d)}</div>
      <div style="margin-top:6px">${statusTag(m.margin)}</div></button>`).join("")}</div>
    <div id="model"></div>
    <h2 style="margin-top:24px">裂解與價差</h2><p class="sub">單品對原油的價差，以及中國口徑的原油到廠成本和化工價差。</p>
    <div id="board"></div>`;
  mountBoard(main.querySelector("#board"), indicators.filter((i) => !i.model));

  const box = main.querySelector("#model");
  const select = async (code) => {
    sessionStorage.setItem("refinery-model", code);
    main.querySelectorAll("#models .tile").forEach((t) => t.classList.toggle("on", t.dataset.model === code));
    box.innerHTML = '<p class="muted">載入中…</p>';
    try { await renderModel(box, code); } catch (e) { box.innerHTML = errorBox(e); }
  };
  main.querySelectorAll("#models .tile").forEach((t) => t.addEventListener("click", () => select(t.dataset.model)));
  const saved = sessionStorage.getItem("refinery-model");
  if (models.length) await select(models.some((m) => m.code === saved) ? saved : models[0].code);
}

async function renderModel(box, code) {
  const m = await api(`/refineries/${code}`);
  const b = m.breakdown;
  box.innerHTML = `<div class="panel"><h2>${esc(m.name_zh)} <span class="tag">${KIND[m.kind]}</span></h2>
    <p class="sub">${esc(m.notes ?? "")}</p><div id="body"></div></div>`;
  const body = box.querySelector("#body");
  const s = m.margin;
  if (s.unverified_parameters.length) {
    body.insertAdjacentHTML("beforeend", `<div class="msg warn">這個模型有 ${s.unverified_parameters.length} 項假設是暫定值，尚待核對（收率、加工費、稅率等）。結果僅供參考，請到「參數」頁核對。</div>`);
  }
  if (!b) {
    const why = s.status === "missing_parameter" ? `缺參數：${s.missing_parameters.join("、")}`
      : s.status === "missing_data" ? `缺數據：${s.missing_series.join("、")}` : "各輸入序列的日期沒有重疊";
    body.insertAdjacentHTML("beforeend", `<div class="msg">目前算不出毛利。${esc(why)}。</div>`);
    return;
  }
  body.insertAdjacentHTML("beforeend", `<div class="cols"><div><h3>毛利走勢</h3><div id="hist"></div></div>
    <div><h3>毛利拆解 <span class="muted" style="font-weight:400">${b.date}</span></h3><div id="fall"></div></div></div>
    <div id="tables"></div>`);
  body.querySelector("#hist").append(lineChart(m.history, { unit: m.unit, height: 290 }));
  body.querySelector("#fall").append(waterfall(b.components, { unit: m.unit }));
  renderTables(body.querySelector("#tables"), m, b, null);
}

// 明細表 + 試算。scenario 為 null 時只顯示基準。state 記住上一次試算填的價格變動。
function renderTables(root, m, base, scenario, state = { shifts: {} }) {
  const cmp = scenario !== null;
  const lineRows = (baseLines, scenLines, weightLabel) => baseLines.map((p, i) => {
    const q = scenLines?.[i];
    return `<tr><td>${esc(p.name_zh)}<div class="mono muted">${esc(p.price_formula)}</div></td>
      <td class="n"><input class="num" data-param="${p.weight_param}" data-base="${p.weight}" value="${q?.weight ?? p.weight}" inputmode="decimal" aria-label="${esc(p.name_zh)}${weightLabel}"></td>
      <td class="n">${num(p.price)}</td>
      <td class="n">${SIMPLE.test(p.price_formula)
        ? `<input class="num" data-price="${p.price_formula}" value="${state.shifts[p.price_formula] ?? ""}" placeholder="0" inputmode="decimal" aria-label="${esc(p.name_zh)}價格變動">`
        : '<span class="muted" title="這個價格由公式折算而來，請改底層序列或參數">—</span>'}</td>
      <td class="n">${p.consumption_tax == null ? "" : num(p.consumption_tax)}</td>
      <td class="n">${num(p.value)}</td>${cmp ? `<td class="n">${num(q.value)}</td>` : ""}</tr>`;
  }).join("");
  const yieldSum = base.products.reduce((a, p) => a + (p.weight ?? 0), 0);

  root.innerHTML = `<div class="cols" style="margin-top:8px">
    <div><h3>加減項</h3><div class="scroll"><table><tr><th>項目</th><th class="n">基準</th>${cmp ? '<th class="n">試算</th><th class="n">差異</th>' : ""}</tr>
      ${base.components.map((c, i) => {
        const v = c.value, w = scenario?.components[i].value;
        const prefix = c.category === "subtotal" || c.category === "total" ? "" : c.sign > 0 ? "＋ " : "－ ";
        return `<tr class="${c.category === "total" ? "total" : ""}"><td>${prefix}${esc(c.label)}
          ${c.ref_kind === "parameter" ? `<input class="num" style="margin-left:8px" data-param="${c.ref}" data-base="${v}" value="${w ?? v ?? ""}" inputmode="decimal" aria-label="${esc(c.label)}">` : ""}</td>
          <td class="n">${num(v)}</td>${cmp ? `<td class="n">${num(w)}</td><td class="n ${w - v > 0 ? "up" : w - v < 0 ? "down" : ""}">${signed(w - v)}</td>` : ""}</tr>`;
      }).join("")}</table></div></div>
    <div><h3>產品</h3><div class="scroll"><table><tr><th>產品</th><th class="n">收率</th><th class="n">價格</th><th class="n">價格變動</th><th class="n">消費稅</th><th class="n">收入</th>${cmp ? '<th class="n">試算</th>' : ""}</tr>
      ${lineRows(base.products, scenario?.products, "收率")}
      <tr class="total"><td>合計</td><td class="n">${num(yieldSum, 3)}</td><td colspan="${cmp ? 5 : 4}" class="muted" style="font-weight:400">其餘 ${num((1 - yieldSum) * 100, 1)}% 為未列產品、自用和損失</td></tr></table></div>
      <h3>原料</h3><div class="scroll"><table><tr><th>原料</th><th class="n">配比</th><th class="n">價格</th><th class="n">價格變動</th><th></th><th class="n">成本</th>${cmp ? '<th class="n">試算</th>' : ""}</tr>
      ${lineRows(base.feeds, scenario?.feeds, "配比")}</table></div></div></div>
    <div class="row" style="margin-top:12px"><button class="btn primary" id="run">試算</button>
      ${cmp ? '<button class="btn" id="reset">清除試算</button>' : ""}
      <span class="muted">改收率、配比、成本，或在「價格變動」填加減多少（單位 ${esc(m.unit)}），然後按試算。只是臨時計算，不會保存；要永久修改請到「參數」頁。</span></div>
    <div id="smsg"></div>`;

  root.querySelector("#reset")?.addEventListener("click", () => renderTables(root, m, base, null));
  root.querySelector("#run").addEventListener("click", async () => {
    const msg = root.querySelector("#smsg");
    const parameters = {}, shifts = {};
    let bad = false;
    root.querySelectorAll("input[data-param]").forEach((input) => {
      const v = Number(input.value);
      if (input.value.trim() === "" || !Number.isFinite(v)) bad = true;
      else if (v !== Number(input.dataset.base)) parameters[input.dataset.param] = v;
    });
    root.querySelectorAll("input[data-price]").forEach((input) => {
      if (input.value.trim() === "") return;
      const v = Number(input.value), name = input.dataset.price;
      if (!Number.isFinite(v)) bad = true;
      else if (v !== 0) shifts[name] = v;
    });
    if (bad) { msg.innerHTML = '<div class="msg bad">請填有效的數字。</div>'; return; }
    try {
      const result = await api(`/refineries/${m.code}/scenario`, { json: { on: base.date, parameters, shifts } });
      renderTables(root, m, base, result.scenario, { shifts });
    } catch (e) { msg.innerHTML = errorBox(e); }
  });
}
