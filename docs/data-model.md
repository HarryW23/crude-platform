# 數據字典

由 `crudelab data-model --output docs/data-model.md` 從 `src/crudelab/models.py` 生成，不要手改。
表結構的變更一律通過遷移腳本（`src/crudelab/migrations/versions/`）。

## 總覽

```
數據表定義。表結構的變更一律通過 migrations/ 下的遷移腳本，不要直接改庫。

表按用途分組（tests/test_data_model.py 檢查每張表都歸了組）：
  主數據       unit / location / commodity / data_source / series / series_alias / parameter —— 配置文件的鏡像
  事實數據     import_batch / observation / observation_vintage / parameter_version
  外部數據     calendar_event / news_flash / external_call —— 金十財經日曆、快訊、調用記錄
  判斷         scenario / adjustment / event / projection / projection_case / market_view —— 人的判斷，和數據分開存
  產出         report / backtest_run —— 生成時刻的快照
  帳號         user_account / user_session / login_failure
  調度與運行   job_run / scheduler_heartbeat / switch / notification / app_setting
  大模型       llm_call / llm_pause
  修訂歷史     observation_revision
  操作日誌     audit_log

時間一律存 UTC（不帶時區的 datetime）。

兩條時間軸要分清：
  業務時間   observation.obs_date、parameter_version.valid_from —— 這個值「說的是哪一天」
  記錄時間   created_at / updated_at / replaced_at —— 系統「什麼時候知道的」
有了記錄時間，就能還原任意時點當時看到的數據（as_of），回測時不會偷看到後來的修訂。
```

共 34 張表。取值約束由 `src/crudelab/domain.py` 統一定義。

## adjustment

一條調整：在某段日期裡，給某個序列或指標加上一個數。只增不改；撤銷是填 removed_at。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `scenario_code` | VARCHAR(64) | 否 | FK IX | → scenario.code |
| `target` | VARCHAR(64) | 否 |  | 序列或指標代碼 |
| `date_from` | DATE | 否 |  | 從哪天起生效 |
| `date_to` | DATE | 是 |  | 空 = 一直持續 |
| `delta` | FLOAT | 否 |  | 加上的量（單位同目標） |
| `note` | TEXT | 是 |  | 依據：為什麼這樣調 |
| `created_by` | VARCHAR(64) | 否 |  | 建立者（登錄用戶名；未開登錄時為 local） |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |
| `removed_at` | DATETIME | 是 |  | 撤銷時間（UTC；空 = 有效） |

## app_setting

後台頁面上改的設定（不是開關的那種），例如大模型用哪個型號、開不開思考模式。值是 JSON。不存密鑰。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `key` | VARCHAR(64) | 否 | PK | 鍵 |
| `value` | JSON | 否 |  | 數值 |
| `updated_at` | DATETIME | 否 |  | 最後修改時間（UTC） |
| `updated_by` | VARCHAR(64) | 否 |  | 最後修改者 |

## audit_log

操作日誌：誰在什麼時候改了什麼。只追加，不修改。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `ts` | DATETIME | 否 | IX | 時間（UTC） |
| `actor` | VARCHAR(64) | 否 |  | 操作者 |
| `action` | VARCHAR(32) | 否 |  | 做了什麼（如 user_create、restore） |
| `entity` | VARCHAR(32) | 否 |  | 對象類型 |
| `entity_id` | VARCHAR(128) | 否 |  | 對象編號或代碼 |
| `detail` | JSON | 是 |  | 詳細信息（JSON） |

## backtest_run

一次回測。結果是生成那一刻的快照，存成 JSON。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `created_by` | VARCHAR(64) | 否 |  | 建立者（登錄用戶名；未開登錄時為 local） |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |
| `finished_at` | DATETIME | 是 |  | 結束時間（UTC；空 = 未結束） |
| `status` | VARCHAR(16) | 否 |  | 狀態；取值：running / done / failed |
| `mode` | VARCHAR(16) | 否 |  | 取值：observation / vintage |
| `start_date` | DATE | 是 |  | 開始日期 |
| `end_date` | DATE | 是 |  | 結束日期 |
| `step` | INTEGER | 否 |  | 每隔幾個交易日評估一次 |
| `message` | TEXT | 是 |  | 結果說明（已脫敏） |
| `results` | JSON | 是 |  | 各渠道的發送結果 {渠道: ok 或原因} |

