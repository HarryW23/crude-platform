// 大模型：選哪一家、哪個型號、開不開思考模式；調用限制、最近 24 小時用量、自動暫停、最近的調用記錄（不含內容）。
// 型號名從供應商的模型列表裡選（「查可用的模型」），不在頁面上寫死：供應商會改名、停用舊名字。
import { api } from "../../api.js";
import { errorBox, esc, num, time, zoneLabel } from "../../ui.js";

const STATUS = { ok: ["ok", "成功"], failed: ["bad", "失敗"], running: ["warn", "進行中"] };

export async function render(main) {
  const d = await api("/llm/usage");
  const s = d.status, k = d.settings, u = d.usage;
  const keyName = s.key_name;
  const bar = (used, cap) => `<b>${num(used, 0)}</b> <span class="muted">/ ${num(cap, 0)}</span>`;
  main.innerHTML = `<h2>大模型</h2>
    <p class="sub">用於抽取事件草稿、報告解讀和問答。在這裡選用哪一家、哪個型號、開不開思考模式，保存後立即生效；
    調用上限在 config/llm.yaml。密鑰用 <span class="mono">crudelab secret set</span> 在服務器上保存，不經過網頁。
    每次調用只記時間、用途、用量和結果，不記發出去的內容和回覆。</p>
    ${s.configured ? `<div class="msg ok">現在用：${esc(s.provider_name)} / <span class="mono">${esc(s.model)}</span>，思考模式${s.thinking ? "開" : "關"}。${esc(s.message)}</div>`
      : `<div class="msg warn">${esc(s.message)}${keyName && !s.key_set ? `。在服務器上運行 <span class="mono">crudelab secret set ${keyName}</span>` : ""}</div>`}
    <div class="panel" id="choice"><h2>模型設定</h2>
      <div class="row" style="align-items:flex-end;gap:16px">
        <label>供應商<br><select id="c-provider">${d.providers.map((p) => `<option value="${p.code}" ${p.code === s.provider ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
        <label>模型<br><select id="c-model" style="min-width:240px"></select></label>
        <button class="btn" id="c-list">查可用的模型</button>
        <label id="c-manual-wrap">或手動填寫<br><input id="c-manual" class="mono" style="width:220px" placeholder="型號名" value=""></label>
        <label id="c-base-wrap" hidden>接口地址<br><input id="c-base" class="mono" style="width:260px" placeholder="https://…" value=""></label>
        <span id="c-think-wrap"><span class="ink2">思考模式</span><br><label class="switch" title="思考模式"><input type="checkbox" id="c-think" ${s.thinking ? "checked" : ""} aria-label="思考模式"><span></span></label></span>
        <button class="btn primary" id="c-save">保存</button>
      </div>
      <div id="c-msg"></div>
      <p class="muted" style="margin:8px 0 0">「查可用的模型」會用你保存的密鑰向供應商查詢這個密鑰能用哪些模型（不耗 token），型號名以它返回的為準。
        思考模式只有 DeepSeek 支持：先推理再回答，回覆更慢、用的 token 多得多；抽取事件和寫解讀一般不需要，複雜的問答可以試試。
        ${s.source !== "後台" ? `目前的設定來自${esc(s.source)}；在這裡保存後以這裡為準。` : ""}</p>
    </div>
    ${d.pause ? `<div class="msg bad">已自動暫停到 ${time(d.pause.until)}（${esc(zoneLabel())}）：${esc(d.pause.reason)}。確認處理好之後再解除。
      <button class="btn" id="reset" style="margin-left:8px">解除暫停</button></div>` : ""}
    <div class="panel"><div class="row"><h2 style="margin:0">最近 24 小時</h2><span style="flex:1"></span>
        <button class="btn" id="test" ${s.configured ? "" : "disabled"}>連通測試</button></div>
      <div id="msg"></div>
      <div class="stat">
        <div>請求次數<span style="display:block">${bar(u.day, k.max_requests_per_day)}</span></div>
        <div>最近一分鐘<span style="display:block">${bar(u.minute, k.max_requests_per_minute)}</span></div>
        <div>token（輸入 + 輸出）<span style="display:block">${bar(u.prompt_tokens_day + u.completion_tokens_day, k.max_tokens_per_day)}</span></div>
        <div>失敗<b>${num(u.failed_day, 0)}</b></div>
        <div>進行中<span style="display:block">${bar(u.running, k.max_concurrent)}</span></div>
      </div>
      <p class="muted" style="margin:0">超過上限的請求不會發出去。連通測試會真的發一次很短的請求（幾十個 token）。
        密鑰無效、餘額不足、觸發對方頻率限制、反覆被拒絕時會自動暫停，到期自動恢復；任何失敗都不自動重試。</p>
      ${d.by_purpose.length ? `<table style="margin-top:10px"><tr><th>用途</th><th class="n">次數</th><th class="n">輸入 token</th><th class="n">輸出 token</th></tr>
        ${d.by_purpose.map((p) => `<tr><td>${esc(p.name_zh)}</td><td class="n">${num(p.calls, 0)}</td><td class="n">${num(p.prompt_tokens, 0)}</td><td class="n">${num(p.completion_tokens, 0)}</td></tr>`).join("")}</table>` : ""}
    </div>
    <div class="panel"><h2>最近的調用</h2><div class="scroll"><table>
      <tr><th>時間（${esc(zoneLabel())}）</th><th>用途</th><th>模型</th><th>結果</th><th class="n">用時</th><th class="n">輸入</th><th class="n">輸出</th><th>說明</th></tr>
      ${d.recent.map((c) => `<tr><td class="nw">${time(c.at, true)}</td><td>${esc(c.purpose_zh)}</td><td class="mono">${esc(c.provider)} / ${esc(c.model)}</td>
        <td><span class="tag ${STATUS[c.status]?.[0] ?? ""}">${STATUS[c.status]?.[1] ?? esc(c.status)}</span>${c.http_status && c.http_status !== 200 ? ` <span class="muted">${c.http_status}</span>` : ""}</td>
        <td class="n">${c.seconds == null ? "" : num(c.seconds, 1) + " 秒"}</td><td class="n">${num(c.prompt_tokens, 0)}</td><td class="n">${num(c.completion_tokens, 0)}</td>
        <td>${esc(c.message ?? "")}</td></tr>`).join("") || '<tr><td colspan="8" class="muted">還沒有調用過。</td></tr>'}</table></div></div>`;

  mountChoice(main, s);
  main.querySelector("#test")?.addEventListener("click", async (ev) => {
    const msg = main.querySelector("#msg");
    ev.target.disabled = true;
    msg.innerHTML = '<div class="msg">測試中…</div>';
    try {
      const r = await api("/llm/test", { method: "POST" });
      await render(main);
      main.querySelector("#msg").innerHTML = `<div class="msg ok">連通正常：${esc(r.provider)} / ${esc(r.model)}${r.thinking ? "（思考模式）" : ""}，回覆「${esc(r.reply)}」，token 輸入 ${num(r.tokens[0], 0)} / 輸出 ${num(r.tokens[1], 0)}。</div>`;
    } catch (e) { msg.innerHTML = errorBox(e); ev.target.disabled = false; }
  });
  main.querySelector("#reset")?.addEventListener("click", async () => {
    if (!confirm("確認已經處理好（例如換了密鑰、充了值）？解除後下一次請求會照常發出。")) return;
    try { await api("/llm/reset", { method: "POST" }); await render(main); }
    catch (e) { main.querySelector("#msg").innerHTML = errorBox(e); }
  });
}

function mountChoice(main, s) {
  const $ = (id) => main.querySelector(id);
  const provider = $("#c-provider"), model = $("#c-model"), manual = $("#c-manual"), msg = $("#c-msg");
  let listed = [];
  const fillModels = () => {
    const current = provider.value === s.provider ? s.model : null;
    const names = [...new Set([...(current ? [current] : []), ...listed])];
    model.innerHTML = names.length
      ? names.map((n) => `<option value="${esc(n)}" ${n === current ? "selected" : ""}>${esc(n)}</option>`).join("")
      : '<option value="">（先點「查可用的模型」，或手動填寫）</option>';
  };
  const sync = () => {
    const none = provider.value === "none";
    const thinking = provider.value === "deepseek";
    $("#c-think-wrap").style.visibility = thinking ? "visible" : "hidden";
    $("#c-base-wrap").hidden = provider.value !== "compatible";
    for (const el of [model, manual, $("#c-list")]) el.disabled = none;
  };
  provider.addEventListener("change", () => { listed = []; fillModels(); sync(); msg.innerHTML = ""; });
  fillModels();
  sync();

  $("#c-list").addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    msg.innerHTML = '<div class="msg">查詢中…</div>';
    try {
      const r = await api("/llm/models", { json: { provider: provider.value } });
      listed = r.models;
      fillModels();
      if (provider.value !== s.provider || !s.model) model.value = listed[0] ?? "";
      msg.innerHTML = listed.length ? `<div class="msg ok">這個密鑰能用 ${listed.length} 個模型，已列在「模型」裡。</div>`
        : '<div class="msg warn">供應商沒有返回模型列表，請手動填寫型號名。</div>';
    } catch (e) { msg.innerHTML = errorBox(e); }
    ev.target.disabled = false;
  });

  $("#c-save").addEventListener("click", async (ev) => {
    const chosen = manual.value.trim() || model.value;
    ev.target.disabled = true;
    try {
      await api("/llm/choice", { method: "PUT", json: { provider: provider.value, model: chosen, thinking: $("#c-think").checked,
                                                         base_url: $("#c-base").value.trim() || null } });
      await render(main);
      main.querySelector("#c-msg").innerHTML = '<div class="msg ok">已保存，立即生效。建議點下面的「連通測試」確認一下。</div>';
    } catch (e) { msg.innerHTML = errorBox(e); ev.target.disabled = false; }
  });
}
