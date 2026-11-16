import { api } from "../api.js";
import { mountBoard } from "../indicator-board.js";
import { MODULES } from "../ui.js";

export async function render(main) {
  main.innerHTML = `<h2>指標</h2>
    <p class="sub">全部指標及其公式，定義在 config/indicators/ 和 config/refineries/。點一行看走勢，以及最新一個值是由哪些數據和參數算出來的。</p>
    <div class="row" style="margin-bottom:14px">
      <select id="mod" aria-label="模組"><option value="">全部模組</option>${Object.entries(MODULES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
      <label><input type="checkbox" id="hidden"> 顯示中間量</label></div><div id="board"></div>`;
  const draw = async () => {
    const query = new URLSearchParams();
    if (main.querySelector("#mod").value) query.set("module", main.querySelector("#mod").value);
    if (main.querySelector("#hidden").checked) query.set("include_hidden", "true");
    mountBoard(main.querySelector("#board"), await api("/indicators?" + query), { formula: true });
  };
  main.querySelector("#mod").addEventListener("change", draw);
  main.querySelector("#hidden").addEventListener("change", draw);
  await draw();
}
