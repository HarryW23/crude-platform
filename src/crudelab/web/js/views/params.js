import { api } from "../api.js";
import { errorBox, esc, time } from "../ui.js";

const since = (d) => d == null ? "" : d === "1900-01-01" ? "自始" : d;

export async function render(main) {
  const rows = await api("/parameters");
  main.innerHTML = `<h2>參數</h2>
    <p class="sub">公式和模型裡用到的所有常數。每個值都帶生效日期：填了生效日期，只影響那天之後的計算，歷史結果不變；不填則視為更正目前的值。所有改動都保留。</p>
    <div class="row" style="margin-bottom:14px"><input id="q" placeholder="搜尋" size="20" aria-label="搜尋">
      <label><input type="checkbox" id="todo"> 只看待處理（缺值或待核對）</label></div><div id="editor"></div><div id="list"></div>`;
  const $ = (id) => main.querySelector("#" + id);

  const draw = () => {
    const q = $("q").value.toLowerCase(), todo = $("todo").checked;
    const list = rows.filter((p) => (!q || (p.key + p.name_zh + p.group).toLowerCase().includes(q)) && (!todo || p.value == null || !p.verified));
    const groups = new Map();
    for (const p of list) { if (!groups.has(p.group)) groups.set(p.group, []); groups.get(p.group).push(p); }
    $("list").innerHTML = [...groups].map(([name, items]) => `<div class="panel"><h2>${esc(name)}</h2><div class="scroll"><table>
      <tr><th>參數</th><th class="n">當前值</th><th>單位</th><th>生效自</th><th>狀態</th><th>說明</th><th class="n">用到的指標</th></tr>
      ${items.map((p) => `<tr class="click" data-key="${p.key}"><td>${esc(p.name_zh)}<div class="mono muted">${p.key}</div></td>
        <td class="n">${p.value ?? "—"}</td><td class="nw">${p.unit === "1" ? "" : esc(p.unit)}</td><td class="nw">${since(p.valid_from)}${p.n_versions > 1 ? ` <span class="muted">（共 ${p.n_versions} 段）</span>` : ""}</td>
        <td>${p.value == null ? '<span class="tag warn">缺值</span>' : p.verified ? '<span class="tag ok">已核對</span>' : '<span class="tag warn">待核對</span>'}</td>
        <td>${esc(p.description ?? "")}</td><td class="n">${p.used_by.length}</td></tr>`).join("")}</table></div></div>`).join("")
      || '<p class="muted">沒有符合條件的參數。</p>';
    main.querySelectorAll("tr[data-key]").forEach((tr) => tr.addEventListener("click", () => edit(tr.dataset.key)));
  };

  const edit = async (key) => {
    const p = rows.find((r) => r.key === key);
    const versions = await api(`/parameters/${key}/versions`);
    const box = $("editor");
    box.innerHTML = `<div class="panel inset"><h2>${esc(p.name_zh)} <span class="mono muted">${key}</span></h2>
      <p class="sub">${esc(p.description ?? "")}${p.used_by.length ? `<br>用到它的指標：<span class="mono">${p.used_by.join("、")}</span>` : ""}</p>
      <div class="row"><label>新數值 <input id="pv" class="num" value="${p.value ?? ""}" inputmode="decimal"></label>
        <span class="ink2">${p.unit === "1" ? "" : esc(p.unit)}</span>
        <label>生效日期 <input type="date" id="pd"></label><span class="muted">不填 = 更正目前的值</span>
        <label>原因 <input id="pn" size="18"></label><button class="btn primary" id="psave">保存</button>
        ${p.value != null && !p.verified ? '<button class="btn" id="pverify">數值無誤，標為已核對</button>' : ""}</div><div id="pmsg"></div>
      <h3>版本記錄</h3><div class="scroll"><table><tr><th>生效自</th><th class="n">數值</th><th>原因</th><th>錄入者</th><th>錄入時間（UTC）</th><th></th></tr>
        ${versions.map((v) => `<tr><td>${since(v.valid_from)}</td><td class="n">${v.value}</td><td>${esc(v.note ?? "")}</td><td>${esc(v.created_by)}</td>
          <td>${time(v.created_at)}</td><td>${v.effective ? "" : '<span class="tag">已被更正</span>'}</td></tr>`).join("") || '<tr><td colspan="6" class="muted">還沒有數值。</td></tr>'}</table></div></div>`;
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    const fail = (e) => { box.querySelector("#pmsg").innerHTML = errorBox(e); };
    box.querySelector("#psave").addEventListener("click", async () => {
      const raw = box.querySelector("#pv").value.trim(), value = Number(raw);
      if (raw === "" || !Number.isFinite(value)) { fail(new Error("請輸入數字。")); return; }
      try {
        await api(`/parameters/${key}/versions`, { json: { value, valid_from: box.querySelector("#pd").value || null, note: box.querySelector("#pn").value || null } });
        await render(main);
      } catch (e) { fail(e); }
    });
    box.querySelector("#pverify")?.addEventListener("click", async () => {
      try { await api(`/parameters/${key}/verify`, { json: { verified: true } }); await render(main); } catch (e) { fail(e); }
    });
  };

  $("q").addEventListener("input", draw);
  $("todo").addEventListener("change", draw);
  draw();
}