## calendar_event

財經日曆的一項（例如 EIA 原油庫存）。預期值會在公佈前變動：實際值第一次出現時，把當時的預期凍結下來， 「超預期」一律用凍結的預期算，避免事後修改的預期讓歷史看起來更準。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `source` | VARCHAR(32) | 否 |  | 來源 |
| `key` | VARCHAR(64) | 否 |  | 公佈時間 + 標題的雜湊 |
| `pub_time` | DATETIME | 否 | IX | UTC |
| `title` | VARCHAR(300) | 否 |  | 標題 |
| `star` | INTEGER | 是 |  | 重要性 1–5 |
| `previous` | VARCHAR(64) | 是 |  | 前值 |
| `consensus` | VARCHAR(64) | 是 |  | 市場預期（最新） |
| `actual` | VARCHAR(64) | 是 |  | 公佈值 |
| `revised` | VARCHAR(64) | 是 |  | 修正後的前值 |
| `affect` | VARCHAR(64) | 是 |  | 對方給的「利多 / 利空」說明 |
| `consensus_at_release` | VARCHAR(64) | 是 |  | 公佈前凍結的預期（算超預期用） |
| `released_at` | DATETIME | 是 |  | 我們第一次看到實際值的時間 |
| `first_seen_at` | DATETIME | 否 |  | 第一次收到的時間（UTC） |
| `updated_at` | DATETIME | 否 |  | 最後修改時間（UTC） |

- 唯一：source, key

## commodity

品種字典（config/master/commodities.yaml）。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `code` | VARCHAR(32) | 否 | PK | 代碼（主鍵，來自配置） |
| `name_zh` | VARCHAR(64) | 否 |  | 中文名稱 |
| `category` | VARCHAR(32) | 否 |  | 類別；取值：crude / product / chem / freight / fx / fundamental / metals / rates |

## data_source

數據源（config/master/sources.yaml）：同一序列有多個來源時按優先級取值。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `code` | VARCHAR(32) | 否 | PK | 代碼（主鍵，來自配置） |
| `name` | VARCHAR(128) | 否 |  | 名稱 |
| `kind` | VARCHAR(16) | 否 |  | 類型；取值：manual / excel / api |
| `status` | VARCHAR(16) | 否 |  | 狀態；取值：active / reserved |
| `priority` | INTEGER | 否 |  | 越小越優先 |
| `notes` | TEXT | 是 |  | 備註 |

## event

地緣事件。draft=True 的是大模型從文字裡抽取、還沒有人確認的草稿，不參與任何計算。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `title` | VARCHAR(200) | 否 |  | 標題 |
| `category` | VARCHAR(16) | 否 |  | 類別；取值：conflict / chokepoint / sanction / opec_policy / outage / policy / demand / other |
| `location_code` | VARCHAR(32) | 是 |  | 地區（地區字典的代碼） |
| `status` | VARCHAR(16) | 否 |  | 狀態；取值：rumor / confirmed / ongoing / resolved / dismissed |
| `bias` | INTEGER | 否 |  | 對油價的方向判斷：−2 強利空 … +2 強利多 |
| `started_on` | DATE | 是 |  | 開始日期 |
| `ended_on` | DATE | 是 |  | 結束日期（空 = 仍在進行） |
| `summary` | TEXT | 是 |  | 摘要 |
| `source` | TEXT | 是 |  | 出處：連結或原文摘錄 |
| `scenario_code` | VARCHAR(64) | 是 | FK | 這個事件的量化影響；→ scenario.code |
| `probability` | FLOAT | 是 |  | 推演期限內發生 / 持續的機率 0–1（人的判斷） |
| `price_premium` | FLOAT | 是 |  | 發生時直接加在價格上的溢價（錨定價格的單位） |
| `origin` | VARCHAR(16) | 否 |  | 取值：manual / llm |
| `draft` | BOOLEAN | 否 |  | 大模型抽取、尚未確認的草稿（不參與計算） |
| `created_by` | VARCHAR(64) | 否 |  | 建立者（登錄用戶名；未開登錄時為 local） |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |
| `updated_at` | DATETIME | 否 |  | 最後修改時間（UTC） |

