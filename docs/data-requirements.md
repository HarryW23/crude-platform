# 數據與接口需求清單

> 這份文檔由 `crudelab data-needs --output docs/data-requirements.md` 從 `config/` 生成。不要手改；改了配置後重新生成。

系統目前定義了 **193 條序列**，供 161 個指標、5 個煉廠模型、2 張平衡表使用。下面按「從哪裡取得」分組。任何序列都可以用 Excel 導入或在後台手動錄入，接口只是省去人工。

## 一覽

| 來源 | 序列數 | 現在怎麼取得 | 需要的憑證 | 狀態 |
|---|---|---|---|---|
| EIA（美國能源信息署）公開數據 | 27 | 接口自動取數：後台「數據源」頁點「立即取數」，或 `crudelab pull eia` | `EIA_API_KEY`，在 eia.gov/opendata 免費申請 | 接口已寫好，**沒有用真實密鑰調用過** |
| 國際交易所期貨結算價（ICE、NYMEX、DME / IFAD） | 33 | 路透終端導出 Excel 後在後台導入 | 路透 / LSEG 訂閱；之後可接 LSEG API（`LSEG_*`） | Excel 導入可用；API 預留 |
| 國內交易所期貨結算價（INE、上期所、大商所、鄭商所） | 19 | 路透或 Wind 導出 Excel 後導入 | 路透 / Wind 等任一行情訂閱 | Excel 導入可用 |
| 報價機構的現貨評估價（Platts、Argus 等） | 44 | 路透終端導出 Excel 後導入 | 路透訂閱 **加** 對應報價機構的授權 | Excel 導入可用 |
| 產油國官方售價（沙特、伊拉克等） | 5 | 每月公佈一次，手動錄入或隨 Excel 導入 | 公開信息，路透有轉載 | 手動 / Excel |
| 油輪運價（波羅的海交易所 TD 航線） | 4 | 路透終端導出 Excel 後導入 | 路透訂閱（含波羅的海交易所數據） | Excel 導入可用 |
| 匯率 | 1 | 路透導出 Excel 後導入 | 任一行情訂閱 | Excel 導入可用 |
| 美元指數、美債收益率 | 4 | `scripts/lseg_export.py` 從路透導出後導入 | 路透 / LSEG 訂閱 | 導出腳本可用 |
| 國內現貨價（隆眾、卓創、金聯創等） | 10 | 資訊商網站或終端導出 Excel 後導入 | 國內資訊商訂閱；之後可接其 API（`CN_VENDOR_API_KEY`） | Excel 導入可用；API 預留 |
| 機構月報（IEA、OPEC） | 29 | 從月報的表格整理成 Excel 後導入 | IEA 月報需訂閱；OPEC 月報公開 | Excel 導入可用 |
| 煉廠檢修（諮詢機構） | 1 | 從諮詢機構的報告整理成 Excel 後導入 | IIR、Platts、Energy Aspects 等任一家的訂閱 | Excel 導入可用 |
| CFTC / ICE 持倉報告 | 6 | 定時任務自動取（CFTC 公開接口；ICE 由路透導出） | CFTC 不需要；ICE 見路透 | CFTC 接口已接通 |
| 金十數據 | 10 | 定時任務自動取（財經日曆、報價） | `JIN10_TOKEN`（`crudelab secret set JIN10_TOKEN`） | 已接通 |

序列有沒有數據、最新到哪一天，在後台的「序列」頁看。

## 各來源的序列

### EIA（美國能源信息署）公開數據

