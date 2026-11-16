import { api } from "../api.js";
import { esc, time } from "../ui.js";

const ACTION = { import: "導入文件", manual_entry: "手動錄入", pull: "接口取數", purge: "清除數據" };

export async function render(main) {
  const rows = await api("/audit");
  main.innerHTML = `<div class="panel"><h2>操作日誌</h2>
    <p class="sub">每一次數據寫入都記在這裡。單個數據點的改寫記錄在各序列的「修訂歷史」，參數的改動在各參數的「版本記錄」。</p>
    ${rows.length ? `<div class="scroll"><table><tr><th>時間（UTC）</th><th>操作</th><th>對象</th><th>詳情</th><th>操作者</th></tr>
      ${rows.map((a) => `<tr><td class="nw">${time(a.ts)}</td><td class="nw">${ACTION[a.action] ?? esc(a.action)}</td><td class="mono">${esc(a.entity_id)}</td>
        <td class="mono">${esc(JSON.stringify(a.detail))}</td><td>${esc(a.actor)}</td></tr>`).join("")}</table></div>` : '<p class="muted">還沒有記錄。</p>'}</div>`;
}