- 約束：`bias BETWEEN -2 AND 2`
- 約束：`probability IS NULL OR (probability >= 0 AND probability <= 1)`

## external_call

一次對外部數據服務的調用（目前是金十）。用來按「每個工具每天」計數、守住對方的額度，也讓人看得到用量。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `source` | VARCHAR(32) | 否 |  | 來源 |
| `tool` | VARCHAR(64) | 否 |  | 調用的工具名 |
| `day` | DATE | 否 | IX | 對方計額度的日曆日（金十按北京時間） |
| `started_at` | DATETIME | 否 |  | 開始時間（UTC） |
| `status` | VARCHAR(16) | 否 |  | 狀態；取值：ok / failed |
| `message` | TEXT | 是 |  | 結果說明（已脫敏） |

## import_batch

一次文件導入。原文件按 sha256 保存在 data/raw/，批次記下當時用的列對應。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `source_code` | VARCHAR(32) | 否 | FK | → data_source.code |
| `filename` | VARCHAR(256) | 否 |  | 上傳時的文件名 |
| `sha256` | VARCHAR(64) | 否 | IX | 文件內容的 SHA-256 |
| `stored_path` | VARCHAR(512) | 否 |  | 原文件保存位置 |
| `status` | VARCHAR(16) | 否 |  | 狀態；取值：previewed / committed |
| `mapping` | JSON | 是 |  | 當時用的列對應 |
| `stats` | JSON | 是 |  | 導入結果統計 |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |
| `created_by` | VARCHAR(64) | 否 |  | 建立者（登錄用戶名；未開登錄時為 local） |
| `vintage` | DATE | 是 |  | 機構報告的發佈日期（補錄歷史報告時填） |

## job_run

定時任務的一次執行。(job_code, slot, attempt) 唯一： 同一個時點的同一次嘗試，不管有幾個進程在跑調度，只會被執行一次。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `job_code` | VARCHAR(64) | 否 | IX | 定時任務代碼（schedule.yaml） |
| `slot` | DATETIME | 否 |  | 這次執行對應的計劃時點（手動觸發時是觸發時刻） |
| `attempt` | INTEGER | 否 |  | 同一個時點的第幾次（重試 + 1） |
| `trigger` | VARCHAR(16) | 否 |  | 取值：schedule / manual |
| `status` | VARCHAR(16) | 否 |  | 狀態；取值：running / ok / skipped / failed / retry / retried |
| `started_at` | DATETIME | 否 |  | 開始時間（UTC） |
| `finished_at` | DATETIME | 是 |  | 結束時間（UTC；空 = 未結束） |
| `message` | TEXT | 是 |  | 給人看的一句話結果（已脫敏） |
| `detail` | JSON | 是 |  | 詳細信息（JSON） |

- 唯一：job_code, slot, attempt

## llm_call

一次大模型調用。用來做跨進程的頻率和用量限制，也讓人看得到花了多少。不存發出去的內容和回覆。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `started_at` | DATETIME | 否 | IX | 開始時間（UTC） |
| `finished_at` | DATETIME | 是 |  | 結束時間（UTC；空 = 未結束） |
| `provider` | VARCHAR(32) | 否 |  | 大模型供應商 |
| `model` | VARCHAR(128) | 否 |  | 模型名 |
| `purpose` | VARCHAR(32) | 否 |  | 取值：chat / extract / narrative / test / models / other |
| `status` | VARCHAR(16) | 否 |  | 狀態；取值：running / ok / failed |
| `http_status` | INTEGER | 是 |  | 對方返回的 HTTP 狀態碼 |
| `prompt_tokens` | INTEGER | 是 |  | 輸入 token 數 |
| `completion_tokens` | INTEGER | 是 |  | 輸出 token 數 |
| `message` | TEXT | 是 |  | 失敗原因（我們自己的說明，已脫敏） |

## llm_pause