序列號是按記憶登記的，第一次取數後請核對每條是否取到、數量級是否合理。

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `EIA_CRUDE_STOCKS`　`PET.WCESTUS1.W` | 美國商業原油庫存 | kb | 周 | 供需 4 |
| `EIA_SPR_STOCKS`　`PET.WCSSTUS1.W` | 美國戰略儲備原油庫存 | kb | 周 | 供需 1 |
| `EIA_CUSHING_STOCKS`　`PET.W_EPC0_SAX_YCUOK_MBBL.W` | 庫欣原油庫存 | kb | 周 | 供需 1 |
| `EIA_GASOLINE_STOCKS`　`PET.WGTSTUS1.W` | 美國汽油庫存 | kb | 周 | 供需 1 |
| `EIA_DISTILLATE_STOCKS`　`PET.WDISTUS1.W` | 美國餾分油庫存 | kb | 周 | 供需 1 |
| `EIA_JET_STOCKS`　`PET.WKJSTUS1.W` | 美國航煤庫存 | kb | 周 | （目前沒有指標用到） |
| `EIA_CRUDE_PRODUCTION`　`PET.WCRFPUS2.W` | 美國原油產量 | kb/d | 周 | 供需 2 |
| `EIA_CRUDE_IMPORTS`　`PET.WCRIMUS2.W` | 美國原油進口 | kb/d | 周 | 供需 2 |
| `EIA_CRUDE_EXPORTS`　`PET.WCREXUS2.W` | 美國原油出口 | kb/d | 周 | 供需 2 |
| `EIA_REFINERY_INPUT`　`PET.WCRRIUS2.W` | 美國煉廠原油加工量 | kb/d | 周 | 供需 4 |
| `EIA_REFINERY_UTIL`　`PET.WPULEUS3.W` | 美國煉廠開工率 | pct | 周 | 煉廠 2 |
| `EIA_REFINERY_GROSS_INPUT`　`PET.WGIRIUS2.W` | 美國煉廠總投入量 | kb/d | 周 | 煉廠 2 |
| `EIA_REFINERY_CAPACITY`　`PET.WOCLEUS2.W` | 美國煉廠可運行產能（常減壓） | kb/d | 周 | 煉廠 2 |
| `EIA_PADD3_REFINERY_UTIL`　`PET.W_NA_YUP_R30_PER.W` | 美灣（PADD 3）煉廠開工率 | pct | 周 | 煉廠 1 |
| `EIA_PRODUCT_SUPPLIED`　`PET.WRPUPUS2.W` | 美國成品油表觀需求（合計） | kb/d | 周 | 供需 2 |
| `EIA_GASOLINE_SUPPLIED`　`PET.WGFUPUS2.W` | 美國汽油表觀需求 | kb/d | 周 | 供需 2 |
| `EIA_DISTILLATE_SUPPLIED`　`PET.WDIUPUS2.W` | 美國餾分油表觀需求 | kb/d | 周 | 供需 2 |
| `EIA_JET_SUPPLIED`　`PET.WKJUPUS2.W` | 美國航煤表觀需求 | kb/d | 周 | （目前沒有指標用到） |
| `STEO_OPEC_SUPPLY`　`STEO.PAPR_OPEC.M` | OPEC 液體燃料產量（EIA） | mb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 3 |
| `STEO_NONOPEC_SUPPLY`　`STEO.PAPR_NONOPEC.M` | 非 OPEC 液體燃料產量（EIA） | mb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 3 |
| `STEO_OPEC_CRUDE`　`STEO.COPR_OPEC.M` | OPEC 原油產量（EIA） | mb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 1 |
| `STEO_US_CRUDE`　`STEO.COPRPUS.M` | 美國原油產量（EIA 月度） | mb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `STEO_OECD_DEMAND`　`STEO.PATC_OECD.M` | OECD 需求（EIA） | mb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 4 |
| `STEO_NONOECD_DEMAND`　`STEO.PATC_NON_OECD.M` | 非 OECD 需求（EIA） | mb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 3 |
| `STEO_CHINA_DEMAND`　`STEO.PATC_CH.M` | 中國需求（EIA） | mb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `STEO_US_DEMAND`　`STEO.PATC_US.M` | 美國需求（EIA 月度） | mb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `STEO_OECD_STOCKS`　`STEO.PASC_OECD_T3.M` | OECD 商業庫存（EIA） | mb | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 2 |

