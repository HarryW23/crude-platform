# crudelab — 原油研究平台

數據 → 指標 → 模型 → 推演 → 報告。四期都已完成：骨架、地域價差與煉廠盈利、供需平衡、地緣與推演與報告。
版面以電腦端為主。

架構、約定和擴展方法見 [docs/architecture.md](docs/architecture.md)。

## 現在能做什麼

**前台**

- **地域價差**：基準價差、月差結構、各油種現貨絕對價、品質價差、運費、中國到岸成本、套利窗口。
- **供需平衡**：
  - 兩張平衡表：全球液體燃料（EIA 口徑，月度，含預測期）、美國原油（周度，含未解釋項）。
  - **情境**：你的判斷不改數據，而是建一個情境，在某一項、某段時間加減一個量並寫明依據；平衡表可在基準和情境之間對照。
  - 庫存對比前五年同期的區間；庫存與月差的線性關係（給出樣本數、R²、當前偏離）。
  - 庫存偏離、表觀需求、同比、可用天數、機構口徑對照等指標。
- **煉廠盈利**：單品裂解（美國、西北歐、新加坡）、中國原油到廠成本和化工價差，以及 5 個整廠毛利模型：
  新加坡複雜型 / 簡單型、西北歐複雜型、山東燃料型、中國煉化一體化。
  中國的兩個模型按含稅口徑計算（增值稅、消費稅、附加）。
- **毛利拆解與試算**：每個模型可以看逐項加減、產品明細，並臨時改收率、成本、價格重算，不會保存。
- **地緣事件**：事件庫（類別、地區、狀態、對油價的方向判斷）；套用預案把事件變成情境，它的影響就能在平衡表和推演裡使用；可讓大模型從新聞裡抽取事件草稿，由你確認。
- **推演**：
  - 方向信號：四個模組各自一個 −1 到 +1 的分數，逐個信號列出依據。
  - 價格推演：設幾個情境分支和機率，得到各分支在 1 周 / 1 個月 / 3 個月的價格中樞與區間，以及機率加權價。公式全部攤開。
- **報告**：一鍵生成日報 / 周報 / 月報，是那一刻的快照；可下載 Markdown、列印成 PDF；可讓大模型加一段解讀（解讀裡的數字會和正文逐個核對）。
- **問答**：用自然語言提問（「美國原油庫存比五年均值高還是低」「如果 OPEC 少供應 1 百萬桶會怎樣」）。大模型只能調用系統的查詢工具取數，回答裡的數字逐個核對，取數過程可以展開查看。問答只能查詢和試算，不能改數據。
- 每個指標顯示日 / 5 日 / 20 日變化和一年分位；任何結果都能逐層追溯到原始文件的格子和當時生效的參數。

**後台**

- **數據導入**：Excel / CSV，不要求固定模板，自動識別版式和表頭。
- **序列**：字典、走勢、手動錄入、修訂歷史。
- **參數**：所有收率、稅率、換算係數、成本，帶生效日期和完整版本記錄；標記是否已核對。
- **指標、數據源、日誌**。

**底層保證**

- 公式做量綱檢查，單位對不上的配置載入不了。
- 參數按生效日期取值；數據修訂完整留檔；可還原任意時點當時看到的數據。
- 缺數據或缺參數的指標如實顯示缺什麼，不估算。樣本不足的統計關係不擬合。
- 未來期間的值算預測，不會被當成「最新值」。

## 啟動

