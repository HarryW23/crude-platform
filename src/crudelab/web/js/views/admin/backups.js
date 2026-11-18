// 備份：位置與保留策略、最近的備份、立即備份、校驗。恢復只能用命令行，這裡只給出命令。
import { api } from "../../api.js";
import { errorBox, esc, num } from "../../ui.js";

const LABEL_TAG = { scheduled: "", manual: "ok", "pre-restore": "warn" };
const mb = (bytes) => `${num(bytes / 1e6, 1)} MB`;

function ageText(hours) {
  if (hours == null) return "還沒有任何備份";
  if (hours < 1) return "不到 1 小時前";
  if (hours < 48) return `${num(hours, 0)} 小時前`;
  return `${num(hours / 24, 0)} 天前`;
}

export async function render(main) {
  const s = await api("/backups");
  const k = s.settings;
  main.innerHTML = `<h2>備份</h2>
    <p class="sub">每份備份包含數據庫、配置文件和導入時保存的原始文件，附每個文件的校驗碼；做完會立即完整校驗一遍。
    不包含密鑰：換機器恢復後要用 <span class="mono">crudelab secret set</span> 重新錄入。設定在 config/backup.yaml。</p>
    ${s.stale ? `<div class="msg warn">最近一次成功備份：${ageText(s.age_hours)}，超過了 ${k.max_age_hours} 小時。
      檢查「定時任務」頁的${s.jobs.map((j) => esc(j.name_zh)).join("、") || "備份任務（目前沒有定義）"}是否開著、有沒有失敗。</div>`
      : `<div class="msg ok">最近一次成功備份：${ageText(s.age_hours)}。</div>`}
    ${s.same_disk ? `<div class="msg warn">備份和數據在同一塊磁盤上：能防誤刪、防改壞，防不了硬盤損壞或機器丟失。
      正式使用前，把 config/backup.yaml 的 directory（或環境變量 CRUDELAB_BACKUP_DIR）指到另一塊硬盤、NAS 或雲同步文件夾。</div>` : ""}
    <div class="panel"><div class="row"><h2 style="margin:0">位置與保留</h2><span style="flex:1"></span>
        <button class="btn primary" id="now">立即備份</button></div>
      <div id="msg"></div>
      <div class="stat">
        <div>備份目錄<b class="mono" style="font-size:13px;word-break:break-all">${esc(s.directory)}</b></div>
        <div>數據庫<b>${esc(s.database === "postgresql" ? "PostgreSQL（pg_dump）" : s.database === "sqlite" ? "SQLite（在線快照）" : "不支持")}</b></div>
        <div>原始文件<b>${k.include_raw ? "包含" : "不包含"}</b></div>
      </div>
      <p class="muted" style="margin:0">定時備份保留：最近 ${k.keep_daily} 天每天一份，再往前 ${k.keep_weekly} 周每周一份、${k.keep_monthly} 個月每月一份。
        手動備份和恢復前的自動備份不會被自動清理，不需要時自己刪除。</p></div>
    <div class="panel"><h2>備份記錄</h2><div class="scroll"><table>
      <tr><th>時間（${esc(s.timezone)}）</th><th>類型</th><th class="n">大小</th><th class="n">數據點</th><th class="n">原始文件</th><th>程序版本</th><th>文件名</th><th></th></tr>
      ${s.backups.map((b) => `<tr><td class="nw">${esc(b.at)}</td>
        <td><span class="tag ${LABEL_TAG[b.label]}">${esc(b.label_zh)}</span></td>
        <td class="n">${mb(b.size)}</td>
        ${b.error ? `<td colspan="3"><span class="tag bad">無法讀取</span> ${esc(b.error)}</td>`
          : `<td class="n">${num(b.observations, 0)}</td><td class="n">${num(b.raw_files, 0)}</td><td class="mono">${esc(b.version ?? "")}</td>`}
        <td class="mono muted">${esc(b.name)}</td>
        <td><button class="btn" data-verify="${esc(b.name)}">校驗</button><div class="vr" data-for="${esc(b.name)}"></div></td></tr>`).join("")
        || '<tr><td colspan="8" class="muted">還沒有備份。點「立即備份」做第一份。</td></tr>'}</table></div></div>
    <div class="panel"><h2>怎麼恢復</h2>
      <p class="sub">恢復會覆蓋現在的數據，所以只能在服務器上用命令行做，不能從網頁操作。步驟：</p>
      <ol>
        <li>關掉服務（關掉運行 <span class="mono">crudelab serve</span> 的窗口，或停掉系統服務）。</li>
        <li>運行（文件名換成上表裡要恢復的那一份）：<pre class="mono">crudelab restore crudelab-YYYYMMDD-HHMMSS-scheduled.tar.gz --yes</pre>
          它會先校驗備份，再自動把現在的數據備份一份（類型「恢復前自動」），恢復錯了還能回去。配置文件預設不動，要一起恢復加 <span class="mono">--with-config</span>。</li>
        <li>重新啟動服務。在新機器上恢復的話，再用 <span class="mono">crudelab secret list</span> 檢查密鑰。</li>
      </ol></div>`;

  main.querySelector("#now").addEventListener("click", async (ev) => {
    const msg = main.querySelector("#msg");
    ev.target.disabled = true;
    msg.innerHTML = '<div class="msg">備份中…數據多時可能要一兩分鐘。</div>';
    try { await api("/backups", { method: "POST" }); await render(main); }
    catch (e) { msg.innerHTML = errorBox(e); ev.target.disabled = false; }
  });
  main.querySelectorAll("[data-verify]").forEach((b) => b.addEventListener("click", async () => {
    const out = main.querySelector(`.vr[data-for="${CSS.escape(b.dataset.verify)}"]`);
    b.disabled = true;
    out.innerHTML = '<span class="muted">校驗中…</span>';
    try {
      const r = await api(`/backups/${encodeURIComponent(b.dataset.verify)}/verify`, { method: "POST" });
      out.innerHTML = r.ok ? `<span class="tag ok">通過</span> <span class="muted">${r.files} 個文件</span>`
        : `<span class="tag bad">不通過</span> ${esc(r.message)}`;
    } catch (e) { out.innerHTML = errorBox(e); }
    b.disabled = false;
  }));
}