### 國際交易所期貨結算價（ICE、NYMEX、DME / IFAD）

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `BRENT_M1` | 布倫特期貨 M1 | USD/bbl | 日 | 煉廠 6、地域價差 11 |
| `BRENT_M2` | 布倫特期貨 M2 | USD/bbl | 日 | 地域價差 1 |
| `BRENT_M3` | 布倫特期貨 M3 | USD/bbl | 日 | 地域價差 1 |
| `BRENT_M4` | 布倫特期貨 M4 | USD/bbl | 日 | （目前沒有指標用到） |
| `BRENT_M5` | 布倫特期貨 M5 | USD/bbl | 日 | （目前沒有指標用到） |
| `BRENT_M6` | 布倫特期貨 M6 | USD/bbl | 日 | 地域價差 3 |
| `BRENT_M7` | 布倫特期貨 M7 | USD/bbl | 日 | （目前沒有指標用到） |
| `BRENT_M8` | 布倫特期貨 M8 | USD/bbl | 日 | （目前沒有指標用到） |
| `BRENT_M9` | 布倫特期貨 M9 | USD/bbl | 日 | （目前沒有指標用到） |
| `BRENT_M10` | 布倫特期貨 M10 | USD/bbl | 日 | （目前沒有指標用到） |
| `BRENT_M11` | 布倫特期貨 M11 | USD/bbl | 日 | （目前沒有指標用到） |
| `BRENT_M12` | 布倫特期貨 M12 | USD/bbl | 日 | 地域價差 3 |
| `WTI_M1` | WTI 期貨 M1 | USD/bbl | 日 | 煉廠 3、地域價差 10 |
| `WTI_M2` | WTI 期貨 M2 | USD/bbl | 日 | 地域價差 5 |
| `WTI_M3` | WTI 期貨 M3 | USD/bbl | 日 | 地域價差 1 |
| `WTI_M4` | WTI 期貨 M4 | USD/bbl | 日 | （目前沒有指標用到） |
| `WTI_M5` | WTI 期貨 M5 | USD/bbl | 日 | （目前沒有指標用到） |
| `WTI_M6` | WTI 期貨 M6 | USD/bbl | 日 | 地域價差 3 |
| `WTI_M7` | WTI 期貨 M7 | USD/bbl | 日 | （目前沒有指標用到） |
| `WTI_M8` | WTI 期貨 M8 | USD/bbl | 日 | （目前沒有指標用到） |
| `WTI_M9` | WTI 期貨 M9 | USD/bbl | 日 | （目前沒有指標用到） |
| `WTI_M10` | WTI 期貨 M10 | USD/bbl | 日 | （目前沒有指標用到） |
| `WTI_M11` | WTI 期貨 M11 | USD/bbl | 日 | （目前沒有指標用到） |
| `WTI_M12` | WTI 期貨 M12 | USD/bbl | 日 | 地域價差 3 |
| `OMAN_M1` | 阿曼期貨 M1 | USD/bbl | 日 | 煉廠 1、地域價差 8 |
| `MURBAN_M1` | 穆爾班期貨 M1 | USD/bbl | 日 | 地域價差 1 |
| `GASOIL_ICE_M1` | ICE 柴油期貨 M1 | USD/t | 日 | 煉廠 1 |
| `GASOIL_ICE_M2` | ICE 柴油期貨 M2 | USD/t | 日 | （目前沒有指標用到） |
| `GASOIL_ICE_M3` | ICE 柴油期貨 M3 | USD/t | 日 | （目前沒有指標用到） |
| `RBOB_M1` | RBOB 汽油期貨 M1 | USD/gal | 日 | 煉廠 2 |
| `RBOB_M2` | RBOB 汽油期貨 M2 | USD/gal | 日 | （目前沒有指標用到） |
| `HO_M1` | 取暖油（ULSD）期貨 M1 | USD/gal | 日 | 煉廠 2 |
| `HO_M2` | 取暖油（ULSD）期貨 M2 | USD/gal | 日 | （目前沒有指標用到） |

### 國內交易所期貨結算價（INE、上期所、大商所、鄭商所）

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `SC_M1` | 上海原油期貨 M1 | CNY/bbl | 日 | 煉廠 7、地域價差 8 |
| `SC_M2` | 上海原油期貨 M2 | CNY/bbl | 日 | （目前沒有指標用到） |
| `SC_M3` | 上海原油期貨 M3 | CNY/bbl | 日 | 地域價差 1 |
| `SC_M4` | 上海原油期貨 M4 | CNY/bbl | 日 | （目前沒有指標用到） |
| `SC_M5` | 上海原油期貨 M5 | CNY/bbl | 日 | （目前沒有指標用到） |
| `SC_M6` | 上海原油期貨 M6 | CNY/bbl | 日 | 地域價差 3 |
| `SC_M7` | 上海原油期貨 M7 | CNY/bbl | 日 | （目前沒有指標用到） |
| `SC_M8` | 上海原油期貨 M8 | CNY/bbl | 日 | （目前沒有指標用到） |
| `SC_M9` | 上海原油期貨 M9 | CNY/bbl | 日 | （目前沒有指標用到） |
| `SC_M10` | 上海原油期貨 M10 | CNY/bbl | 日 | （目前沒有指標用到） |
| `SC_M11` | 上海原油期貨 M11 | CNY/bbl | 日 | （目前沒有指標用到） |
| `SC_M12` | 上海原油期貨 M12 | CNY/bbl | 日 | 地域價差 3 |
| `FU_M1` | 上期所燃料油主力 | CNY/t | 日 | （目前沒有指標用到） |
| `LU_M1` | INE 低硫燃料油主力 | CNY/t | 日 | （目前沒有指標用到） |
| `BU_M1` | 上期所瀝青主力 | CNY/t | 日 | （目前沒有指標用到） |
| `PP_CN` | 聚丙烯期貨主力 | CNY/t | 日 | 煉廠 2 |
| `LLDPE_CN` | LLDPE 期貨主力 | CNY/t | 日 | 煉廠 2 |
| `PTA_CN` | PTA 期貨主力 | CNY/t | 日 | 煉廠 1 |
| `MEG_CN` | 乙二醇期貨主力 | CNY/t | 日 | 煉廠 2 |

### 報價機構的現貨評估價（Platts、Argus 等）