需要先安裝 [Python 3.11 或更新版本](https://www.python.org/downloads/)（Windows 安裝時勾選「Add python.exe to PATH」）。

**雙擊啟動**

| 系統 | 雙擊這個文件 |
|---|---|
| Windows | `start-windows.bat` |
| Mac | `start-mac.command` |

第一次會自動安裝依賴（需要聯網，約一兩分鐘），之後直接啟動並打開瀏覽器。彈出的窗口要保持開著，關掉就停了。

Mac 第一次雙擊如果被系統攔住，在文件上按右鍵選「打開」，再確認一次即可。

**手動啟動**（雙擊不成功時，或在 Linux 上）

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/crudelab serve --open
```

Windows 把 `.venv/bin/` 換成 `.venv\Scripts\`。地址是 <http://127.0.0.1:8000>。

### 用假數據試一遍

```bash
.venv/bin/crudelab make-sample sample.xlsx
```

在「數據導入」上傳 `sample.xlsx`，數據源選「演示用假數據」。數字是隨機生成的，不是行情。試完後清除：

```bash
.venv/bin/crudelab purge-source demo --yes
```

### 命令

| 命令 | 作用 |
|---|---|
| `crudelab check` | 校驗 `config/`，一次列出全部問題 |
| `crudelab init` | 升級表結構並同步配置（`serve` 會自動做） |
| `crudelab import 文件.xlsx --dry-run` | 只預覽識別結果，不入庫 |
| `crudelab import 文件.xlsx --source reuters_xls` | 導入（只入庫能自動識別的列） |
| `crudelab pull eia` | 取 EIA 周度數據，需要環境變量 `EIA_API_KEY` |
| `pytest` / `ruff check src tests` | 測試 / 代碼檢查 |

## 大模型（可選）

不配置也能用：事件手動錄入，報告只有系統排版的正文。配置後多三項功能：從文字抽取事件草稿、為報告寫解讀、問答。

| 環境變量 | 說明 |
|---|---|
| `CRUDELAB_LLM_PROVIDER` | `none`（預設）/ `deepseek` / `openai` / `anthropic` / `compatible` |
| `CRUDELAB_LLM_MODEL` | 模型名。`deepseek` 預設 `deepseek-chat`，`anthropic` 預設 `claude-opus-5-5`；`openai` 和 `compatible` 必填 |
| `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | 對應供應商的密鑰 |
| `CRUDELAB_LLM_BASE_URL`、`CRUDELAB_LLM_API_KEY` | 僅 `compatible`：任何兼容 chat completions 格式的服務 |

例如用 DeepSeek：

```bash
export CRUDELAB_LLM_PROVIDER=deepseek
```

```bash
export DEEPSEEK_API_KEY=你的密鑰
```

用 Claude 需要另外安裝 `pip install -e ".[anthropic]"`。想省錢時把 `CRUDELAB_LLM_MODEL` 換成同一家更便宜的型號即可，不用改代碼。
三家的接入都還沒有用真實密鑰驗證過（DeepSeek / GPT 這條只用本機的模擬服務驗過請求格式），第一次用時請先拿一段短新聞試抽取。
問答一個問題要調用大模型幾次（每查一次數據一次），費用和等待時間比抽取、寫解讀高。

## 使用前需要你做的

1. **核對參數。** 所有收率、加工費、稅率、換算係數都標為「待核對」。其中收率和加工費是示意性的暫定值，不能直接當結論用。在「參數」頁逐項核對或修改。
2. **錄入運費基準費率。** TD3C / TD15 / TD20 / TD25 的 Worldscale 年度基準費率和 ESPO 運費沒有預設值，需要按年錄入；否則運費、到岸成本、套利窗口共 14 個指標算不出來。
3. **核對 EIA 序列號。** 供需的 24 條序列登記了 EIA 序列號，是按記憶填的，沒有實際調用過。配好 `EIA_API_KEY` 後先取一次數，看每條是否取到、數量級是否合理。
4. **給出推演的敏感度。** 參數 `PROJ_PRICE_PER_BALANCE`（全球平衡每多過剩 1 百萬桶/日，Brent 跌多少）是推演的核心假設，沒有預設值；不設的話，帶供需調整的分支算不出價格。
5. **核對信號的方向。** `config/signals/signals.yaml` 裡每個信號的 `direction`（數值高是利多還是利空）是我按常規理解填的，請按你的看法核對。
6. **導入數據。** 各指標需要哪些序列，在指標的狀態欄和「序列」頁可以看到。

## 目錄

```
config/                 所有可調的東西
  master/               單位、地區、品種、數據源、序列
  parameters/           參數定義和初始值
  indicators/           指標公式
  refineries/           煉廠模型
  balances/             供需平衡表
  relations/            關係模型
  signals/              方向信號
  geo/playbooks.yaml    地緣事件預案
  projection.yaml       價格推演設定
src/crudelab/
  schema.py config.py   配置的結構與校驗
  units.py formula.py   單位代數、公式語言
  refinery.py balance.py 煉廠模型、平衡表的編譯器
  periods.py            期間型數據的日期約定
  models.py migrations/ 表結構與遷移
  ingest.py             數據點的寫入與讀取
  parameters.py         參數服務
  scenarios.py          情境與調整
  indicators.py         指標引擎（計算、追溯、情境）
  analytics.py          季節性、同比、線性擬合
  events.py             地緣事件與預案
  signals.py            方向信號
  projection.py         價格推演
  reports.py            報告快照與排版
  connectors/ imports.py 數據接入
  api/                  接口
  web/                  頁面
  llm/                  大模型接口：供應商接入、抽取與撰寫任務、數字核對
tests/
docs/architecture.md
```

## 數據庫

預設 SQLite，文件在 `data/crudelab.db`（不進 git，請自行備份 `data/`）。換 PostgreSQL：

```bash
docker compose up -d db
```

```bash
export DATABASE_URL=postgresql+psycopg://crudelab:crudelab@localhost:5432/crudelab
```

並安裝 `pip install -e ".[postgres]"`。PostgreSQL 路徑尚未實測。

## 已知限制

- 計算上的簡化見 [docs/architecture.md](docs/architecture.md) 第 8 節。
- 大模型的三家接入沒有用真實密鑰驗證過。
- 不支援舊版 `.xls`；合併儲存格的表頭只算在第一列上。
- EIA 接口沒有實際調用過，序列號未驗證。
- 演示數據的日度行情預設給三年（`make-sample`），供需給六年多；都是隨機數。
- 沒有登錄和權限，只應在本機使用。

## 路線

1. ✅ 骨架
2. ✅ 地域價差 + 煉廠盈利
3. ✅ 供需平衡
4. ✅ 地緣政治 + 推演 + 報告

之後可以做的：定時自動取數和出報告、統計預測模型作為推演的輸入、登錄與權限、Word 格式報告。
