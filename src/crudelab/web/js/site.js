// 站點信息：現在是前台還是後台、另一個站點的地址。前台和後台是兩個獨立的站點，頁面之間用完整網址互相連結。
import { api } from "./api.js";
import { esc } from "./ui.js";

export const SITE = { site: document.body.dataset.site, front_url: "", admin_url: "", config_error: null };

export async function loadSite() {
  Object.assign(SITE, await api("/site"));
  return SITE;
}

// 指向後台某一頁的連結（已在後台時就是站內連結）。回傳 HTML 字串。
export function adminLink(page, label) {
  return SITE.site === "admin" ? `<a href="#${page}">「${esc(label)}」頁</a>`
    : `<a href="${esc(SITE.admin_url)}/#${page}" target="_blank" rel="noopener">後台的「${esc(label)}」頁</a>`;
}