即期布倫特、迪拜現貨與掉期、各油種現貨升貼水、新加坡和西北歐成品油、化工品外盤價，多數是報價機構的評估價。它們在路透終端裡通常需要另外訂閱才看得到，**請確認你們的訂閱範圍**，以及授權是否允許把數據存進自建系統。

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `DUBAI_SWAP_M1` | 迪拜掉期 M1 | USD/bbl | 日 | 煉廠 9、地域價差 11 |
| `DUBAI_SWAP_M2` | 迪拜掉期 M2 | USD/bbl | 日 | 煉廠 1、地域價差 4 |
| `DUBAI_SWAP_M3` | 迪拜掉期 M3 | USD/bbl | 日 | 地域價差 1 |
| `DUBAI_SWAP_M4` | 迪拜掉期 M4 | USD/bbl | 日 | （目前沒有指標用到） |
| `DUBAI_SWAP_M5` | 迪拜掉期 M5 | USD/bbl | 日 | （目前沒有指標用到） |
| `DUBAI_SWAP_M6` | 迪拜掉期 M6 | USD/bbl | 日 | 地域價差 3 |
| `DUBAI_SWAP_M7` | 迪拜掉期 M7 | USD/bbl | 日 | （目前沒有指標用到） |
| `DUBAI_SWAP_M8` | 迪拜掉期 M8 | USD/bbl | 日 | （目前沒有指標用到） |
| `DUBAI_SWAP_M9` | 迪拜掉期 M9 | USD/bbl | 日 | （目前沒有指標用到） |
| `DUBAI_SWAP_M10` | 迪拜掉期 M10 | USD/bbl | 日 | （目前沒有指標用到） |
| `DUBAI_SWAP_M11` | 迪拜掉期 M11 | USD/bbl | 日 | （目前沒有指標用到） |
| `DUBAI_SWAP_M12` | 迪拜掉期 M12 | USD/bbl | 日 | 地域價差 3 |
| `DATED_BRENT` | 即期布倫特 | USD/bbl | 日 | 地域價差 8、煉廠 1 |
| `DUBAI_CASH` | 迪拜現貨 | USD/bbl | 日 | 煉廠 1、地域價差 7 |
| `BRENT_DUBAI_EFS` | Brent-Dubai EFS | USD/bbl | 日 | （目前沒有指標用到） |
| `ESPO_DIFF` | ESPO 升貼水 | USD/bbl | 日 | 煉廠 1、地域價差 3 |
| `URALS_DIFF` | 烏拉爾升貼水 | USD/bbl | 日 | 地域價差 2 |
| `UPPER_ZAKUM_DIFF` | 上扎庫姆升貼水 | USD/bbl | 日 | 地域價差 1 |
| `FORTIES_DIFF` | 福蒂斯升貼水 | USD/bbl | 日 | 地域價差 2 |
| `EKOFISK_DIFF` | 埃科菲斯克升貼水 | USD/bbl | 日 | （目前沒有指標用到） |
| `JOHAN_SVERDRUP_DIFF` | 約翰斯維德魯普升貼水 | USD/bbl | 日 | （目前沒有指標用到） |
| `CPC_DIFF` | CPC 升貼水 | USD/bbl | 日 | 地域價差 1 |
| `BONNY_LIGHT_DIFF` | 邦尼輕質升貼水 | USD/bbl | 日 | 地域價差 2 |
| `WTI_MIDLAND_DIFF` | WTI 米德蘭升貼水 | USD/bbl | 日 | （目前沒有指標用到） |
| `WTI_MEH_DIFF` | WTI 休斯頓升貼水 | USD/bbl | 日 | 地域價差 3 |
| `MARS_DIFF` | Mars 升貼水 | USD/bbl | 日 | 地域價差 2 |
| `WCS_HARDISTY_DIFF` | WCS（哈迪斯蒂）升貼水 | USD/bbl | 日 | 地域價差 1 |
| `TUPI_CHINA_DIFF` | Tupi 到中國升貼水（到岸） | USD/bbl | 日 | 地域價差 1 |
| `SG_GASOLINE_92` | 新加坡 92# 汽油 | USD/bbl | 日 | 煉廠 3 |
| `SG_GASOIL_10PPM` | 新加坡 10ppm 柴油 | USD/bbl | 日 | 煉廠 4 |
| `SG_JET_KERO` | 新加坡航煤 | USD/bbl | 日 | 煉廠 4 |
| `SG_NAPHTHA` | 新加坡石腦油 | USD/bbl | 日 | 煉廠 3 |
| `SG_HSFO_380` | 新加坡 380cst 高硫燃料油 | USD/t | 日 | 煉廠 4 |
| `SG_VLSFO` | 新加坡 0.5% 低硫燃料油 | USD/t | 日 | 煉廠 2 |
| `JAPAN_NAPHTHA` | 日本到岸石腦油 | USD/t | 日 | 煉廠 2 |
| `NWE_EUROBOB` | 西北歐 Eurobob 汽油 | USD/t | 日 | 煉廠 2 |
| `NWE_DIESEL_10PPM` | 西北歐 10ppm 柴油 | USD/t | 日 | 煉廠 2 |
| `NWE_JET` | 西北歐航煤 | USD/t | 日 | 煉廠 2 |
| `NWE_NAPHTHA` | 西北歐石腦油 | USD/t | 日 | 煉廠 2 |
| `RDAM_HSFO_35` | 鹿特丹 3.5% 燃料油 | USD/t | 日 | 煉廠 2 |
| `PX_CFR_CHINA` | PX CFR 中國 | USD/t | 日 | 煉廠 5 |
| `BENZENE_FOB_KOREA` | 純苯 FOB 韓國 | USD/t | 日 | （目前沒有指標用到） |
| `ETHYLENE_CFR_NEA` | 乙烯 CFR 東北亞 | USD/t | 日 | 煉廠 1 |
| `PROPYLENE_CFR_CHINA` | 丙烯 CFR 中國 | USD/t | 日 | （目前沒有指標用到） |

