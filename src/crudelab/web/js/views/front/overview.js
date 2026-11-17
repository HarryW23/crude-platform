import { api } from "../../api.js";
import { show } from "../../app.js";
import { adminLink } from "../../site.js";
import { deltaCell, esc, num, statusTag } from "../../ui.js";

export async function render(main) {
  const [o, models, indicators, balances, sig, fresh] = await Promise.all(
    [api("/overview"), api("/refineries"), api("/indicators"), api("/balances"), api("/signals"), api("/freshness")]);
  const headline = ["SPR_BRENT_WTI", "SPR_BRENT_DUBAI", "SPR_SC_BRENT", "TS_BRENT_M1_M3", "TS_WTI_M1_M3",
                    "CRK_US_321", "CRK_ICE_GASOIL", "CRK_SG_GASOIL", "STK_US_CRUDE_VS_5Y", "DEM_US_TOTAL_YOY"]
    .map((code) => indicators.find((i) => i.code === code)).filter(Boolean);
  const todo = [];
  if (fresh.problems) {
    const names = fresh.rows.filter((r) => r.status === "stale" || r.status === "never").slice(0, 4).map((r) => esc(r.name_zh));
    todo.push(`<b>有 ${fresh.problems} 條序列超過期限沒有更新</b>（${names.join("、")}${fresh.problems > 4 ? " 等" : ""}），用到它們的數字可能是舊的。詳見${adminLink("series", "序列")}。`);
  }
  if (!o.observations) todo.push(`還沒有任何數據。到${adminLink("import", "數據導入")}上傳 Excel，或在${adminLink("series", "序列")}手動錄入。`);
  if (o.parameters_missing) todo.push(`有 ${o.parameters_missing} 個必填參數還沒有數值（例如運費基準費率），用到它們的 ${o.indicators_missing_parameter} 個指標暫時算不出來。到${adminLink("params", "參數")}錄入。`);
  if (o.parameters_unverified) todo.push(`有 ${o.parameters_unverified} 個參數是暫定值，尚待核對（收率、加工費、稅率、換算係數）。在${adminLink("params", "參數")}核對。`);

  main.innerHTML = `
    <div class="tiles">
      <button class="tile" id="to-outlook"><div class="k">綜合方向信號</div><div class="v">${esc(sig.overall.label)}</div>
        <div class="d">${sig.modules.map((m) => `${m.name_zh} ${m.label}`).join("　")}</div></button>
      <div class="tile"><div class="k">最新數據日期</div><div class="v">${o.last_obs_date ?? "—"}</div><div class="d">${num(o.observations, 0)} 個數據點</div></div>
      <div class="tile"><div class="k">有數據的序列</div><div class="v">${o.series_with_data} / ${o.series_total}</div></div>
      <div class="tile"><div class="k">可計算的指標</div><div class="v">${o.indicators_ok} / ${o.indicators_total}</div>
        <div class="d">缺數據 ${o.indicators_missing_data}，缺參數 ${o.indicators_missing_parameter}</div></div>
      <div class="tile"><div class="k">待核對的參數</div><div class="v">${o.parameters_unverified} / ${o.parameters_total}</div></div>
    </div>
    ${todo.map((t) => `<div class="msg">${t}</div>`).join("")}
    <div class="cols"><div class="panel"><h2>煉廠毛利</h2><p class="sub">各模型最新一天的毛利。點一行進入拆解和試算。</p>
      <div class="scroll"><table><tr><th>模型</th><th>日期</th><th class="n">毛利</th><th class="n">日變化</th><th class="n">20 日</th><th>單位</th><th>狀態</th></tr>
      ${models.map((m) => `<tr class="click" data-model="${m.code}"><td>${esc(m.name_zh)}</td><td class="nw">${m.margin.last_date ?? ""}</td>
        <td class="n">${m.margin.status === "ok" ? num(m.margin.last_value) : "—"}</td>${deltaCell(m.margin.chg_1d)}${deltaCell(m.margin.chg_20d)}
        <td class="nw">${esc(m.unit)}</td><td>${statusTag(m.margin)}</td></tr>`).join("")}</table></div>
      <h2 style="margin-top:22px">供需平衡</h2><p class="sub">各平衡表最新一期的平衡（供應 − 需求，正為累庫）。點一行進入平衡表。</p>
      <div class="scroll"><table><tr><th>平衡表</th><th>期間</th><th class="n">平衡</th><th class="n">上期變化</th><th>單位</th><th>狀態</th></tr>
      ${balances.map((b) => `<tr class="click" data-balance="${b.code}"><td>${esc(b.name_zh)}</td><td class="nw">${b.balance.last_date ?? ""}</td>
        <td class="n">${b.balance.status === "ok" ? num(b.balance.last_value) : "—"}</td>${deltaCell(b.balance.chg_1d)}
        <td class="nw">${esc(b.unit)}</td><td>${statusTag(b.balance)}</td></tr>`).join("")}</table></div></div>
    <div class="panel"><h2>關鍵價差</h2><p class="sub">完整列表見「地域價差」和「煉廠盈利」。</p>
      <div class="scroll"><table><tr><th>指標</th><th>日期</th><th class="n">最新值</th><th class="n">上期變化</th><th class="n">20 期</th><th>單位</th><th>狀態</th></tr>
      ${headline.map((i) => `<tr><td>${esc(i.name_zh)}</td><td class="nw">${i.last_date ?? ""}</td>
        <td class="n">${i.status === "ok" ? num(i.last_value) : "—"}</td>${deltaCell(i.chg_1d)}${deltaCell(i.chg_20d)}
        <td class="nw">${esc(i.unit)}</td><td>${statusTag(i)}</td></tr>`).join("")}</table></div></div></div>`;
  main.querySelector("#to-outlook").addEventListener("click", () => show("outlook"));
  main.querySelectorAll("tr[data-balance]").forEach((tr) => tr.addEventListener("click", () => {
    sessionStorage.setItem("balance-table", tr.dataset.balance);
    show("balance");
  }));
  main.querySelectorAll("tr[data-model]").forEach((tr) => tr.addEventListener("click", () => {
    sessionStorage.setItem("refinery-model", tr.dataset.model);
    show("refinery");
  }));
}