某家大模型被自動暫停（密鑰無效、餘額不足、觸發頻率限制、連續出錯）。到期自動恢復，也可以手動解除。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `provider` | VARCHAR(32) | 否 | PK | 大模型供應商 |
| `until` | DATETIME | 否 |  | 暫停到（UTC） |
| `reason` | TEXT | 否 |  | 暫停原因 |
| `strikes` | INTEGER | 否 |  | 連續觸發頻率限制的次數，用來加長暫停 |

## location

地區字典（config/master/locations.yaml）。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `code` | VARCHAR(32) | 否 | PK | 代碼（主鍵，來自配置） |
| `name_zh` | VARCHAR(64) | 否 |  | 中文名稱 |
| `region` | VARCHAR(32) | 否 |  | 地區 |

## login_failure

一次登錄失敗（#36）。存數據庫而不是內存：多進程、重啟後鎖定仍然有效。 只記小寫用戶名和時間（不記密碼、不記 IP）；過了計數窗口的記錄在下一次失敗時順手清掉。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `username_key` | VARCHAR(128) | 否 | IX | 小寫的用戶名 |
| `at` | DATETIME | 否 | IX | 時間（UTC） |

## market_view

一條記下的觀點（判斷）。記下後內容不能改，只能撤銷：這是覆盤可信的前提。 結果不存，到期後按當時的數據現算（數據修訂了，結果跟著修正）。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `made_on` | DATE | 否 |  | 判斷的日期（以這天的收盤價為起點） |
| `horizon` | VARCHAR(8) | 否 |  | 期限代碼：W1 / M1 / M3 |
| `target_date` | DATE | 否 | IX | 到期日 = made_on + 期限的日曆天數 |
| `series_code` | VARCHAR(64) | 否 |  | 看的是哪個價格（推演的錨定價格） |
| `direction` | INTEGER | 否 |  | −1 偏空 / 0 中性 / +1 偏多 |
| `low` | FLOAT | 是 |  | 目標區間（可不填） |
| `high` | FLOAT | 是 |  | 區間上沿 |
| `confidence` | FLOAT | 是 |  | 方向判斷對的把握 0.5–1（可不填） |
| `reasons` | JSON | 否 |  | 理由的因子標籤 |
| `rationale` | TEXT | 是 |  | 理由 |
| `start_price` | FLOAT | 否 |  | 起點價格（made_on 當天或之前最近的值） |
| `start_date` | DATE | 否 |  | 開始日期 |
| `daily_vol` | FLOAT | 是 |  | 當時的日波動率：決定「中性」的範圍 |
| `system` | JSON | 是 |  | 當時系統信號的快照（綜合與各模組分數） |
| `created_by` | VARCHAR(64) | 否 |  | 建立者（登錄用戶名；未開登錄時為 local） |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |
| `cancelled_at` | DATETIME | 是 |  | 撤回時間（UTC） |
| `cancel_reason` | TEXT | 是 |  | 撤回原因 |

## news_flash

一條收錄的快訊（命中原油相關關鍵詞的才收錄）。可以交給大模型抽取成事件草稿，草稿要人確認。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `source` | VARCHAR(32) | 否 |  | 來源 |
| `ext_id` | VARCHAR(64) | 否 |  | 對方的編號（沒有時用時間 + 內容的雜湊） |
| `time` | DATETIME | 否 | IX | UTC |
| `content` | TEXT | 否 |  | 正文 |
| `url` | VARCHAR(500) | 是 |  | 原文網址 |
| `keywords` | JSON | 否 |  | 命中的關鍵詞 |
| `important` | BOOLEAN | 否 |  | 金十標記的重要快訊 |
| `first_seen_at` | DATETIME | 否 |  | 第一次收到的時間（UTC） |
| `extracted_at` | DATETIME | 是 |  | 交給大模型抽取的時間 |
| `drafts` | INTEGER | 否 |  | 抽出了幾條事件草稿 |

- 唯一：source, ext_id

## notification

發出過的通知，以及每個渠道的發送結果（不含地址）。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `created_at` | DATETIME | 否 | IX | 記錄建立時間（UTC） |
| `job_code` | VARCHAR(64) | 是 | IX | 定時任務代碼（schedule.yaml） |
| `event` | VARCHAR(16) | 否 |  | 取值：failure / reminder / recovery / test |
| `title` | VARCHAR(200) | 否 |  | 標題 |
| `body` | TEXT | 否 |  | 正文 |
| `results` | JSON | 否 |  | {渠道代碼: "ok" 或失敗原因} |