### 產油國官方售價（沙特、伊拉克等）

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `ARAB_LIGHT_OSP_ASIA` | 沙特輕質官價（亞洲） | USD/bbl | 月（只對當期生效：日期記適用期間的 1 日，例如官價記裝船月） | 地域價差 3 |
| `ARAB_MEDIUM_OSP_ASIA` | 沙特中質官價（亞洲） | USD/bbl | 月（只對當期生效：日期記適用期間的 1 日，例如官價記裝船月） | 煉廠 1、地域價差 3 |
| `ARAB_HEAVY_OSP_ASIA` | 沙特重質官價（亞洲） | USD/bbl | 月（只對當期生效：日期記適用期間的 1 日，例如官價記裝船月） | 地域價差 1 |
| `BASRAH_MEDIUM_OSP_ASIA` | 巴士拉中質官價（亞洲） | USD/bbl | 月（只對當期生效：日期記適用期間的 1 日，例如官價記裝船月） | 地域價差 2 |
| `BASRAH_HEAVY_OSP_ASIA` | 巴士拉重質官價（亞洲） | USD/bbl | 月（只對當期生效：日期記適用期間的 1 日，例如官價記裝船月） | （目前沒有指標用到） |

### 油輪運價（波羅的海交易所 TD 航線）

WS 點折成 美元/桶 還需要 Worldscale 每年的基準費率，見下面「需要外部數據的參數」。

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `TD3C_WS` | TD3C 中東→中國 VLCC | WS | 日 | 煉廠 1、地域價差 5 |
| `TD15_WS` | TD15 西非→中國 VLCC | WS | 日 | 地域價差 3 |
| `TD20_WS` | TD20 西非→歐洲 蘇伊士型 | WS | 日 | 地域價差 2 |
| `TD25_WS` | TD25 美灣→歐洲 阿芙拉型 | WS | 日 | 地域價差 2 |

### 匯率

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `USDCNY` | 美元兌人民幣（1 美元合多少人民幣） | CNY/USD | 日 | 資金持倉 1、煉廠 6、地域價差 4 |

### 美元指數、美債收益率

國債 RIC 同時有價格和收益率字段：配置裡的 lseg.field 要指定收益率那個，否則會取到價格。

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `MACRO_DXY` | 美元指數 | idx | 日 | 資金持倉 1 |
| `MACRO_UST2Y` | 美國 2 年期國債收益率 | pct | 日 | 資金持倉 1 |
| `MACRO_UST10Y` | 美國 10 年期國債收益率 | pct | 日 | 資金持倉 3 |
| `MACRO_REAL10Y` | 美國 10 年期實際利率（TIPS） | pct | 日 | 資金持倉 2 |

### 國內現貨價（隆眾、卓創、金聯創等）

山東地煉汽柴油、液化氣、石油焦，華東汽柴油和純苯，航煤出廠價。兩個中國煉廠模型依賴這一組。

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `CN_SD_TEAPOT_UTIL` | 山東地煉常減壓開工率 | pct | 周 | 煉廠 2 |
| `CN_MAJOR_REFINERY_UTIL` | 中國主營煉廠開工率 | pct | 周 | 煉廠 1 |
| `SD_GASOLINE_92` | 山東地煉 92# 汽油 | CNY/t | 日 | 煉廠 4 |
| `SD_DIESEL_0` | 山東地煉 0# 柴油 | CNY/t | 日 | 煉廠 4 |
| `SD_LPG` | 山東民用液化氣 | CNY/t | 日 | 煉廠 2 |
| `SD_PETCOKE` | 山東地煉石油焦 | CNY/t | 日 | 煉廠 2 |
| `EC_GASOLINE_92` | 華東 92# 汽油 | CNY/t | 日 | 煉廠 2 |
| `EC_DIESEL_0` | 華東 0# 柴油 | CNY/t | 日 | 煉廠 2 |
| `CN_JET_EXWORKS` | 中國航煤出廠價 | CNY/t | 月 | 煉廠 2 |
| `BENZENE_EC` | 華東純苯 | CNY/t | 日 | 煉廠 2 |

