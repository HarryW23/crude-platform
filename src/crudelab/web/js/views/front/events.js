import { api } from "../../api.js";
import { show } from "../../app.js";
import { errorBox, esc, num, signed, time, zoneLabel } from "../../ui.js";

const CATEGORY = { conflict: "衝突", chokepoint: "航道受阻", sanction: "制裁", opec_policy: "產油國政策", outage: "設施中斷",
                   policy: "其他政策", demand: "需求側", other: "其他" };
const STATUS = { rumor: "傳聞", confirmed: "已確認", ongoing: "持續中", resolved: "已結束", dismissed: "不採納" };
const BIAS = { "-2": "強利空", "-1": "利空", "0": "中性", "1": "利多", "2": "強利多" };
const options = (map, selected) => Object.entries(map).map(([k, v]) => `<option value="${k}" ${String(selected) === k ? "selected" : ""}>${v}</option>`).join("");

export async function render(main) {
  const [events, playbooks, meta, llm, impacts, flashes] = await Promise.all([api("/events"), api("/playbooks"),
    api("/meta"), api("/llm"), api("/events/impacts"), api("/flashes?limit=40")]);
  const month = impacts.horizons.find((h) => h.code === "M1") ?? impacts.horizons[0];
  const locations = Object.fromEntries(Object.entries(meta.locations).map(([k, v]) => [k, v.name_zh]));
  const drafts = events.filter((e) => e.draft), confirmed = events.filter((e) => !e.draft);

  main.innerHTML = `<h2>地緣事件</h2>
    <p class="sub">記錄會影響供需或價格的事件，並給出方向判斷。已確認且持續中的事件構成地緣模組的信號；需要量化時，套用預案把事件變成一個情境，它的影響就能在供需平衡表和推演裡使用。
    再填上<b>機率</b>（推演期限內發生或持續的可能性）和<b>風險溢價</b>（發生時直接加在價格上、供需以外的部分），就能看到它的期望影響，並在「推演」頁一鍵同步成分支。</p>
    <div class="cols">
      <div class="panel"><h2>新增事件</h2>
        <div class="row"><input id="e-title" placeholder="一句話標題" style="flex:1;min-width:260px">
          <select id="e-category" aria-label="類別">${options(CATEGORY, "other")}</select>
          <select id="e-status" aria-label="狀態">${options({ rumor: STATUS.rumor, confirmed: STATUS.confirmed, ongoing: STATUS.ongoing }, "confirmed")}</select></div>
        <div class="row" style="margin-top:8px"><label>地區 <select id="e-location"><option value="">（不指定）</option>${options(locations, "")}</select></label>
          <label>方向判斷 <select id="e-bias">${options(BIAS, "0")}</select></label>
          <label>開始 <input type="date" id="e-start"></label>
          <label>機率 <input class="num" id="e-prob" placeholder="0–100" inputmode="decimal"> %</label>
          <label>風險溢價 <input class="num" id="e-premium" placeholder="可不填" inputmode="decimal"> USD/bbl</label></div>
        <textarea id="e-summary" rows="2" placeholder="摘要（可不填）" style="margin-top:8px"></textarea>
        <div class="row" style="margin-top:8px"><input id="e-source" placeholder="出處：連結或來源（可不填）" style="flex:1">
          <button class="btn primary" id="e-add">加入事件庫</button></div><div id="e-msg"></div></div>
      <div class="panel"><h2>從文字抽取 <span class="tag ${llm.configured ? "ok" : "warn"}">${llm.configured ? esc(llm.provider + " / " + llm.model) : "大模型未配置"}</span></h2>
        <p class="sub">貼一段新聞或筆記，讓大模型整理成事件草稿。草稿不參與任何計算，要你逐條確認後才生效。${llm.configured ? "" : "<br>" + esc(llm.message) + "。配置方法見 README。"}</p>
        <textarea id="x-text" rows="5" placeholder="貼上文字…" ${llm.configured ? "" : "disabled"}></textarea>
        <div class="row" style="margin-top:8px"><button class="btn" id="x-run" ${llm.configured ? "" : "disabled"}>抽取成草稿</button></div><div id="x-msg"></div></div>
    </div>
    <div class="panel"><h2>原油快訊 <span class="muted" style="font-weight:400;font-size:13px">金十，每 10 分鐘同步，只收錄命中原油關鍵詞的</span></h2>
      ${flashes.configured ? "" : '<div class="msg warn">還沒有設定金十的 token（在服務器上運行 <span class="mono">crudelab secret set JIN10_TOKEN</span>），快訊不會更新。</div>'}
      ${flashes.items.length ? `<div class="scroll" style="max-height:420px"><table>
        ${flashes.items.map((f) => `<tr><td class="nw muted">${time(f.time)}</td>
          <td>${f.important ? '<span class="tag warn">重要</span> ' : ""}${esc(f.content)}${f.url ? ` <a href="${esc(f.url)}" target="_blank" rel="noopener noreferrer">原文</a>` : ""}
            <div>${f.keywords.map((k) => `<span class="tag">${esc(k)}</span>`).join(" ")}</div></td>
          <td class="nw">${f.extracted ? `<span class="muted">已抽取${f.drafts ? ` ${f.drafts} 條草稿` : "，沒有事件"}</span>`
            : `<button class="btn" data-flash="${f.id}" ${llm.configured ? "" : 'disabled title="大模型未配置"'}>抽成草稿</button>`}</td></tr>`).join("")}</table></div>`
        : '<p class="muted">還沒有收錄的快訊。</p>'}
      <p class="muted" style="margin:8px 0 0">點「抽成草稿」才會調用大模型（只發這一條快訊）；抽出的草稿出現在下面「待確認的草稿」，確認後才生效。關鍵詞在 config/jin10.yaml。</p>
      <div id="f-msg"></div></div>
    ${drafts.length ? `<div class="panel"><h2>待確認的草稿 <span class="tag warn">${drafts.length}</span></h2>
      <p class="sub">由大模型抽取，尚未生效。核對內容後確認，或標為不採納。</p><div class="scroll">${table(drafts, true)}</div></div>` : ""}
    <div class="panel"><h2>事件庫</h2>${confirmed.length ? `<div class="scroll">${table(confirmed, false)}</div>` : '<p class="muted">還沒有事件。</p>'}</div>
    <div id="playbook"></div>`;
  const $ = (id) => main.querySelector("#" + id);
  const reload = () => render(main);

  $("e-add").addEventListener("click", async () => {
    try {
      await api("/events", { json: { title: $("e-title").value, category: $("e-category").value, status: $("e-status").value,
        bias: Number($("e-bias").value), location_code: $("e-location").value || null, started_on: $("e-start").value || null,
        summary: $("e-summary").value.trim() || null, source: $("e-source").value.trim() || null,
        probability: percent($("e-prob").value), price_premium: number($("e-premium").value) } });
      await reload();
    } catch (e) { $("e-msg").innerHTML = errorBox(e); }
  });
  $("x-run").addEventListener("click", async () => {
    $("x-msg").innerHTML = '<div class="msg">抽取中…</div>';
    try {
      const r = await api("/events/extract", { json: { text: $("x-text").value } });
      if (!r.drafts.length) { $("x-msg").innerHTML = `<div class="msg">沒有抽取到相關事件。${r.dropped ? `另有 ${r.dropped} 條格式不合被丟棄。` : ""}</div>`; return; }
      await reload();
    } catch (e) { $("x-msg").innerHTML = errorBox(e); }
  });
  main.querySelectorAll("[data-flash]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    $("f-msg").innerHTML = '<div class="msg">抽取中…</div>';
    try {
      const r = await api(`/flashes/${b.dataset.flash}/extract`, { method: "POST" });
      await reload();
      if (!r.drafts) main.querySelector("#f-msg").innerHTML = '<div class="msg">這條快訊裡沒有抽取到相關事件。</div>';
    } catch (e) { $("f-msg").innerHTML = errorBox(e); b.disabled = false; }
  }));
  main.querySelectorAll("[data-set]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/events/${b.dataset.id}`, { method: "PATCH", json: JSON.parse(b.dataset.set) }); await reload(); }
    catch (e) { b.closest("td").insertAdjacentHTML("beforeend", errorBox(e)); }
  }));
  main.querySelectorAll("select[data-field]").forEach((s) => s.addEventListener("change", async () => {
    const value = s.dataset.field === "bias" ? Number(s.value) : s.value;
    try { await api(`/events/${s.dataset.id}`, { method: "PATCH", json: { [s.dataset.field]: value } }); await reload(); }
    catch (e) { s.closest("td").insertAdjacentHTML("beforeend", errorBox(e)); }
  }));
  main.querySelectorAll("input[data-num]").forEach((input) => input.addEventListener("change", async () => {
    const field = input.dataset.num, raw = input.value.trim();
    const value = field === "probability" ? percent(raw) : number(raw);
    if (raw && value === null) { input.closest("td").insertAdjacentHTML("beforeend", errorBox(new Error("請填數字"))); return; }
    try { await api(`/events/${input.dataset.id}`, { method: "PATCH", json: { [field]: value } }); await reload(); }
    catch (e) { input.closest("td").insertAdjacentHTML("beforeend", errorBox(e)); }
  }));
  main.querySelectorAll("[data-quantify]").forEach((b) => b.addEventListener("click", () =>
    playbookForm($("playbook"), events.find((e) => e.id === Number(b.dataset.quantify)), playbooks, reload)));
  main.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => {
    sessionStorage.setItem("balance-scenario", b.dataset.view);
    show("balance");
  }));

  function table(rows, isDraft) {
    return `<table><tr><th>事件</th><th>類別</th><th>地區</th><th>開始</th><th>狀態</th><th>方向判斷</th><th>量化影響</th>
      ${isDraft ? "" : `<th class="n">機率</th><th class="n" title="發生時直接加在價格上的部分（USD/bbl）">風險溢價</th><th title="對${esc(month?.name_zh ?? "")}後價格中樞的影響（USD/bbl）">影響（${esc(month?.name_zh ?? "")}）</th>`}
      <th>更新（${esc(zoneLabel())}）</th><th></th></tr>
      ${rows.map((e) => `<tr><td><b>${esc(e.title)}</b>${e.origin === "llm" ? ' <span class="tag">大模型抽取</span>' : ""}
          ${e.summary ? `<div class="ink2">${esc(e.summary)}</div>` : ""}${e.source ? `<div class="muted" style="max-width:60ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(e.source)}">出處：${esc(e.source)}</div>` : ""}</td>
        <td class="nw">${CATEGORY[e.category] ?? esc(e.category)}</td><td class="nw">${esc(e.location_name ?? "")}</td><td class="nw">${e.started_on ?? ""}</td>
        <td><select data-field="status" data-id="${e.id}" aria-label="狀態">${options(STATUS, e.status)}</select></td>
        <td><select data-field="bias" data-id="${e.id}" aria-label="方向判斷">${options(BIAS, e.bias)}</select></td>
        <td class="nw">${e.scenario_code ? `<button class="btn" data-view="${e.scenario_code}">${e.adjustments} 條調整，在平衡表查看</button>` : '<span class="muted">未量化</span>'}</td>
        ${isDraft ? "" : `<td class="n"><input class="num" style="width:56px" data-num="probability" data-id="${e.id}" value="${e.probability == null ? "" : num(e.probability * 100, 0)}" aria-label="機率（%）">%</td>
          <td class="n"><input class="num" style="width:64px" data-num="price_premium" data-id="${e.id}" value="${e.price_premium ?? ""}" aria-label="風險溢價"></td>
          <td class="nw">${impactCell(impacts.events[e.id]?.[month?.code], e)}</td>`}
        <td class="nw">${time(e.updated_at)}</td>
        <td class="nw">${isDraft ? `<button class="btn primary" data-id="${e.id}" data-set='{"draft":false}'>確認</button>
            <button class="btn" data-id="${e.id}" data-set='{"draft":false,"status":"dismissed"}'>不採納</button>`
          : `<button class="btn" data-quantify="${e.id}">套用預案</button>`}</td></tr>`).join("")}</table>`;
  }
}

