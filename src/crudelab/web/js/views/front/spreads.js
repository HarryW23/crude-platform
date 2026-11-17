import { api } from "../../api.js";
import { mountBoard } from "../../indicator-board.js";

export async function render(main) {
  const list = await api("/indicators?module=spread");
  main.innerHTML = `<h2>地域價差</h2>
    <p class="sub">基準價差、月差結構、各油種現貨絕對價、運費、到岸成本與套利窗口。點任一行看走勢和計算追溯。
    升貼水與官價的計價期用 M1 或當日現貨近似。</p><div id="board"></div>`;
  mountBoard(main.querySelector("#board"), list);
}