### 機構月報（IEA、OPEC）

IEA / OPEC 的供需只用於和 EIA 口徑對照，不影響平衡表本身。OPEC+ 分國產量（CRUDE_PROD_*，千桶/日）取 OPEC 月報的二手來源表：在後台導入頁直接上傳月報 PDF（數據源選「OPEC 月報（二手來源）」），讀不出時用導入模板貼上；配額和可持續產能是參數（config/parameters/opec.yaml），需要你按 OPEC+ 的決定錄入。

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `IEA_WORLD_DEMAND` | 全球需求（IEA） | mb/d | 季（按報告期保存：補錄歷史報告時填發佈日期） | 供需 2 |
| `IEA_WORLD_SUPPLY` | 全球供應（IEA） | mb/d | 季（按報告期保存：補錄歷史報告時填發佈日期） | 供需 1 |
| `OPEC_WORLD_DEMAND` | 全球需求（OPEC） | mb/d | 季（按報告期保存：補錄歷史報告時填發佈日期） | 供需 1 |
| `OPEC_CALL_ON_OPEC` | 對 OPEC 原油的需求（OPEC） | mb/d | 季（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_SAUDI` | 沙特原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 4 |
| `CRUDE_PROD_IRAQ` | 伊拉克原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 4 |
| `CRUDE_PROD_UAE` | 阿聯酋原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 4 |
| `CRUDE_PROD_KUWAIT` | 科威特原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 4 |
| `CRUDE_PROD_IRAN` | 伊朗原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_RUSSIA` | 俄羅斯原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 2 |
| `CRUDE_PROD_OPEC` | OPEC 原油產量合計（二手來源） | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_ALGERIA` | 阿爾及利亞原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 2 |
| `CRUDE_PROD_CONGO` | 剛果（布）原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_EQ_GUINEA` | 赤道幾內亞原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_GABON` | 加蓬原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_LIBYA` | 利比亞原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_NIGERIA` | 尼日利亞原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_VENEZUELA` | 委內瑞拉原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_AZERBAIJAN` | 阿塞拜疆原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_BAHRAIN` | 巴林原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_BRUNEI` | 文萊原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_KAZAKHSTAN` | 哈薩克斯坦原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 2 |
| `CRUDE_PROD_MALAYSIA` | 馬來西亞原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_MEXICO` | 墨西哥原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_OMAN` | 阿曼原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | 供需 2 |
| `CRUDE_PROD_SUDAN` | 蘇丹原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_SOUTH_SUDAN` | 南蘇丹原油產量 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_NON_OPEC_DOC` | OPEC+ 非歐佩克國家原油產量合計 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |
| `CRUDE_PROD_DOC` | OPEC+ 原油產量合計 | kb/d | 月（按報告期保存：補錄歷史報告時填發佈日期） | （目前沒有指標用到） |

### 煉廠檢修（諮詢機構）

全球煉廠停工量（檢修 + 非計劃）。各家口徑不同，固定用一家。

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `GLOBAL_REFINERY_OUTAGES` | 全球煉廠停工量（檢修 + 非計劃） | kb/d | 月 | 煉廠 2 |

### CFTC / ICE 持倉報告

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `CFTC_WTI_MM_LONG` | WTI 管理基金多頭 | lots | 周 | 資金持倉 5 |
| `CFTC_WTI_MM_SHORT` | WTI 管理基金空頭 | lots | 周 | 資金持倉 5 |
| `CFTC_WTI_OI` | WTI 總持倉量 | lots | 周 | 資金持倉 1 |
| `ICE_BRENT_MM_LONG` | Brent 管理基金多頭 | lots | 周 | 資金持倉 5 |
| `ICE_BRENT_MM_SHORT` | Brent 管理基金空頭 | lots | 周 | 資金持倉 5 |
| `ICE_BRENT_OI` | Brent 總持倉量 | lots | 周 | 資金持倉 1 |

### 金十數據

報價只保存最近 1 個交易日，歷史補不了，從接上那天起積累。

