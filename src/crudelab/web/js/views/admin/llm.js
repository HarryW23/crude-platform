// 大模型：用哪一家、調用限制、最近 24 小時用量、自動暫停、最近的調用記錄（只有時間、用途和用量，不含內容）。
import { api } from "../../api.js";
import { errorBox, esc, num, time, zoneLabel } from "../../ui.js";

const STATUS = { ok: ["ok", "成功"], failed: ["bad", "失敗"], running: ["warn", "進行中"] };

export async function render(main) {
  const d = await api("/llm/usage");
  const s = d.status, k = d.settings, u = d.usage;
  const keyName = { deepseek: "DEEPSEEK_API_KEY", openai: "OPENAI_API_KEY", anthropic: "ANTHROPIC_API_KEY", compatible: "CRUDELAB_LLM_API_KEY" }[s.provider];
  const bar = (used, cap) => `<b>${num(used, 0)}</b> <span class="muted">/ ${num(cap, 0)}</span>`;
  main.innerHTML = `<h2>大模型</h2>
    <p class="sub">用於抽取事件草稿、報告解讀和問答。用哪一家、調用上限都在 config/llm.yaml；密鑰用 <span class="mono">crudelab secret set</span> 保存，不經過網頁。
    每次調用只記時間、用途、用量和結果，不記發出去的內容和回覆。</p>
    ${s.configured ? `<div class="msg ok">${esc(s.provider)} / ${esc(s.model)}：${esc(s.message)}</div>`
      : `<div class="msg warn">${esc(s.message)}${keyName ? `。在服務器上運行 <span class="mono">crudelab secret set ${keyName}</span>` : ""}</div>`}
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

  main.querySelector("#test")?.addEventListener("click", async (ev) => {
    const msg = main.querySelector("#msg");
    ev.target.disabled = true;
    msg.innerHTML = '<div class="msg">測試中…</div>';
    try {
      const r = await api("/llm/test", { method: "POST" });
      await render(main);
      main.querySelector("#msg").innerHTML = `<div class="msg ok">連通正常：${esc(r.provider)} / ${esc(r.model)}，回覆「${esc(r.reply)}」，token 輸入 ${num(r.tokens[0], 0)} / 輸出 ${num(r.tokens[1], 0)}。</div>`;
    } catch (e) { msg.innerHTML = errorBox(e); ev.target.disabled = false; }
  });
  main.querySelector("#reset")?.addEventListener("click", async () => {
    if (!confirm("確認已經處理好（例如換了密鑰、充了值）？解除後下一次請求會照常發出。")) return;
    try { await api("/llm/reset", { method: "POST" }); await render(main); }
    catch (e) { main.querySelector("#msg").innerHTML = errorBox(e); }
  });
}
