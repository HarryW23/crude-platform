# crudelab — 原油研究平台

數據 → 指標 → 模型 → 推演 → 報告。目前完成的是**第一期：骨架**。

## 現在能做什麼

- **主數據字典**：品種、地區、單位、數據源、76 條序列，全部定義在 `config/` 下的 YAML。
- **Excel / CSV 導入**：不要求固定模板。自動識別直排、橫排、長表三種版式和多行表頭；認不出的列在後台指定一次，之後自動識別。
- **入庫**：原文件原樣保存；同一序列可並存多個來源，按優先級取用；數據被修訂時留記錄。
- **指標引擎**：17 個基礎指標（基準價差、月差、裂解），全部由「公式 + 參數」定義。任何一個結果都能拆回到原始文件的具體格子。
- **後台**：總覽、數據導入、序列（含手動錄入）、指標（含計算追溯）、參數（含修改歷史）、數據源、日誌。
- **預留**：Kpler、路透 API、國內資訊商的 connector 空殼；大模型抽象接口。

缺數據的指標顯示「缺數據」，不會用估算值代替。

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

在後台「數據導入」上傳 `sample.xlsx`，數據源選「演示用假數據」。試完後清除：

```bash
.venv/bin/crudelab purge-source demo --yes
```

### 其他命令

| 命令 | 作用 |
|---|---|
| `crudelab import 文件.xlsx --dry-run` | 只預覽識別結果，不入庫 |
| `crudelab import 文件.xlsx --source reuters_xls` | 導入（只入庫能自動識別的列） |
| `crudelab pull eia` | 取 EIA 周度數據，需要環境變量 `EIA_API_KEY` |
| `pytest` | 跑測試 |

## 目錄

```
config/                 所有可調的東西都在這裡
  master/*.yaml         品種 / 地區 / 單位 / 數據源 / 序列
  parameters.yaml       參數初始值（之後在後台改）
  indicators.yaml       指標公式
src/crudelab/
  master.py             讀取、校驗配置並同步到數據庫
  models.py             數據表
  ingest.py             統一的入庫與讀取
  connectors/           數據接入（excel、eia、預留）
  imports.py            文件導入流程（預覽 → 確認 → 入庫）
  indicators.py         指標引擎與追溯
  api/app.py            後台接口
  web/index.html        後台頁面
  llm/                  大模型抽象接口（預留）
tests/
```

## 常見修改

**加一條序列**：在 `config/master/series.yaml` 加一行，`aliases` 填 Excel 裡可能出現的列名。

**加一個指標**：在 `config/indicators.yaml` 加一行。公式可用 `+ - * / **`、括號，以及 `lag(x, n)`、`ma(x, n)`、`diff(x, n)`、`abs(x)`；名字可以是序列代碼、參數 key 或其他指標代碼。

**加一個參數**：在 `config/parameters.yaml` 加一行。已存在的參數值以後台為準，重新同步不會覆蓋。

改完配置後重啟服務，或調用 `POST /api/admin/reload-config`。配置有錯時會指出是哪一項，並保持原配置不變。

**接通一個預留數據源**：步驟寫在 `src/crudelab/connectors/reserved.py` 開頭。

## 數據庫

預設 SQLite，文件在 `data/crudelab.db`。換 PostgreSQL：

```bash
docker compose up -d db
```

```bash
export DATABASE_URL=postgresql+psycopg://crudelab:crudelab@localhost:5432/crudelab
```

並安裝 `pip install -e ".[postgres]"`。PostgreSQL 路徑尚未實測。

## 已知限制

- 不支援舊版 `.xls`，需另存為 `.xlsx`。
- 合併儲存格的表頭只會算在第一列上。
- 期貨序列按 M1 / M2 / M3 連續合約存，還沒有具體到期月份和換月處理。
- 升貼水的計價期用 M1 近似。
- 沒有登錄和權限，操作者固定記為 `local`；只應在本機使用。
- 參數初始值（桶噸換算係數）是行業常用值，請核對是否符合你的口徑。

## 路線

1. ✅ 骨架
2. 地域價差 + 煉廠盈利（燃料型 / 一體化兩套收率與稅費配置）
3. 供需平衡
4. 地緣政治 + 推演引擎 + 報告