| 代碼 | 名稱 | 單位 | 頻率 | 用到它的指標 |
|---|---|---|---|---|
| `CAL_EIA_CRUDE_SURPRISE` | EIA 原油庫存超預期 | kb | 周 | （目前沒有指標用到） |
| `CAL_EIA_GASOLINE_SURPRISE` | EIA 汽油庫存超預期 | kb | 周 | （目前沒有指標用到） |
| `CAL_EIA_DISTILLATE_SURPRISE` | EIA 餾分油庫存超預期 | kb | 周 | （目前沒有指標用到） |
| `CAL_API_CRUDE_SURPRISE` | API 原油庫存超預期 | kb | 周 | （目前沒有指標用到） |
| `MACRO_USDCNH` | 美元兌離岸人民幣 | CNY/USD | 日 | 資金持倉 2 |
| `MACRO_EURUSD` | 歐元兌美元 | USD/EUR | 日 | 資金持倉 1 |
| `MACRO_USDJPY` | 美元兌日元 | JPY/USD | 日 | 資金持倉 1 |
| `MACRO_XAUUSD` | 現貨黃金 | USD/oz | 日 | 資金持倉 2 |
| `JIN10_UKOIL` | 布倫特原油報價（金十） | USD/bbl | 日 | 資金持倉 1 |
| `JIN10_USOIL` | WTI 原油報價（金十） | USD/bbl | 日 | （目前沒有指標用到） |

## 需要外部數據的參數

這些參數沒有預設值，要你提供；沒填時用到它們的指標顯示「缺參數」。在後台「參數」頁錄入。

| 參數 | 名稱 | 單位 | 說明 |
|---|---|---|---|
| `FLAT_TD3C` | TD3C 基準費率（拉斯坦努拉→寧波） | USD/t | Worldscale 年度 flat rate |
| `FLAT_TD15` | TD15 基準費率（西非→寧波） | USD/t | Worldscale 年度 flat rate |
| `FLAT_TD20` | TD20 基準費率（西非→鹿特丹） | USD/t | Worldscale 年度 flat rate |
| `FLAT_TD25` | TD25 基準費率（美灣→歐洲） | USD/t | Worldscale 年度 flat rate |
| `ESPO_FREIGHT_CN` | ESPO 科茲米諾→山東 運費 | USD/bbl | 短程阿芙拉型運費，波動大，請按實際成交水平錄入並隨時更新 |
| `OPEC_QUOTA_SAUDI` | 沙特配額 | kb/d |  |
| `OPEC_QUOTA_IRAQ` | 伊拉克配額 | kb/d |  |
| `OPEC_QUOTA_UAE` | 阿聯酋配額 | kb/d |  |
| `OPEC_QUOTA_KUWAIT` | 科威特配額 | kb/d |  |
| `OPEC_QUOTA_RUSSIA` | 俄羅斯配額 | kb/d |  |
| `OPEC_QUOTA_KAZAKHSTAN` | 哈薩克斯坦配額 | kb/d |  |
| `OPEC_QUOTA_ALGERIA` | 阿爾及利亞配額 | kb/d |  |
| `OPEC_QUOTA_OMAN` | 阿曼配額 | kb/d |  |
| `OPEC_CAPACITY_SAUDI` | 沙特可持續產能 | kb/d |  |
| `OPEC_CAPACITY_IRAQ` | 伊拉克可持續產能 | kb/d |  |
| `OPEC_CAPACITY_UAE` | 阿聯酋可持續產能 | kb/d |  |
| `OPEC_CAPACITY_KUWAIT` | 科威特可持續產能 | kb/d |  |
| `PROJ_PRICE_PER_STOCK` | 庫存對價格的敏感度 | USD/bbl/mb | 全球庫存每多累積 1 百萬桶，Brent 下跌多少美元/桶。推演按期限算累庫：過剩 1 百萬桶/日持續 30 天 = 累庫 30 百萬桶。 這是推演的核心假設，沒有預設值。推演頁會給出從歷史估計的值和它的可信度，可以在這裡採用，也可以自己填 |

其中運費基準費率（`FLAT_*`）來自 Worldscale 協會的年度費率表，需要訂閱，每年 1 月 1 日更新一次，錄入時按年填生效日期。

## 預留、尚未使用的數據接口

| 接口 | 憑證 | 能提供什麼 | 現狀 |
|---|---|---|---|
| Kpler | `KPLER_API_TOKEN` | 船期、到港量、浮倉、分國別進出口流量 | 目前沒有任何序列依賴它。接通後可以把全球平衡表從「機構口徑」細化到分國別自下而上。 |
| 路透 / LSEG Workspace | App Key（在 Workspace 裡生成，只在那台電腦上用） | 以 RIC 直接取行情，取代人工導出 Excel | `scripts/lseg_export.py` 在裝了 Workspace 的電腦上導出 CSV，再導入；RIC 清單見 `scripts/lseg_rics.txt` |
| 國內資訊商 API | `CN_VENDOR_API_KEY` | 國內現貨價、開工率、庫存 | 預留 |

## 大模型接口

在後台「大模型」頁選擇用哪一家、哪個型號、開不開思考模式；不選時相關功能停用，其餘不受影響。每次調用都受 `config/llm.yaml` 的頻率和用量上限約束，出錯會自動暫停，不自動重試。

