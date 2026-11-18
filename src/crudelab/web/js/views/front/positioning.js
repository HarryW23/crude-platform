import { api } from "../../api.js";
import { mountBoard } from "../../indicator-board.js";

export async function render(main) {
  const list = await api("/indicators?module=positioning");
  main.innerHTML = `<h2>資金持倉</h2>
    <p class="sub">期貨市場上管理基金（商品投機基金）的持倉。WTI 來自 CFTC 每周五發佈的持倉報告（數據截至當周周二），自動取數；
    Brent 來自 ICE 的報告，目前用 Excel 導入。「淨多頭佔總持倉」是跨時期可比的擁擠度：處在高位時做多已經很擁擠，之後回吐的風險大。
    點任一行看走勢和計算追溯。</p><div id="board"></div>`;
  mountBoard(main.querySelector("#board"), list);
}
