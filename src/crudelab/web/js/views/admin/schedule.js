import { api } from "../../api.js";
import { errorBox, esc, time, zoneLabel } from "../../ui.js";

const ACTION = { pull: "接口取數", import_folder: "導入收件夾", report: "生成報告", check_freshness: "檢查斷更", backup: "備份", calendar: "財經日曆", news: "快訊" };
const STATUS = { ok: ["ok", "完成"], skipped: ["", "跳過"], failed: ["bad", "失敗"], running: ["warn", "執行中"],
  retry: ["warn", "失敗，待重試"], retried: ["", "失敗，已重試"] };
const EVENT = { failure: "失敗", reminder: "仍在失敗", recovery: "已恢復", test: "測試" };
// 斷更檢查記為「失敗」是為了觸發通知；頁面上顯示成「有斷更」，免得以為是程序出錯
const tag = (status, action) => action === "check_freshness" && status === "failed" ? '<span class="tag bad">有斷更</span>'
  : `<span class="tag ${STATUS[status]?.[0] ?? ""}">${STATUS[status]?.[1] ?? esc(status)}</span>`;

// 開關。data-switch 是開關的 key；overridden 表示後台改過，和配置文件裡的預設不同步。
const toggle = (key, on, label) => `<label class="switch" title="${esc(label)}"><input type="checkbox" data-switch="${esc(key)}" ${on ? "checked" : ""} aria-label="${esc(label)}"><span></span></label>`;

