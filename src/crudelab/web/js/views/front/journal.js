// 觀點覆盤：記下自己的判斷，到期後自動和實際走勢對比；長期統計哪些情況下判斷得準。
// 記下後不能改，只能在到期前撤銷（後端也這樣限制）。評分規則見 src/crudelab/journal.py 開頭。
import { api } from "../../api.js";
import { errorBox, esc, num, signed } from "../../ui.js";

const pct = (v, d = 0) => (v == null ? "—" : `${num(v * 100, d)}%`);
const rate = (r) => (r && r.n ? `${pct(r.rate)}<span class="muted">（${r.n}）</span>` : '<span class="muted">—</span>');
const MOVE = { 1: "漲", 0: "沒怎麼動", "-1": "跌" };
const state = { direction: 1 };

export async function render(main) {
  const d = await api("/views");
  const s = d.summary;
  if (!d.horizons.some((h) => h.code === state.horizon)) state.horizon = d.horizons.find((h) => h.code === "M1")?.code ?? d.horizons[0]?.code;
  main.innerHTML = `<h2>觀點覆盤</h2>
    <p class="sub">把每次的判斷記下來：看${esc(d.anchor_name ?? "")}在某個期限後偏多、中性還是偏空，可以加目標區間、把握和理由。
    系統自動記下當時的價格、波動率和系統信號；到期後按實際走勢評分。記下後不能改，只能在到期前撤銷，這樣統計才可信。
    時間一長，就能看出自己在什麼情況下判斷得準、什麼情況下不準。</p>

    <div class="cols">
      <div class="panel" data-write><h2>記一筆</h2>
        <div class="row"><span class="ink2">期限</span><div class="tabs">${d.horizons.map((h) => `<button data-h="${h.code}" class="${h.code === state.horizon ? "on" : ""}">${esc(h.name_zh)}</button>`).join("")}</div></div>
        <div class="row" style="margin-top:10px"><span class="ink2">方向</span><div class="tabs">${[[1, "偏多"], [0, "中性"], [-1, "偏空"]].map(([v, n]) => `<button data-dir="${v}" class="${v === state.direction ? "on" : ""}">${n}</button>`).join("")}</div></div>
        <div class="row" style="margin-top:10px">
          <label>目標區間 <input class="num" id="v-low" placeholder="下限" inputmode="decimal"> – <input class="num" id="v-high" placeholder="上限" inputmode="decimal"></label>
          <label>把握 <input class="num" id="v-conf" placeholder="50–100" inputmode="decimal"> %</label></div>
        <div class="row" style="margin-top:10px;gap:12px">${Object.entries(d.reasons).map(([k, v]) => `<label><input type="checkbox" data-reason="${k}"> ${esc(v)}</label>`).join("")}</div>
        <textarea id="v-why" rows="3" placeholder="理由：幾句話寫清楚為什麼這樣判斷（覆盤時最有用的就是這段）" style="margin-top:10px"></textarea>
        <div class="row" style="margin-top:8px"><button class="btn primary" id="v-add">記下</button>
          <span class="muted">起點用${esc(d.anchor_name ?? "")}今天（或最近一個交易日）的價格。區間和把握可以不填。</span></div>
        <div id="v-msg"></div></div>

      <div class="panel"><h2>成績</h2>
        ${s.scored ? `<div class="stat">
            <div>方向判對<b>${rate(s.hit)}</b></div>
            <div title="同一批判斷裡，實際上漲的比例：一直看多能對這麼多">一直看多<b>${rate(s.always_up)}</b></div>
            <div title="記錄時系統綜合信號給的方向，用同樣規則評分">系統信號<b>${rate(s.system)}</b></div>
            <div>落在區間<b>${rate(s.range)}</b></div>
            <div title="（把握 − 是否判對）² 的平均，越小越好；一直說 50% 是 0.25">Brier<b>${s.brier.mean == null ? "—" : num(s.brier.mean)}</b></div></div>
          <p class="muted">已評分 ${s.scored} 條，進行中 ${s.open} 條${s.waiting ? `，到期待數據 ${s.waiting} 條` : ""}。方向分三類（漲 / 沒怎麼動 / 跌），亂猜大約對三分之一。
            樣本少於 20 條時，這些比例波動很大，不要急著下結論。</p>
          ${Object.keys(s.by_reason).length ? `<h3>按理由</h3><table><tr><th>理由</th><th class="n">方向判對</th></tr>
            ${Object.values(s.by_reason).sort((a, b) => b.n - a.n).map((r) => `<tr><td>${esc(r.name_zh)}</td><td class="n">${rate(r)}</td></tr>`).join("")}</table>` : ""}
          ${s.calibration.length ? `<h3>把握是否準確</h3><table><tr><th>說的把握</th><th class="n">平均說了</th><th class="n">實際判對</th><th class="n">條數</th></tr>
            ${s.calibration.map((c) => `<tr><td>${pct(c.from)} – ${pct(c.to)}</td><td class="n">${pct(c.said)}</td><td class="n">${pct(c.actual)}</td><td class="n">${c.n}</td></tr>`).join("")}</table>
            <p class="muted">「實際判對」明顯低於「平均說了」= 過度自信；高於 = 太保守。</p>` : ""}`
        : `<p class="muted">還沒有到期的觀點。記下判斷後，到期那天起這裡會顯示成績。${s.open ? `目前有 ${s.open} 條進行中。` : ""}</p>`}
      </div>
    </div>

    <div class="panel"><h2>記錄</h2>${d.views.length ? `<div class="scroll"><table>
      <tr><th>判斷日</th><th>期限</th><th>方向</th><th class="n">區間</th><th class="n">把握</th><th>理由</th><th class="n">起點</th><th>到期</th><th>結果</th><th></th></tr>
      ${d.views.map((v) => `<tr class="${v.cancelled ? "muted" : ""}"><td class="nw">${v.made_on}</td><td class="nw">${esc(v.horizon_name)}</td>
        <td class="nw"><b>${esc(v.direction_name)}</b>${v.system_direction == null ? "" : `<div class="muted">系統：${{ 1: "偏多", 0: "中性", "-1": "偏空" }[v.system_direction]}</div>`}</td>
        <td class="n nw">${v.low == null ? "—" : `${num(v.low)} – ${num(v.high)}`}</td><td class="n">${pct(v.confidence)}</td>
        <td style="max-width:36ch">${v.reason_names.map((r) => `<span class="tag">${esc(r)}</span>`).join(" ")}${v.rationale ? `<div class="ink2">${esc(v.rationale)}</div>` : ""}</td>
        <td class="n nw">${num(v.start_price)}<div class="muted">${v.start_date}</div></td><td class="nw">${v.target_date}</td>
        <td class="nw">${result(v)}</td>
        <td>${v.status === "open" ? `<button class="btn" data-write data-cancel="${v.id}">撤銷</button>` : ""}</td></tr>`).join("")}</table></div>`
      : '<p class="muted">還沒有記錄。</p>'}<div id="list-msg"></div></div>`;

  const $ = (id) => main.querySelector("#" + id);
  main.querySelectorAll("[data-h]").forEach((b) => b.addEventListener("click", () => { state.horizon = b.dataset.h; render(main); }));
  main.querySelectorAll("[data-dir]").forEach((b) => b.addEventListener("click", () => {
    state.direction = Number(b.dataset.dir);
    main.querySelectorAll("[data-dir]").forEach((x) => x.classList.toggle("on", x === b));
  }));
  $("v-add").addEventListener("click", async () => {
    const value = (id) => ($(id).value.trim() === "" ? null : Number($(id).value));
    const low = value("v-low"), high = value("v-high"), conf = value("v-conf");
    if ([low, high, conf].some((x) => x !== null && !Number.isFinite(x))) { $("v-msg").innerHTML = errorBox(new Error("區間和把握要填數字")); return; }
    try {
      await api("/views", { json: { horizon: state.horizon, direction: state.direction, low, high,
        confidence: conf === null ? null : conf / 100, rationale: $("v-why").value.trim() || null,
        reasons: [...main.querySelectorAll("[data-reason]:checked")].map((x) => x.dataset.reason) } });
      await render(main);
    } catch (e) { $("v-msg").innerHTML = errorBox(e); }
  });
  main.querySelectorAll("[data-cancel]").forEach((b) => b.addEventListener("click", async () => {
    const reason = prompt("撤銷的原因（例如記錯了）：");
    if (!reason || !reason.trim()) return;
    try { await api(`/views/${b.dataset.cancel}/cancel`, { json: { reason } }); await render(main); }
    catch (e) { $("list-msg").innerHTML = errorBox(e); }
  }));
}

function result(v) {
  if (v.status === "cancelled") return `已撤銷<div class="muted">${esc(v.cancel_reason ?? "")}</div>`;
  if (v.status === "waiting") return '<span class="tag warn">到期，等數據</span>';
  if (v.status === "open") return `<span class="tag">進行中</span>${v.return == null ? "" : `<div class="muted">目前 ${signed(v.return * 100, 1)}%</div>`}`;
  const tags = [`<span class="tag ${v.hit ? "ok" : "bad"}">${v.hit ? "判對" : "判錯"}</span>`];
  if (v.range_hit != null) tags.push(`<span class="tag ${v.range_hit ? "ok" : ""}">${v.range_hit ? "在區間內" : "區間外"}</span>`);
  return tags.join(" ") + `<div class="muted">實際${MOVE[v.actual]} ${signed(v.return * 100, 1)}%（±${num(v.band * 100, 1)}% 內算沒怎麼動），到期價 ${num(v.end_price)}</div>`
    + (v.system_hit == null ? "" : `<div class="muted">系統${v.system_hit ? "判對" : "判錯"}</div>`);
}
