import { api } from "../api.js";
import { errorBox, esc, num } from "../ui.js";

const KIND = { manual: "手動", excel: "文件導入", api: "接口拉取" };

export async function render(main) {
  const rows = await api("/sources");
  const state = (s) => s.status === "reserved" ? '<span class="tag">預留</span>'
    : s.pullable && !s.ready ? '<span class="tag warn">未配置</span>' : '<span class="tag ok">可用</span>';
  main.innerHTML = `<div class="panel"><h2>數據源</h2>
    <p class="sub">同一序列同一天有多個來源時，取優先級數字最小的那個；其餘來源的值仍然保留。</p><div id="msg"></div>
    <div class="scroll"><table><tr><th>數據源</th><th>方式</th><th class="n">優先級</th><th>狀態</th><th class="n">數據點</th><th>說明</th><th></th></tr>
    ${rows.map((s) => `<tr><td>${esc(s.name)}<div class="mono muted">${s.code}</div></td><td class="nw">${KIND[s.kind]}</td><td class="n">${s.priority}</td>
      <td>${state(s)}</td><td class="n">${num(s.observations, 0)}</td>
      <td>${esc(s.message ?? s.notes ?? "")}${s.series_supported ? `<div class="muted">已登記 ${s.series_supported} 條序列</div>` : ""}</td>
      <td>${s.pullable && s.ready ? `<button class="btn" data-pull="${s.code}">立即取數</button>` : ""}</td></tr>`).join("")}</table></div></div>`;
  main.querySelectorAll("[data-pull]").forEach((b) => b.addEventListener("click", async () => {
    const msg = main.querySelector("#msg");
    b.disabled = true;
    msg.innerHTML = '<div class="msg">取數中…</div>';
    try {
      const r = await api(`/sources/${b.dataset.pull}/pull`, { json: {} });
      msg.innerHTML = `<div class="msg ok">新增 ${r.inserted}，更新 ${r.updated}，未變 ${r.unchanged}。</div>`;
    } catch (e) { msg.innerHTML = errorBox(e); }
    b.disabled = false;
  }));
}
