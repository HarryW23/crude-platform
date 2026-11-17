// 入口：導覽與頁面切換。每個頁面是 views/ 下的一個模組，導出 render(main)。
import { errorBox } from "./ui.js";

const PAGES = [
  { group: "前台", items: [["overview", "總覽"], ["spreads", "地域價差"], ["balance", "供需平衡"], ["refinery", "煉廠盈利"]] },
  { group: "後台", items: [["import", "數據導入"], ["series", "序列"], ["indicators", "指標"], ["params", "參數"],
                           ["sources", "數據源"], ["audit", "日誌"]] },
];
const IDS = PAGES.flatMap((g) => g.items.map(([id]) => id));
const main = document.getElementById("main");
const nav = document.getElementById("nav");

export async function show(id) {
  if (!IDS.includes(id)) id = "overview";
  if (location.hash.slice(1) !== id) history.replaceState(null, "", "#" + id);
  nav.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.page === id));
  main.innerHTML = '<p class="muted">載入中…</p>';
  try {
    const view = await import(`./views/${id}.js`);
    await view.render(main);
  } catch (e) {
    main.innerHTML = errorBox(e);
  }
}

PAGES.forEach((group, gi) => {
  if (gi) nav.insertAdjacentHTML("beforeend", '<span class="sep"></span>');
  nav.insertAdjacentHTML("beforeend", `<span class="label">${group.group}</span>`);
  for (const [id, name] of group.items) {
    const button = document.createElement("button");
    button.dataset.page = id;
    button.textContent = name;
    button.addEventListener("click", () => show(id));
    nav.append(button);
  }
});
window.addEventListener("hashchange", () => show(location.hash.slice(1)));
show(location.hash.slice(1));
