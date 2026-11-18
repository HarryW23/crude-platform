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

export const time = (iso) => String(iso ?? "").slice(0, 16).replace("T", " ");

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
