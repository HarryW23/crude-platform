// 入口：導覽與頁面切換。前台和後台各有自己的一組頁面（views/front、views/admin），由 <body data-site> 決定載入哪一組。
import { api } from "./api.js";
import { loadSite, SITE } from "./site.js";
import { currentZone, errorBox, esc, setZone, ZONES } from "./ui.js";

const PAGES = {
  front: [["overview", "總覽"], ["spreads", "地域價差"], ["balance", "供需平衡"], ["refinery", "煉廠盈利"],
          ["events", "地緣事件"], ["outlook", "推演"], ["backtest", "回測"], ["reports", "報告"],
          ["chat", "問答"]],
  admin: [["import", "數據導入"], ["series", "序列"], ["indicators", "指標"], ["params", "參數"],
          ["sources", "數據源"], ["schedule", "定時任務"], ["backups", "備份"], ["llm", "大模型"], ["users", "帳號"], ["audit", "日誌"]],
}[SITE.site];
const IDS = PAGES.map(([id]) => id);
const main = document.getElementById("main");
const nav = document.getElementById("nav");

export async function show(id) {
  if (!IDS.includes(id)) id = IDS[0];
  if (location.hash.slice(1) !== id) history.replaceState(null, "", "#" + id);
  nav.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.page === id));
  main.innerHTML = '<p class="muted">載入中…</p>';
  try {
    const view = await import(`./views/${SITE.site}/${id}.js`);
    await view.render(main);
  } catch (e) {
    main.innerHTML = errorBox(e);
  }
}

for (const [id, name] of PAGES) {
  const button = document.createElement("button");
  button.dataset.page = id;
  button.textContent = name;
  button.addEventListener("click", () => show(id));
  nav.append(button);
}

// ------------------------------ 登錄 ------------------------------
// 登錄功能由伺服器的設定決定開不開。沒開時 SITE.auth.enabled 為 false，下面這些都不會出現。

function showLogin(message = "") {
  nav.hidden = true;
  document.getElementById("account").innerHTML = "";
  main.innerHTML = `<form class="panel login" id="login"><h2>登錄${SITE.site === "admin" ? "後台" : ""}</h2>
    ${message ? `<div class="msg warn">${esc(message)}</div>` : ""}
    <label>用戶名<input id="l-user" autocomplete="username" required autofocus></label>
    <label>密碼<input id="l-pass" type="password" autocomplete="current-password" required></label>
    <button class="btn primary">登錄</button><div id="l-msg"></div></form>`;
  main.querySelector("#login").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await api("/auth/login", { json: { username: main.querySelector("#l-user").value, password: main.querySelector("#l-pass").value } });
      location.reload();
    } catch (e) { main.querySelector("#l-msg").innerHTML = errorBox(e); }
  });
}

function showAccount() {
  nav.querySelectorAll("button").forEach((b) => b.classList.remove("on"));
  const u = SITE.auth.user;
  main.innerHTML = `<form class="panel login" id="pw"><h2>${esc(u.username)} <span class="tag">${esc(u.role_name)}</span></h2>
    <p class="sub">修改密碼後，這個帳號在其他瀏覽器和設備上的登錄都會失效。</p>
    <label>目前的密碼<input id="p-old" type="password" autocomplete="current-password" required></label>
    <label>新密碼（至少 10 個字符）<input id="p-new" type="password" autocomplete="new-password" minlength="10" required></label>
    <label>再輸入一次新密碼<input id="p-again" type="password" autocomplete="new-password" required></label>
    <button class="btn primary">修改密碼</button><div id="p-msg"></div></form>`;
  main.querySelector("#pw").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = main.querySelector("#p-msg"), fresh = main.querySelector("#p-new").value;
    if (fresh !== main.querySelector("#p-again").value) { msg.innerHTML = errorBox(new Error("兩次輸入的新密碼不一樣。")); return; }
    try {
      await api("/auth/password", { json: { current_password: main.querySelector("#p-old").value, new_password: fresh } });
      msg.innerHTML = '<div class="msg ok">密碼已修改。</div>';
      ev.target.reset();
    } catch (e) { msg.innerHTML = errorBox(e); }
  });
}

let ready = false;
try {
  await loadSite();
  const other = document.getElementById("other-site");
  other.href = SITE.site === "front" ? SITE.admin_url : SITE.front_url;
  other.textContent = SITE.site === "front" ? "後台 ↗" : "前台 ↗";
  // 時區切換：頁面上的時間都按這裡選的時區顯示，換了之後當前頁面重畫
  const tz = document.createElement("select");
  tz.id = "tz";
  tz.className = "tz";
  tz.title = "頁面上的時間按哪個時區顯示";
  tz.setAttribute("aria-label", "顯示時區");
  tz.innerHTML = ZONES.map(([z, name]) => `<option value="${z}" ${z === currentZone() ? "selected" : ""}>${name === "UTC" ? "UTC" : name + "時間"}</option>`).join("");
  tz.addEventListener("change", () => setZone(tz.value));
  other.before(tz);
  window.addEventListener("crudelab:timezone", () => { if (ready) show(location.hash.slice(1)); });
  if (SITE.config_error) {
    document.getElementById("banner").innerHTML = `<div class="msg bad" style="margin:12px 28px 0;white-space:pre-wrap">config/ 裡的配置有錯，目前沿用上一份正確的配置：\n${esc(SITE.config_error)}</div>`;
  }
  const { enabled, user } = SITE.auth;
  if (enabled && !user) {
    showLogin();
  } else if (enabled && SITE.site === "admin" && user.role !== "admin") {
    nav.hidden = true;
    main.innerHTML = `<div class="panel login"><h2>沒有權限</h2><p class="sub">後台只有管理員能進入。你目前以「${esc(user.username)}」（${esc(user.role_name)}）登錄。</p>
      <div class="row"><a class="btn" style="text-decoration:none" href="${esc(SITE.front_url)}">回到前台</a><button class="btn" id="out">登出</button></div></div>`;
    main.querySelector("#out").addEventListener("click", async () => { await api("/auth/logout", { method: "POST" }); location.reload(); });
  } else {
    ready = true;
    if (user) {
      const account = document.getElementById("account");
      account.innerHTML = `<button class="link" id="me" title="修改密碼">${esc(user.username)}（${esc(user.role_name)}）</button><button class="link" id="logout">登出</button>`;
      account.querySelector("#me").addEventListener("click", showAccount);
      account.querySelector("#logout").addEventListener("click", async () => { await api("/auth/logout", { method: "POST" }); location.reload(); });
      window.addEventListener("crudelab:login-required", () => { ready = false; showLogin("登錄已過期，請重新登錄。"); });
    }
  }
} catch (e) {
  document.getElementById("banner").innerHTML = errorBox(e);
}
// 後台：有定時任務最近一次是失敗時，在「定時任務」的導覽上顯示紅點（不用配置任何通知渠道也有）
async function refreshAlerts() {
  if (SITE.site !== "admin" || !ready) return;
  const button = nav.querySelector('button[data-page="schedule"]');
  try {
    const status = await api("/schedule");
    button.querySelector(".dot")?.remove();
    if (status.failing.length) {
      button.insertAdjacentHTML("beforeend", `<span class="dot" title="有 ${status.failing.length} 個任務最近一次執行失敗"></span>`);
    }
  } catch { /* 取不到就不顯示，不打擾 */ }
}
window.addEventListener("crudelab:refresh-alerts", refreshAlerts);
refreshAlerts();
setInterval(refreshAlerts, 60000);

window.addEventListener("hashchange", () => { if (ready) show(location.hash.slice(1)); });
if (ready) show(location.hash.slice(1));
