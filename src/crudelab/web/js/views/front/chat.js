import { api } from "../../api.js";
import { errorBox, esc } from "../../ui.js";

// 對話只保存在這個瀏覽器分頁裡；關掉分頁就沒有了。伺服器只在操作日誌裡記下問了什麼、用了哪些工具。
const KEY = "chat-turns";
const load = () => { try { return JSON.parse(sessionStorage.getItem(KEY)) ?? []; } catch { return []; } };
const save = (turns) => sessionStorage.setItem(KEY, JSON.stringify(turns.slice(-40)));

const EXAMPLES = ["現在 Brent 和 WTI 的價差是多少，處在一年裡的什麼位置？", "美國原油庫存比五年均值高還是低？",
                  "山東燃料型煉廠的毛利是怎麼構成的？", "各模組的方向信號現在是什麼？",
                  "如果 OPEC 從下個月起每天少供應 1 百萬桶，持續三個月，平衡和價格會怎樣？"];

function turnHtml(t) {
  if (t.role === "user") return `<div class="bubble q">${esc(t.content)}</div>`;
  const steps = t.steps ?? [];
  return `<div class="bubble">${t.guard_passed === false
      ? `<div class="msg bad" style="white-space:normal">數字核對未通過：回答裡的這些數字在查到的數據中找不到，請勿採信：${t.unknown_numbers.map(esc).join("、")}</div>` : ""}${esc(t.content)}
    <details><summary>${steps.length ? `依據：查了 ${steps.length} 次數據（${steps.map((s) => esc(s.tool)).join(" → ")}）` : "依據：這個回答沒有查任何數據"}
      ${t.model ? ` · ${esc(t.provider)} / ${esc(t.model)}` : ""}</summary>
      ${steps.map((s, i) => `<div class="mono" style="margin-top:8px">${i + 1}. ${esc(s.tool)}(${esc(JSON.stringify(s.args))})</div>
        <pre>${esc(JSON.stringify(s.result, null, 1))}</pre>`).join("")}</details></div>`;
}

export async function render(main) {
  const llm = await api("/llm");
  let turns = load();
  main.innerHTML = `<div class="chat"><h2>問答</h2>
    <p class="sub">用自然語言提問。回答時，大模型只能調用系統的查詢工具取數，「如果…會怎樣」也是交給系統去算；回答裡的每個數字都會和查到的數據核對，展開「依據」可以看到每一步查了什麼。問答只能查詢和試算，不會修改任何數據。</p>
    ${llm.configured ? `<p class="ink2"><span class="tag ok">${esc(llm.provider)} / ${esc(llm.model)}</span></p>`
      : `<div class="msg warn">大模型未配置，問答暫時不能用。${esc(llm.message)}。配置方法見 README 的「大模型」一節。</div>`}
    <div id="log"></div>
    <p class="muted" data-readonly-only>只讀帳號不能使用問答（需要研究員權限）。</p>
    <div class="chips" id="chips" data-write>${EXAMPLES.map((q) => `<button>${esc(q)}</button>`).join("")}</div>
    <div class="row" data-write><textarea id="q" rows="2" placeholder="輸入問題，Enter 送出，Shift+Enter 換行" style="flex:1" ${llm.configured ? "" : "disabled"}></textarea>
      <button class="btn primary" id="send" ${llm.configured ? "" : "disabled"}>送出</button>
      <button class="btn" id="clear">清空對話</button></div><div id="msg"></div></div>`;
  const $ = (id) => main.querySelector("#" + id);
  const draw = () => {
    $("log").innerHTML = turns.map(turnHtml).join("");
    $("chips").hidden = turns.length > 0;
    $("log").lastElementChild?.scrollIntoView({ block: "nearest" });
  };
  draw();

  const ask = async () => {
    const question = $("q").value.trim();
    if (!question || $("send").disabled) return;
    const history = turns.map((t) => ({ role: t.role, content: t.content }));
    turns.push({ role: "user", content: question });
    draw();
    $("q").value = "";
    $("send").disabled = true;
    $("msg").innerHTML = '<div class="msg">查詢中… 需要查幾次數據，可能要十幾秒。</div>';
    try {
      const r = await api("/chat", { json: { question, history } });
      turns.push({ role: "assistant", content: r.answer, steps: r.steps, guard_passed: r.guard_passed,
                   unknown_numbers: r.unknown_numbers, provider: r.provider, model: r.model });
      $("msg").innerHTML = "";
    } catch (e) {
      turns.pop();
      $("q").value = question;
      $("msg").innerHTML = errorBox(e);
    }
    save(turns);
    draw();
    $("send").disabled = !llm.configured;
  };
  $("send").addEventListener("click", ask);
  $("q").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); ask(); } });
  $("clear").addEventListener("click", () => { turns = []; save(turns); draw(); });
  main.querySelectorAll("#chips button").forEach((b) => b.addEventListener("click", () => { $("q").value = b.textContent; $("q").focus(); }));
}