型號不內置預設值，從供應商返回的模型列表裡選（供應商會改名、停用舊名字）。

| 供應商 | 憑證 | 思考模式 | 狀態 |
|---|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | 支持（請求參數 `thinking`） | 請求格式用本機模擬服務驗過；沒有用真實密鑰調用過 |
| OpenAI | `OPENAI_API_KEY` | 不支持 | 與 DeepSeek 共用同一段接入代碼；沒有用真實密鑰調用過 |
| Claude | `ANTHROPIC_API_KEY` | 不支持 | 沒有調用過 |

**用大模型時，數據會離開你的系統。** 發給供應商的內容：

| 功能 | 發出去的內容 |
|---|---|
| 從文字抽取事件 | 你貼進去的那段文字，加上地區字典 |
| 報告解讀 | 整份報告正文：各指標的最新值、平衡表、毛利、事件、推演結果 |
| 問答 | 你的問題、對話歷史，以及它查到的數據（指標值、序列的最近幾個點、平衡表、毛利拆解等） |

報告和問答會把由行情算出來的數字發給第三方。行情數據（尤其是報價機構的評估價）的授權通常限制再分發，**啟用前請確認你們的數據授權是否允許**。只想避開這個問題的話，可以只用「抽取事件」，或不配置大模型。

## 憑證清單與保管

系統認得的全部憑證。除了它們，沒有別的地方需要密鑰。

| 名稱 | 用途 |
|---|---|
| `EIA_API_KEY` | EIA 公開數據（免費申請） |
| `DEEPSEEK_API_KEY` | DeepSeek 大模型 |
| `OPENAI_API_KEY` | OpenAI 大模型 |
| `ANTHROPIC_API_KEY` | Claude 大模型 |
| `CRUDELAB_LLM_API_KEY` | 其他兼容接口的大模型（CRUDELAB_LLM_PROVIDER=compatible） |
| `DATABASE_URL` | 數據庫連接串（用 PostgreSQL 時含密碼） |
| `NOTIFY_WECOM_WEBHOOK` | 通知：企業微信群機器人的地址 |
| `NOTIFY_DINGTALK_WEBHOOK` | 通知：釘釘群機器人的地址 |
| `NOTIFY_FEISHU_WEBHOOK` | 通知：飛書群機器人的地址 |
| `NOTIFY_SLACK_WEBHOOK` | 通知：Slack 的 incoming webhook 地址 |
| `NOTIFY_WEBHOOK_URL` | 通知：自定義接收地址 |
| `SMTP_PASSWORD` | 通知：發郵件用的郵箱密碼或授權碼 |
| `JIN10_TOKEN` | 金十數據（MCP）的 Bearer token |
| `LSEG_APP_KEY` | 路透 / LSEG 數據 API（預留） |
| `LSEG_USERNAME` | 路透 / LSEG 帳號（預留） |
| `LSEG_PASSWORD` | 路透 / LSEG 密碼（預留） |
| `KPLER_API_TOKEN` | Kpler 數據 API（預留） |
| `CN_VENDOR_API_KEY` | 國內資訊商數據 API（預留） |

通知類的密鑰（`NOTIFY_*`、`SMTP_PASSWORD`）可以加大寫後綴存好幾個群，例如 `NOTIFY_WECOM_WEBHOOK_TRADING`，保管規則完全相同（脫敏、文件權限都一樣適用）。

保管規則：

- **放在哪裡**：密鑰文件 `~/.crudelab/secrets.env`（在項目文件夾之外，權限僅本人可讀寫），或環境變量。用 `crudelab secret set 名稱` 寫入，輸入不回顯，也不會留在終端的歷史記錄裡。
- **不放在哪裡**：git 倉庫、`config/` 下的任何文件、數據庫、`data/` 文件夾、頁面、聊天記錄、郵件。
- **查看狀態**：`crudelab secret list` 只顯示每個憑證設了沒有、來自哪裡，任何時候都不顯示內容。
- **系統怎麼防洩漏**：密鑰不會出現在接口回應、頁面、日誌、錯誤訊息、操作日誌裡；外部服務出錯時只報狀態碼，不轉述對方返回的原文；所有日誌和錯誤回應另有一層自動遮蔽。
- **倉庫怎麼防誤提交**：`.gitignore` 排除常見的密鑰文件；`scripts/check_secrets.py` 在每次推送時由 CI 掃描。
- **一旦懷疑洩漏**：立刻到供應商後台把那個密鑰作廢並重發，再用 `crudelab secret set` 換上新的。不要只是把它從文件裡刪掉——已經洩漏的密鑰刪了也還能用。
- **申請密鑰時**：能限制權限、額度、來源 IP 的都限制上。大模型的密鑰建議設月度消費上限。