## observation

一個數據點的當前值：某序列、某天、某來源。同一序列同一天可以並存多個來源。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `series_code` | VARCHAR(64) | 否 | FK | → series.code |
| `obs_date` | DATE | 否 |  | 數據日期（業務時間：這個值說的是哪一天） |
| `value` | FLOAT | 否 |  | 數值 |
| `source_code` | VARCHAR(32) | 否 | FK | → data_source.code |
| `batch_id` | INTEGER | 是 | FK | → import_batch.id |
| `source_ref` | VARCHAR(256) | 是 |  | 例如 "Sheet1!C15" |
| `created_at` | DATETIME | 否 |  | 第一次入庫 |
| `updated_at` | DATETIME | 否 |  | 當前值入庫 |

- 唯一：series_code, obs_date, source_code
- 索引：series_code, obs_date

## observation_revision

數據點被改寫前的舊值。配合 observation 可以還原任意時點的數據。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `series_code` | VARCHAR(64) | 否 | FK | → series.code |
| `obs_date` | DATE | 否 |  | 數據日期（業務時間：這個值說的是哪一天） |
| `source_code` | VARCHAR(32) | 否 | FK | → data_source.code |
| `value` | FLOAT | 否 |  | 被取代的舊值 |
| `batch_id` | INTEGER | 是 | FK | → import_batch.id |
| `source_ref` | VARCHAR(256) | 是 |  | 來源裡的位置（工作表、列） |
| `recorded_at` | DATETIME | 否 |  | 舊值當初入庫的時間 |
| `replaced_at` | DATETIME | 否 |  | 被取代的時間 |
| `replaced_by` | VARCHAR(64) | 否 |  | 被哪個批次替換 |

- 索引：series_code, obs_date, source_code

## observation_vintage

按期保存的序列（機構預測：STEO、IEA、OPEC）每一期報告裡的數字。vintage = 那一期的發佈日期。 主表 observation 保存最新一期（頁面照常用）；這裡保存每一期，用來還原「某天之前發佈的最新一期是什麼」， 回測就不會用到後來的修訂。 帶報告期的導入（補錄報告）每一期都記一行，數字沒變也記：後來補錄更早的一期時，靠它知道之後那期確認過什麼。 每天自動取數（不帶報告期）數字沒變時不加行，只把 confirmed_at 延到當天：一行代表「從 vintage 到 confirmed_at 每天取到的都是這個數」。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `series_code` | VARCHAR(64) | 否 | FK | → series.code |
| `obs_date` | DATE | 否 |  | 數據日期（業務時間：這個值說的是哪一天） |
| `source_code` | VARCHAR(32) | 否 | FK | → data_source.code |
| `vintage` | DATE | 否 |  | 報告期（機構報告的發佈日期） |
| `value` | FLOAT | 否 |  | 數值 |
| `batch_id` | INTEGER | 是 | FK | → import_batch.id |
| `source_ref` | VARCHAR(256) | 是 |  | 來源裡的位置（工作表、列） |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |
| `confirmed_at` | DATE | 是 |  | 每天自動取數確認到哪天（空 = 只有 vintage 那一期） |

- 唯一：series_code, obs_date, source_code, vintage
- 索引：series_code, vintage

## parameter

參數的說明信息。數值在 parameter_version 裡，帶生效日期。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `key` | VARCHAR(64) | 否 | PK | 鍵 |
| `name_zh` | VARCHAR(128) | 否 |  | 中文名稱 |
| `unit` | VARCHAR(32) | 否 |  | 單位（參與量綱檢查） |
| `grp` | VARCHAR(64) | 否 |  | 分組（後台顯示用；列名 grp 避開 SQL 保留字） |
| `description` | TEXT | 是 |  | 說明 |
| `model` | VARCHAR(64) | 是 |  | 由哪個煉廠模型生成 |
| `verified` | BOOLEAN | 否 |  | 數值是否已由人核對 |
| `active` | BOOLEAN | 否 |  | 是否仍在配置裡（移除的置為 false，數據保留） |

## parameter_version