export async function render(main) {
  const [s, runs, n] = await Promise.all([api("/schedule"), api("/schedule/runs?limit=60"), api("/notifications")]);
  const ready = n.channels.filter((c) => c.enabled && c.configured);
  const actions = Object.fromEntries(s.jobs.map((j) => [j.code, j.action]));
  main.innerHTML = `<h2>定時任務</h2>
    <p class="sub">任務在 config/schedule.yaml 裡聲明。每個任務有自己的開關，另有一個總開關；在這裡點過之後以這裡為準。任務的「時間」一列按 schedule.yaml 的時區（${esc(zoneLabel(s.timezone))}）寫；其他時間按右上角選的${esc(zoneLabel())}顯示，現在是 ${time(s.now)}。</p>
    ${!s.running ? `<div class="msg warn">調度器目前沒有在運行${s.last_heartbeat ? `（上次檢查在 ${time(s.last_heartbeat)}）` : ""}，到點的任務不會自動執行。它隨 crudelab serve 一起啟動；「立即運行」仍然可用。</div>`
      : s.enabled ? `<div class="msg ok">調度器運行中，最近一次檢查在 ${time(s.last_heartbeat, true)}。</div>`
      : '<div class="msg warn">總開關關著：調度器在運行，但不會執行任何任務。</div>'}
    <div class="panel"><div class="row"><h2 style="margin:0">任務</h2><span style="flex:1"></span>
        <span class="ink2">自動運行總開關</span>${toggle("scheduler", s.enabled, "自動運行總開關")}</div>
      <div id="msg"></div><div class="scroll"><table>
      <tr><th>自動運行</th><th>任務</th><th>動作</th><th>時間（${esc(zoneLabel(s.timezone))}）</th><th>下次運行（${esc(zoneLabel())}）</th><th>最近一次</th><th></th></tr>
      ${s.jobs.map((j) => `<tr><td>${toggle(j.switch, j.enabled, j.name_zh + "：自動運行")}</td>
        <td><b>${esc(j.name_zh)}</b><div class="mono muted">${j.code}</div></td>
        <td class="nw">${ACTION[j.action]}</td><td class="nw">${esc(j.schedule)}</td>
        <td class="nw">${j.next_run ? time(j.next_run) : `<span class="muted">${j.enabled ? "總開關關著" : "已關閉"}</span>`}</td>
        <td>${j.last ? `${tag(j.last.status, j.action)} <span class="muted">${time(j.last.at)}${j.last.trigger === "manual" ? " · 手動" : ""}</span><div style="white-space:pre-line">${esc(j.last.message ?? "")}</div>` : '<span class="muted">還沒有運行過</span>'}</td>
        <td><button class="btn" data-run="${j.code}">立即運行</button></td></tr>`).join("") || '<tr><td colspan="7" class="muted">還沒有定義任務。</td></tr>'}</table></div>
      <p class="muted" style="margin:8px 0 0">「跳過」表示當時沒有可做的事（收件夾是空的、數據源還沒配密鑰），不是錯誤。關掉的任務仍然可以手動運行。</p></div>

    <div class="panel"><div class="row"><h2 style="margin:0">失敗通知</h2><span style="flex:1"></span>
        <span class="ink2">通知總開關</span>${toggle("notify", n.enabled, "通知總開關")}</div>
      <p class="sub" style="margin-top:6px">任務${n.on_failure ? "剛失敗時發一條；持續失敗每 " + n.repeat_hours + " 小時提醒一次" : "失敗時不通知"}${n.on_recovery ? "；恢復時再發一條" : ""}。只針對定時觸發的執行。規則和渠道在 config/notifications.yaml 裡改。</p>
      ${!n.enabled ? '<div class="msg warn">通知總開關關著，不會發出任何通知。</div>'
        : ready.length ? "" : '<div class="msg warn">目前沒有可用的渠道：任務失敗時不會有人收到通知，只會在這一頁和導覽列的紅點上顯示。要啟用，先保存某個渠道的地址（見下表的命令）。</div>'}
      <div id="nmsg"></div><div class="scroll"><table>
        <tr><th>啟用</th><th>渠道</th><th>類型</th><th>狀態</th><th>怎麼配置</th><th></th></tr>
        ${n.channels.map((c) => `<tr><td>${toggle(c.switch, c.enabled, c.name_zh + "：啟用")}</td>
          <td><b>${esc(c.name_zh)}</b><div class="mono muted">${c.code}</div></td><td class="nw">${esc(c.type_name)}${c.target ? `<div class="muted">${esc(c.target)}</div>` : ""}</td>
          <td>${c.configured ? '<span class="tag ok">已配置</span>' : '<span class="tag warn">未配置</span>'}</td>
          <td class="mono">crudelab secret set ${c.credential}</td>
          <td><button class="btn" data-test="${c.code}" ${c.configured ? "" : "disabled"}>發送測試</button></td></tr>`).join("") || '<tr><td colspan="6" class="muted">還沒有定義渠道。</td></tr>'}</table></div>
      ${n.recent.length ? `<h3>最近發出的通知</h3><div class="scroll"><table><tr><th>時間（${esc(zoneLabel())}）</th><th>類型</th><th>內容</th><th>發送結果</th></tr>
        ${n.recent.slice(0, 15).map((x) => `<tr><td class="nw">${time(x.at)}</td><td class="nw">${EVENT[x.event] ?? esc(x.event)}</td>
          <td><b>${esc(x.title)}</b><div class="ink2" style="white-space:pre-wrap">${esc(x.body)}</div></td>
          <td>${Object.entries(x.results).map(([k, v]) => `<div class="nw">${esc(k)}：${v === "ok" ? '<span class="tag ok">已送達</span>' : `<span class="tag bad">${esc(v)}</span>`}</div>`).join("") || '<span class="muted">沒有可用的渠道，未發出</span>'}</td></tr>`).join("")}</table></div>` : ""}</div>

    <div class="panel"><h2>執行記錄</h2>${runs.length ? `<div class="scroll"><table>
      <tr><th>開始</th><th>任務</th><th>觸發</th><th>結果</th><th class="n">耗時</th><th>說明</th></tr>
      ${runs.map((r) => `<tr><td class="nw">${time(r.started_at, true)}</td><td class="mono">${esc(r.job)}</td><td class="nw">${r.trigger === "manual" ? "手動" : "定時"}</td>
        <td>${tag(r.status, actions[r.job])}</td><td class="n">${r.seconds == null ? "" : r.seconds.toFixed(1) + " 秒"}</td>
        <td style="white-space:pre-line">${esc(r.message ?? "")}${r.detail?.files ? `<details><summary class="muted">${r.detail.files.length} 個文件</summary>${r.detail.files.map((f) =>
          `<div class="mono">${esc(f.file)}：${f.error ? "失敗　" + esc(f.error) : `新增 ${f.inserted}，更新 ${f.updated}${f.unmapped ? `，${f.unmapped} 列未識別` : ""}`}</div>`).join("")}</details>` : ""}</td></tr>`).join("")}</table></div>`
      : '<p class="muted">還沒有執行記錄。</p>'}</div>`;

  main.querySelectorAll("[data-switch]").forEach((box) => box.addEventListener("change", async () => {
    try { await api(`/switches/${box.dataset.switch}`, { method: "PUT", json: { enabled: box.checked } }); await render(main); }
    catch (e) { box.checked = !box.checked; main.querySelector("#msg").innerHTML = errorBox(e); }
  }));
  main.querySelectorAll("[data-run]").forEach((b) => b.addEventListener("click", async () => {
    const msg = main.querySelector("#msg");
    b.disabled = true;
    msg.innerHTML = '<div class="msg">執行中…</div>';
    try { await api(`/schedule/${b.dataset.run}/run`, { method: "POST" }); await render(main); }
    catch (e) { msg.innerHTML = errorBox(e); b.disabled = false; }
  }));
  main.querySelectorAll("[data-test]").forEach((b) => b.addEventListener("click", async () => {
    const msg = main.querySelector("#nmsg");
    b.disabled = true;
    msg.innerHTML = '<div class="msg">發送中…</div>';
    try {
      const r = await api(`/notifications/${b.dataset.test}/test`, { method: "POST" });
      await render(main);
      main.querySelector("#nmsg").innerHTML = r.result === "ok" ? '<div class="msg ok">測試消息已送達，請到對應的群或郵箱確認。</div>'
        : `<div class="msg bad">測試消息沒有送達：${esc(r.result)}</div>`;
    } catch (e) { msg.innerHTML = errorBox(e); b.disabled = false; }
  }));
  window.dispatchEvent(new Event("crudelab:refresh-alerts"));
}
