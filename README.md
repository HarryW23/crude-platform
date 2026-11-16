# crudelab — 原油研究平台

數據 → 指標 → 模型 → 推演 → 報告。目前完成第一期（骨架）和第二期（地域價差、煉廠盈利）。

架構、約定和擴展方法見 [docs/architecture.md](docs/architecture.md)。

## 現在能做什麼

**前台**

- **地域價差**：基準價差、月差結構、各油種現貨絕對價、品質價差、運費、中國到岸成本、套利窗口。
- **煉廠盈利**：單品裂解（美國、西北歐、新加坡）、中國原油到廠成本和化工價差，以及 5 個整廠毛利模型：
  新加坡複雜型 / 簡單型、西北歐複雜型、山東燃料型、中國煉化一體化。
  中國的兩個模型按含稅口徑計算（增值稅、消費稅、附加）。
- **毛利拆解與試算**：每個模型可以看逐項加減、產品明細，並臨時改收率、成本、價格重算，不會保存。
- 每個指標顯示日 / 5 日 / 20 日變化和一年分位；任何結果都能逐層追溯到原始文件的格子和當時生效的參數。

**後台**

- **數據導入**：Excel / CSV，不要求固定模板，自動識別版式和表頭。
- **序列**：字典、走勢、手動錄入、修訂歷史。
- **參數**：所有收率、稅率、換算係數、成本，帶生效日期和完整版本記錄；標記是否已核對。
- **指標、數據源、日誌**。

**底層保證**

- 公式做量綱檢查，單位對不上的配置載入不了。
- 參數按生效日期取值；數據修訂完整留檔；可還原任意時點當時看到的數據。
- 缺數據或缺參數的指標如實顯示缺什麼，不估算。

## 啟動

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/crudelab serve
```

打開 <http://127.0.0.1:8000>。Windows 把 `.venv/bin/` 換成 `.venv\Scripts\`。

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

## 使用前需要你做的

1. **核對參數。** 所有收率、加工費、稅率、換算係數都標為「待核對」。其中收率和加工費是示意性的暫定值，不能直接當結論用。在「參數」頁逐項核對或修改。
2. **錄入運費基準費率。** TD3C / TD15 / TD20 / TD25 的 Worldscale 年度基準費率和 ESPO 運費沒有預設值，需要按年錄入；否則運費、到岸成本、套利窗口共 14 個指標算不出來。
3. **導入數據。** 各指標需要哪些序列，在指標的狀態欄和「序列」頁可以看到。

## 目錄

```
config/                 所有可調的東西
  master/               單位、地區、品種、數據源、序列
  parameters/           參數定義和初始值
  indicators/           指標公式
  refineries/           煉廠模型
src/crudelab/
  schema.py config.py   配置的結構與校驗
  units.py formula.py   單位代數、公式語言
  refinery.py           煉廠模型編譯器
  models.py migrations/ 表結構與遷移
  ingest.py             數據點的寫入與讀取
  parameters.py         參數服務
  indicators.py         指標引擎（計算、追溯、情境）
  connectors/ imports.py 數據接入
  api/                  接口
  web/                  頁面
  llm/                  大模型抽象接口（預留）
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

- 計算上的簡化見 [docs/architecture.md](docs/architecture.md) 第 7 節。
- 不支援舊版 `.xls`；合併儲存格的表頭只算在第一列上。
- EIA 接口沒有實際調用過。
- 沒有登錄和權限，只應在本機使用。

## 路線

1. ✅ 骨架
2. ✅ 地域價差 + 煉廠盈利
3. 供需平衡
4. 地緣政治 + 推演引擎 + 報告