參數值，只增不改。 某一天生效的值 = valid_from 不晚於那一天的版本中 valid_from 最新的那個； 同一個 valid_from 有多條記錄時（更正），以 id 最大的為準。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `key` | VARCHAR(64) | 否 | FK | 鍵；→ parameter.key |
| `valid_from` | DATE | 否 |  | 生效日期（業務時間） |
| `value` | FLOAT | 否 |  | 數值 |
| `note` | TEXT | 是 |  | 備註 |
| `created_by` | VARCHAR(64) | 否 |  | 建立者（登錄用戶名；未開登錄時為 local） |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |

- 索引：key, valid_from

## projection

一次價格推演的設定：若干個情境分支，各有機率。結果每次按當前數據現算。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `name_zh` | VARCHAR(128) | 否 |  | 中文名稱 |
| `drift_model` | VARCHAR(64) | 是 |  | 用哪個公允價值模型給漂移；空 = 不加 |
| `created_by` | VARCHAR(64) | 否 |  | 建立者（登錄用戶名；未開登錄時為 local） |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |
| `archived_at` | DATETIME | 是 |  | 歸檔時間（UTC；空 = 未歸檔） |

## projection_case

推演裡的一個分支。scenario_code 為空 = 基準（只看數據，不加任何調整）。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `projection_id` | INTEGER | 否 | FK IX | → projection.id |
| `label` | VARCHAR(128) | 否 |  | 分支名稱 |
| `scenario_code` | VARCHAR(64) | 是 | FK | → scenario.code |
| `probability` | FLOAT | 否 |  | 0–1 |
| `price_adjustment` | FLOAT | 否 |  | 直接加在價格上的判斷（如風險溢價） |
| `event_id` | INTEGER | 是 | FK | 由地緣事件同步生成的分支；→ event.id |
| `event_probability` | FLOAT | 是 |  | 同步時事件的機率（用來發現事件改過） |
| `note` | TEXT | 是 |  | 備註 |
| `created_by` | VARCHAR(64) | 否 |  | 建立者（登錄用戶名；未開登錄時為 local） |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |
| `removed_at` | DATETIME | 是 |  | 撤銷時間（UTC；空 = 有效） |

## report

一份報告 = 生成那一刻的數據快照。生成後不再變化，之後數據怎麼改都不影響已出的報告。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `kind` | VARCHAR(16) | 否 |  | 類型；取值：daily / weekly / monthly |
| `title` | VARCHAR(200) | 否 |  | 標題 |
| `data_date` | DATE | 是 |  | 報告的數據截至日 |
| `facts` | JSON | 否 |  | 結構化的事實：全部來自指標引擎 |
| `markdown` | TEXT | 否 |  | 由 facts 按固定模板排出的正文 |
| `narrative` | TEXT | 是 |  | 大模型寫的解讀（可沒有） |
| `narrative_meta` | JSON | 是 |  | 哪個模型寫的、數字核對結果 |
| `created_by` | VARCHAR(64) | 否 |  | 建立者（登錄用戶名；未開登錄時為 local） |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |

## scenario

情境：一組有名字的人為調整，疊加在數據之上，不改動數據本身。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `code` | VARCHAR(64) | 否 | PK | 代碼（主鍵，來自配置） |
| `name_zh` | VARCHAR(128) | 否 |  | 中文名稱 |
| `description` | TEXT | 是 |  | 說明 |
| `created_by` | VARCHAR(64) | 否 |  | 建立者（登錄用戶名；未開登錄時為 local） |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |
| `archived_at` | DATETIME | 是 |  | 歸檔時間（UTC；空 = 未歸檔） |

## scheduler_heartbeat

調度器的心跳，只有一行。頁面用它判斷調度器是不是在運行。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `at` | DATETIME | 否 |  | 時間（UTC） |
| `host` | VARCHAR(128) | 否 |  | 寫心跳的機器名 |

## series

