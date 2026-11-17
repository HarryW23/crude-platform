import { api } from "../../api.js";
import { errorBox, esc } from "../../ui.js";

const ACTION = { pull: "接口取數", import_folder: "導入收件夾", report: "生成報告" };
const STATUS = { ok: ["ok", "完成"], skipped: ["", "跳過"], failed: ["bad", "失敗"], running: ["warn", "執行中"] };
const tag = (status) => `<span class="tag ${STATUS[status]?.[0] ?? ""}">${STATUS[status]?.[1] ?? esc(status)}</span>`;

export async function render(main) {
  const [s, runs] = await Promise.all([api("/schedule"), api("/schedule/runs?limit=60")]);
  main.innerHTML = `<h2>定時任務</h2>
    <p class="sub">在 config/schedule.yaml 裡聲明，改完幾秒內生效。時間按 ${esc(s.timezone)} 顯示，現在是 ${esc(s.now)}。「跳過」表示當時沒有可做的事（收件夾是空的、數據源還沒配密鑰），不是錯誤。</p>
    ${s.running ? `<div class="msg ok">調度器運行中，最近一次檢查在 ${esc(s.last_heartbeat)}。</div>`
      : `<div class="msg warn">調度器目前沒有在運行${s.last_heartbeat ? `（上次檢查在 ${esc(s.last_heartbeat)}）` : ""}，到點的任務不會自動執行。它隨 crudelab serve 一起啟動；下面的「立即運行」仍然可用。</div>`}
    <div class="panel"><h2>任務</h2><div id="msg"></div><div class="scroll"><table>
      <tr><th>任務</th><th>動作</th><th>時間</th><th>下次運行</th><th>最近一次</th><th></th></tr>
      ${s.jobs.map((j) => `<tr><td><b>${esc(j.name_zh)}</b>${j.enabled ? "" : ' <span class="tag warn">停用</span>'}<div class="mono muted">${j.code}</div></td>
        <td class="nw">${ACTION[j.action]}</td><td class="nw">${esc(j.schedule)}</td><td class="nw">${j.next_run ?? '<span class="muted">—</span>'}</td>
        <td>${j.last ? `${tag(j.last.status)} <span class="muted">${esc(j.last.at)}${j.last.trigger === "manual" ? " · 手動" : ""}</span><div>${esc(j.last.message ?? "")}</div>` : '<span class="muted">還沒有運行過</span>'}</td>
        <td><button class="btn" data-run="${j.code}">立即運行</button></td></tr>`).join("") || '<tr><td colspan="6" class="muted">還沒有定義任務。</td></tr>'}</table></div></div>
    <div class="panel"><h2>執行記錄</h2>${runs.length ? `<div class="scroll"><table>
      <tr><th>開始</th><th>任務</th><th>觸發</th><th>結果</th><th class="n">耗時</th><th>說明</th></tr>
      ${runs.map((r) => `<tr><td class="nw">${esc(r.started_at)}</td><td class="mono">${esc(r.job)}</td><td class="nw">${r.trigger === "manual" ? "手動" : "定時"}</td>
        <td>${tag(r.status)}</td><td class="n">${r.seconds == null ? "" : r.seconds.toFixed(1) + " 秒"}</td>
        <td>${esc(r.message ?? "")}${r.detail?.files ? `<details><summary class="muted">${r.detail.files.length} 個文件</summary>${r.detail.files.map((f) =>
          `<div class="mono">${esc(f.file)}：${f.error ? "失敗　" + esc(f.error) : `新增 ${f.inserted}，更新 ${f.updated}${f.unmapped ? `，${f.unmapped} 列未識別` : ""}`}</div>`).join("")}</details>` : ""}</td></tr>`).join("")}</table></div>`
      : '<p class="muted">還沒有執行記錄。</p>'}</div>`;
  main.querySelectorAll("[data-run]").forEach((b) => b.addEventListener("click", async () => {
    const msg = main.querySelector("#msg");
    b.disabled = true;
    msg.innerHTML = '<div class="msg">執行中…</div>';
    try { await api(`/schedule/${b.dataset.run}/run`, { method: "POST" }); await render(main); }
    catch (e) { msg.innerHTML = errorBox(e); b.disabled = false; }
  }));
}
