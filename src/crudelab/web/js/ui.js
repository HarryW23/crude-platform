// 通用的小工具：轉義、數字格式、狀態標籤。不含業務邏輯。
export const MODULES = { spread: "地域價差", balance: "供需", geo: "地緣", refinery: "煉廠", common: "公用" };

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const num = (v, digits = 2) => v == null || Number.isNaN(v) ? "—"
  : Number(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });

export const signed = (v, digits = 2) => v == null ? "" : (v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v), digits);

// note：給這個變化量加一個標記和說明（例如「含換月跳動」）
export const deltaCell = (v, digits = 2, note = "") =>
  `<td class="n ${v > 0 ? "up" : v < 0 ? "down" : ""}"${note ? ` title="${esc(note)}"` : ""}>${signed(v, digits)}${note ? '<sup class="muted">†</sup>' : ""}</td>`;

// ------------------------------ 時間與時區 ------------------------------
// 接口給的時間都是 UTC（帶 Z；舊接口不帶時區的也按 UTC 理解），這裡按使用者選的時區顯示。
// 選擇記在瀏覽器裡（每個站點各記各的），預設北京時間。洛杉磯時間會自動處理夏令時。
export const ZONES = [["Asia/Shanghai", "北京"], ["America/Los_Angeles", "洛杉磯"], ["UTC", "UTC"]];
const ZONE_KEY = "crudelab.tz";
let zone = ZONES[0][0];
try {
  const saved = localStorage.getItem(ZONE_KEY);
  if (ZONES.some(([z]) => z === saved)) zone = saved;
} catch { /* 瀏覽器不讓用本地存儲時就用預設 */ }

export const currentZone = () => zone;

export function setZone(z) {
  if (!ZONES.some(([k]) => k === z)) return;
  zone = z;
  try { localStorage.setItem(ZONE_KEY, z); } catch { /* 同上 */ }
  window.dispatchEvent(new Event("crudelab:timezone"));
}

function toDate(iso) {
  const s = String(iso);
  return new Date(/(Z|[+-]\d\d:?\d\d)$/.test(s) ? s : s + "Z");
}

// 時區的縮寫，例如 PST / PDT / GMT+8。at 決定是不是夏令時。
export function zoneAbbr(at = new Date(), z = zone) {
  return new Intl.DateTimeFormat("en-US", { timeZone: z, timeZoneName: "short" })
    .formatToParts(at).find((p) => p.type === "timeZoneName")?.value ?? z;
}

// 例如「北京時間」「洛杉磯時間（PST）」「UTC」：放在表頭說明這一列按什麼時區顯示
export function zoneLabel(z = zone) {
  const name = ZONES.find(([k]) => k === z)?.[1] ?? z;
  if (z === "UTC") return "UTC";
  return z === "Asia/Shanghai" ? `${name}時間` : `${name}時間（${zoneAbbr(new Date(), z)}）`;
}

// "2026-11-18 15:20"；seconds 為 true 時帶秒。無效值原樣返回。
export function time(iso, seconds = false) {
  if (iso == null || iso === "") return "";
  const d = toDate(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    second: seconds ? "2-digit" : undefined, hourCycle: "h23" }).formatToParts(d).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}${seconds ? ":" + parts.second : ""}`;
}

export function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

// 指標狀態標籤。缺什麼就把缺的東西列出來。
export function statusTag(s) {
  if (s.status === "ok") {
    return s.unverified_parameters.length
      ? `<span class="tag warn" title="${esc(s.unverified_parameters.join(", "))}">含 ${s.unverified_parameters.length} 項待核對假設</span>`
      : '<span class="tag ok">正常</span>';
  }
  if (s.status === "missing_parameter") {
    return `<span class="tag warn">缺參數</span><div class="mono muted">${esc(s.missing_parameters.join(", "))}</div>`;
  }
  if (s.status === "missing_data") {
    return `<span class="tag warn">缺數據</span><div class="mono muted">${esc(s.missing_series.join(", "))}</div>`;
  }
  return '<span class="tag warn">輸入日期對不上</span>';
}

export const errorBox = (e) => `<div class="msg bad">${esc(e.message ?? e)}</div>`;