序列（config/master/series/*.yaml 的鏡像）：一條時間序列的定義，數據在 observation。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `code` | VARCHAR(64) | 否 | PK | 代碼（主鍵，來自配置） |
| `name_zh` | VARCHAR(128) | 否 |  | 中文名稱 |
| `commodity_code` | VARCHAR(32) | 否 | FK | → commodity.code |
| `location_code` | VARCHAR(32) | 否 | FK | → location.code |
| `kind` | VARCHAR(16) | 否 |  | 類型；取值：futures / swap / spot / diff / osp / freight / fx / stock / flow / ratio / position |
| `unit_code` | VARCHAR(32) | 否 | FK | → unit.code |
| `frequency` | VARCHAR(16) | 否 |  | 取值：daily / weekly / monthly / quarterly |
| `valid_days` | INTEGER | 否 |  | 一個值最多沿用幾天 |
| `contract` | VARCHAR(8) | 是 |  | 連續合約月序 M1 / M2 … |
| `benchmark` | VARCHAR(64) | 是 |  | 對應的基準品種 |
| `module` | VARCHAR(16) | 否 |  | 取值：spread / balance / geo / refinery / positioning / common |
| `external_ids` | JSON | 否 |  | {"eia": "PET.…"} |
| `notes` | TEXT | 是 |  | 備註 |
| `active` | BOOLEAN | 否 |  | 已從配置移除的序列置為 False，數據保留 |
| `within_period` | BOOLEAN | 否 |  | 見 SeriesDef |
| `vintaged` | BOOLEAN | 否 |  | 按報告期保存 |
| `data_from` | DATE | 是 |  | 這天之前的數據不入庫（見 SeriesDef） |

## series_alias

列名 → 序列 的對照。導入時自動識別靠它；人工確認過的對應也存回這裡（origin=learned）。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `alias_norm` | VARCHAR(128) | 否 | UQ | 規範化後的別名（小寫、簡體、去空白，唯一） |
| `alias` | VARCHAR(128) | 否 |  | 原文 |
| `series_code` | VARCHAR(64) | 否 | FK | → series.code |
| `origin` | VARCHAR(16) | 否 |  | 取值：config / learned |

## switch

後台頁面上的開關。沒有記錄 = 用配置文件裡的預設值；有記錄 = 以這裡為準。 key 的寫法：scheduler（定時任務總開關）、job:任務代碼、notify（通知總開關）、channel:渠道代碼。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `key` | VARCHAR(96) | 否 | PK | 鍵 |
| `enabled` | BOOLEAN | 否 |  | 開或關 |
| `updated_by` | VARCHAR(64) | 否 |  | 最後修改者 |
| `updated_at` | DATETIME | 否 |  | 最後修改時間（UTC） |

## unit

單位字典（config/master/units.yaml），參與公式的量綱檢查。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `code` | VARCHAR(32) | 否 | PK | 代碼（主鍵，來自配置） |
| `name_zh` | VARCHAR(64) | 否 |  | 中文名稱 |
| `dimension` | VARCHAR(32) | 否 |  | 量綱 |

## user_account

登錄帳號。密碼只存加鹽哈希（auth.py）。帳號不刪除，只停用，這樣歷史記錄裡的操作者始終對得上。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `username` | VARCHAR(64) | 否 | UQ | 用戶名（唯一，不分大小寫） |
| `password_hash` | VARCHAR(256) | 否 |  | scrypt 加鹽哈希 |
| `role` | VARCHAR(16) | 否 |  | 取值：viewer / analyst / admin |
| `active` | BOOLEAN | 否 |  | 是否仍在配置裡（移除的置為 false，數據保留） |
| `created_by` | VARCHAR(64) | 否 |  | 建立者（登錄用戶名；未開登錄時為 local） |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |
| `last_login_at` | DATETIME | 是 |  | 上次登錄（UTC） |

## user_session

一次登錄。瀏覽器拿到的是隨機令牌，這裡只存它的 SHA-256，所以數據庫洩漏也冒充不了任何人。

| 列 | 類型 | 可空 | 鍵 | 說明 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | PK | 自增主鍵 |
| `token_hash` | VARCHAR(64) | 否 | UQ | 令牌的 SHA-256（不存原文） |
| `user_id` | INTEGER | 否 | FK IX | → user_account.id |
| `created_at` | DATETIME | 否 |  | 記錄建立時間（UTC） |
| `expires_at` | DATETIME | 否 |  | 過期時間（UTC） |
| `revoked_at` | DATETIME | 是 |  | 登出或被撤銷的時間（UTC） |