const number = (raw) => (String(raw).trim() === "" || !Number.isFinite(Number(raw)) ? null : Number(raw));
const percent = (raw) => { const v = number(raw); return v === null ? null : v / 100; };

function impactCell(cell, e) {
  if (["resolved", "dismissed"].includes(e.status)) return '<span class="muted">—</span>';
  if (!cell) return `<span class="muted">${e.scenario_code || e.price_premium ? "算不出" : "未量化"}</span>`;
  if (cell.if_happens == null) return `<span class="muted" title="${esc(cell.reason ?? "")}">算不出</span>`;
  return `發生時 <b>${signed(cell.if_happens)}</b>` + (cell.expected == null ? '<div class="muted">填了機率才有期望值</div>'
    : `<div class="ink2">期望 ${signed(cell.expected)}</div>`);
}

function playbookForm(box, event, playbooks, reload) {
  const draw = (code) => {
    const p = playbooks.find((x) => x.code === code);
    box.innerHTML = `<div class="panel inset"><h2>套用預案到「${esc(event.title)}」</h2>
      <p class="sub">預案列出這類事件通常影響的項目。下面的數值只是起點，不是對這個事件的估計：請逐項改成你的判斷再套用。套用後會生成（或補充）這個事件的情境。</p>
      <div class="row"><label>預案 <select id="pb">${playbooks.map((x) => `<option value="${x.code}" ${x.code === code ? "selected" : ""}>${esc(x.name_zh)}</option>`).join("")}</select></label>
        <label>影響開始 <input type="date" id="pb-start" value="${event.started_on ?? new Date().toISOString().slice(0, 10)}"></label></div>
      <p class="ink2" style="margin:8px 0">${esc(p.description)}</p>
      <table><tr><th>影響的項目</th><th class="n">加減</th><th>單位</th><th class="n">持續天數</th><th>依據</th></tr>
        ${p.impacts.map((i, k) => `<tr><td>${esc(i.target_name)}<div class="mono muted">${i.target}</div></td>
          <td class="n"><input class="num" data-delta="${k}" value="${i.delta}" inputmode="decimal"></td><td>${esc(i.unit)}</td>
          <td class="n"><input class="num" data-days="${k}" value="${i.duration_days ?? ""}" placeholder="持續" inputmode="numeric"></td>
          <td><input data-note="${k}" value="${esc(i.note ?? "")}" style="width:100%"></td></tr>`).join("")}</table>
      <div class="row" style="margin-top:10px"><button class="btn primary" id="pb-apply">套用</button><button class="btn" id="pb-cancel">取消</button></div><div id="pb-msg"></div></div>`;
    box.querySelector("#pb").addEventListener("change", (ev) => draw(ev.target.value));
    box.querySelector("#pb-cancel").addEventListener("click", () => { box.innerHTML = ""; });
    box.querySelector("#pb-apply").addEventListener("click", async () => {
      const impacts = p.impacts.map((i, k) => ({ target: i.target, delta: Number(box.querySelector(`[data-delta="${k}"]`).value),
        duration_days: box.querySelector(`[data-days="${k}"]`).value ? Number(box.querySelector(`[data-days="${k}"]`).value) : null,
        note: box.querySelector(`[data-note="${k}"]`).value.trim() || null }));
      if (impacts.some((i) => !Number.isFinite(i.delta))) { box.querySelector("#pb-msg").innerHTML = errorBox(new Error("加減量必須是數字。")); return; }
      try {
        await api(`/events/${event.id}/apply-playbook`, { json: { playbook: p.code, start: box.querySelector("#pb-start").value, impacts } });
        await reload();
      } catch (e) { box.querySelector("#pb-msg").innerHTML = errorBox(e); }
    });
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };
  draw((playbooks.find((p) => p.category === event.category) ?? playbooks[0]).code);
}
