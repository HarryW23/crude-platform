import { api } from "../../api.js";
import { errorBox, esc, num, time, zoneLabel } from "../../ui.js";

const since = (d) => d == null ? "" : d === "1900-01-01" ? "自始" : d;

export async function render(main) {
  const rows = await api("/parameters");
  main.innerHTML = `<h2>參數</h2>
    <p class="sub">公式和模型裡用到的所有常數。每個值都帶生效日期：填了生效日期，只影響那天之後的計算，歷史結果不變；不填則視為更正目前的值。所有改動都保留。</p>
    <div class="row" style="margin-bottom:14px"><input id="q" placeholder="搜尋" size="20" aria-label="搜尋">
      <label><input type="checkbox" id="todo"> 只看待處理（缺值或待核對）</label></div><div id="calib"></div><div id="editor"></div><div id="list"></div>`;
  const $ = (id) => main.querySelector("#" + id);
  calibration($("calib"), () => render(main));

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
    const [versions, est] = await Promise.all([api(`/parameters/${key}/versions`), api("/projection/sensitivity")]);
    const isSensitivity = est.parameter === key;
    const box = $("editor");
    box.innerHTML = `<div class="panel inset"><h2>${esc(p.name_zh)} <span class="mono muted">${key}</span></h2>
      <p class="sub">${esc(p.description ?? "")}${p.used_by.length ? `<br>用到它的指標：<span class="mono">${p.used_by.join("、")}</span>` : ""}</p>
      <div class="row"><label>新數值 <input id="pv" class="num" value="${p.value ?? ""}" inputmode="decimal"></label>
        <span class="ink2">${p.unit === "1" ? "" : esc(p.unit)}</span>
        <label>生效日期 <input type="date" id="pd"></label><span class="muted">不填 = 更正目前的值</span>
        <label>原因 <input id="pn" size="18"></label><button class="btn primary" id="psave">保存</button>
        ${p.value != null && !p.verified ? '<button class="btn" id="pverify">數值無誤，標為已核對</button>' : ""}</div><div id="pmsg"></div>
      ${isSensitivity ? `<div class="msg ${est.status === "ok" ? "" : "warn"}">歷史估計：${est.value == null ? "沒有結果" : `<b>${num(est.value, 3)}</b>（95% 區間 ${num(est.low, 3)} – ${num(est.high, 3)}，t = ${num(est.t, 1)}，${est.n} 個月）`}。${esc(est.message)}。
        ${est.status === "ok" ? '<button class="btn" id="padopt" style="margin-left:8px">採用這個估計（今天起生效）</button>' : ""}</div>` : ""}
      <h3>版本記錄</h3><div class="scroll"><table><tr><th>生效自</th><th class="n">數值</th><th>原因</th><th>錄入者</th><th>錄入時間（${zoneLabel()}）</th><th></th></tr>
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
    box.querySelector("#padopt")?.addEventListener("click", async () => {
      try { await api("/projection/sensitivity/adopt", { method: "POST" }); await render(main); } catch (e) { fail(e); }
    });
    box.querySelector("#pverify")?.addEventListener("click", async () => {
      try { await api(`/parameters/${key}/verify`, { json: { verified: true } }); await render(main); } catch (e) { fail(e); }
    });
  };

  $("q").addEventListener("input", draw);
  $("todo").addEventListener("change", draw);
  draw();
}

// 按最近一次回測校準信號和模組權重。逐步前推檢驗沒有變好時不能採用（後端也這樣限制）
async function calibration(box, reload) {
  let c;
  try { c = await api("/calibration/latest"); } catch (e) { box.innerHTML = errorBox(e); return; }
  if (!c.run_id) { box.innerHTML = ""; return; }
  const w = c.walk_forward;
  box.innerHTML = `<div class="panel inset"><h2>按回測校準權重 <span class="muted">回測 #${c.run_id}，${time(c.finished_at)}</span></h2>
    <p class="sub">${esc(c.why)}${w ? `（逐步前推 IC：現在 ${w.current.ic == null ? "—" : num(w.current.ic, 3)} → 校準 ${w.calibrated.ic == null ? "—" : num(w.calibrated.ic, 3)}）` : ""}。
      看 ${esc(c.horizon_name)} 的預測力；明細在前台「回測」頁。採用後今天起生效，舊版本保留，歷史回測不受影響。</p>
    ${c.changes.length ? `<div class="scroll"><table><tr><th>參數</th><th class="n">現在</th><th class="n">建議</th><th>理由</th></tr>
      ${c.changes.map((x) => `<tr><td>${esc(x.name_zh)}<div class="mono muted">${x.parameter}</div></td><td class="n">${x.current ?? "—"}</td>
        <td class="n"><b>${x.proposed}</b></td><td>${esc(x.reason)}</td></tr>`).join("")}</table></div>
      ${c.adoptable ? '<button class="btn primary" id="cadopt">採用這些權重（今天起生效）</button>' : ""}`
      : '<p class="muted">建議和現在的權重一樣，不需要改。</p>'}<div id="cmsg"></div></div>`;
  box.querySelector("#cadopt")?.addEventListener("click", async () => {
    try { await api(`/calibration/${c.run_id}/adopt`, { method: "POST" }); await reload(); }
    catch (e) { box.querySelector("#cmsg").innerHTML = errorBox(e); }
  });
}
