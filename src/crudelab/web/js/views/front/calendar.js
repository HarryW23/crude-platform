// 財經日曆（來自金十）：之後幾周要公佈的數據和事件，以及最近公佈的「實際 vs 預期」。
// 預期在公佈前可能變動：實際值第一次出現時系統把當時的預期凍結下來，「超預期」用的是凍結的預期。
import { api } from "../../api.js";
import { adminLink } from "../../site.js";
import { esc, signed, time, zoneLabel } from "../../ui.js";

const state = { minStar: 3 };
const stars = (n) => (n ? "★".repeat(Math.min(n, 5)) : "");

export async function render(main) {
  const d = await api(`/calendar?days_back=7&days_ahead=28&min_star=${state.minStar}`);
  const upcoming = d.events.filter((e) => e.upcoming), recent = d.events.filter((e) => !e.upcoming).reverse();
  main.innerHTML = `<h2>財經日曆</h2>
    <p class="sub">之後四周要公佈的數據和事件，以及最近一周已公佈的結果（來自金十，每 2 小時同步）。做 1 個月判斷時，先看清楚期間有哪些關鍵節點。
    標了「跟蹤」的項目（例如 EIA 原油庫存），系統會把「實際 − 公佈時的預期」記成序列，積累後用於信號和回測。</p>
    ${d.configured ? "" : `<div class="msg warn">還沒有設定金十的 token（在服務器上運行 <span class="mono">crudelab secret set JIN10_TOKEN</span>），日曆不會更新。設好後可以在${adminLink("schedule", "定時任務")}點「同步財經日曆」立即取一次。</div>`}
    <div class="row" style="margin-bottom:12px"><span class="ink2">重要性</span><div class="tabs">${[[0, "全部"], [2, "★★ 以上"], [3, "★★★ 以上"], [4, "★★★★ 以上"]].map(([v, n]) => `<button data-star="${v}" class="${v === state.minStar ? "on" : ""}">${n}</button>`).join("")}</div>
      <span class="muted">時間按${esc(zoneLabel())}顯示</span></div>
    <div class="panel"><h2>之後四周</h2>${table(upcoming, false)}</div>
    <div class="panel"><h2>最近一周已公佈</h2>${table(recent, true)}</div>`;
  main.querySelectorAll("[data-star]").forEach((b) => b.addEventListener("click", () => { state.minStar = Number(b.dataset.star); render(main); }));
}

function table(rows, released) {
  if (!rows.length) return '<p class="muted">沒有符合條件的項目。</p>';
  return `<div class="scroll"><table><tr><th>時間</th><th>重要性</th><th>項目</th><th class="n">前值</th><th class="n">預期</th>${released ? '<th class="n">實際</th><th class="n">實際 − 預期</th><th>說明</th>' : ""}</tr>
    ${rows.map((e) => `<tr><td class="nw">${time(e.pub_time)}</td><td class="nw" style="color:var(--warn)">${stars(e.star)}</td>
      <td>${esc(e.title)}${e.tracked ? ' <span class="tag ok" title="超預期會記成序列">跟蹤</span>' : ""}</td>
      <td class="n">${esc(e.previous ?? "—")}</td><td class="n">${esc(e.consensus ?? "—")}</td>
      ${released ? `<td class="n"><b>${esc(e.actual ?? "—")}</b>${e.revised ? `<div class="muted">前值修正 ${esc(e.revised)}</div>` : ""}</td>
        <td class="n ${e.surprise > 0 ? "up" : e.surprise < 0 ? "down" : ""}">${e.surprise == null ? "—" : signed(e.surprise, Math.abs(e.surprise) >= 100 ? 0 : 2)}</td>
        <td class="nw">${esc(e.affect ?? "")}</td>` : ""}</tr>`).join("")}</table></div>`;
}
