import { api } from "../../api.js";
import { SITE } from "../../site.js";
import { errorBox, esc, time, zoneLabel } from "../../ui.js";

const ROLES = { viewer: "只讀：只能看前台", analyst: "研究員：前台全部功能", admin: "管理員：前台 + 後台" };

export async function render(main) {
  const users = await api("/users");
  main.innerHTML = `<h2>帳號</h2>
    <p class="sub">誰能登錄、能做什麼。帳號不能刪除，只能停用，這樣歷史記錄裡的操作者始終對得上。停用或重設密碼會讓該帳號已有的登錄立刻失效。</p>
    ${SITE.auth.enabled ? "" : `<div class="msg warn">登錄功能目前<b>沒有啟用</b>：所有人都不用登錄，操作者一律記為 local。可以先在這裡把帳號建好；啟用方法見 README 的「登錄」一節。</div>`}
    <div class="panel"><h2>新增帳號</h2>
      <div class="row"><label>用戶名 <input id="u-name" size="16" autocomplete="off"></label>
        <label>初始密碼 <input id="u-pass" type="password" size="18" autocomplete="new-password" placeholder="至少 10 個字符"></label>
        <label>角色 <select id="u-role">${Object.entries(ROLES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
        <button class="btn primary" id="u-add">建立</button></div><div id="u-msg"></div></div>
    <div class="panel"><h2>現有帳號</h2>${users.length ? `<div class="scroll"><table>
      <tr><th>用戶名</th><th>角色</th><th>狀態</th><th>建立（${zoneLabel()}）</th><th>上次登錄（${zoneLabel()}）</th><th>重設密碼</th><th></th></tr>
      ${users.map((u) => `<tr><td><b>${esc(u.username)}</b>${SITE.auth.user?.username === u.username ? ' <span class="tag">你</span>' : ""}</td>
        <td><select data-role="${u.id}" aria-label="角色">${Object.entries(ROLES).map(([k, v]) => `<option value="${k}" ${k === u.role ? "selected" : ""}>${v}</option>`).join("")}</select></td>
        <td>${u.active ? '<span class="tag ok">可用</span>' : '<span class="tag warn">已停用</span>'}</td>
        <td class="nw">${time(u.created_at)}</td><td class="nw">${u.last_login_at ? time(u.last_login_at) : '<span class="muted">從未</span>'}</td>
        <td class="nw"><input type="password" size="16" data-pass="${u.id}" placeholder="新密碼" autocomplete="new-password"> <button class="btn" data-reset="${u.id}">重設</button></td>
        <td><button class="btn" data-active="${u.id}" data-to="${!u.active}">${u.active ? "停用" : "啟用"}</button></td></tr>`).join("")}</table></div>`
      : '<p class="muted">還沒有帳號。</p>'}<div id="t-msg"></div></div>`;
  const $ = (id) => main.querySelector("#" + id);
  const patch = async (id, body, done) => {
    try { await api(`/users/${id}`, { method: "PATCH", json: body }); done ? done() : await render(main); }
    catch (e) { $("t-msg").innerHTML = errorBox(e); }
  };
  $("u-add").addEventListener("click", async () => {
    try {
      await api("/users", { json: { username: $("u-name").value, password: $("u-pass").value, role: $("u-role").value } });
      await render(main);
    } catch (e) { $("u-msg").innerHTML = errorBox(e); }
  });
  main.querySelectorAll("[data-role]").forEach((s) => s.addEventListener("change", () => patch(s.dataset.role, { role: s.value })));
  main.querySelectorAll("[data-active]").forEach((b) => b.addEventListener("click", () => patch(b.dataset.active, { active: b.dataset.to === "true" })));
  main.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", () => {
    const input = main.querySelector(`[data-pass="${b.dataset.reset}"]`);
    patch(b.dataset.reset, { password: input.value }, () => { input.value = ""; $("t-msg").innerHTML = '<div class="msg ok">密碼已重設，該帳號已有的登錄已失效。</div>'; });
  }));
}
