// 入口：導覽與頁面切換。前台和後台各有自己的一組頁面（views/front、views/admin），由 <body data-site> 決定載入哪一組。
import { loadSite, SITE } from "./site.js";
import { errorBox, esc } from "./ui.js";

const PAGES = {
  front: [["overview", "總覽"], ["spreads", "地域價差"], ["balance", "供需平衡"], ["refinery", "煉廠盈利"],
          ["events", "地緣事件"], ["outlook", "推演"], ["reports", "報告"], ["chat", "問答"]],
  admin: [["import", "數據導入"], ["series", "序列"], ["indicators", "指標"], ["params", "參數"],
          ["sources", "數據源"], ["audit", "日誌"]],
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

try {
  await loadSite();
  const other = document.getElementById("other-site");
  other.href = SITE.site === "front" ? SITE.admin_url : SITE.front_url;
  other.textContent = SITE.site === "front" ? "後台 ↗" : "前台 ↗";
  if (SITE.config_error) {
    document.getElementById("banner").innerHTML = `<div class="msg bad" style="margin:12px 28px 0;white-space:pre-wrap">config/ 裡的配置有錯，目前沿用上一份正確的配置：\n${esc(SITE.config_error)}</div>`;
  }
} catch (e) {
  document.getElementById("banner").innerHTML = errorBox(e);
}
window.addEventListener("hashchange", () => show(location.hash.slice(1)));
show(location.hash.slice(1));
