# crudelab 標誌檔案

字標已外框化：所有字母都是向量路徑，SVG 裡沒有 `<text>`，也不引用任何字體，
沒有安裝 Nunito 的電腦、瀏覽器、Office、印刷廠都會顯示一致。

| 檔案 | 用途 |
|---|---|
| `crudelab-icon.svg` / `@16 @32 @64 @180 @512 @1024.png` | 圖標：網站圖標（16/32）、iOS 主畫面（180）、應用圖標（512/1024） |
| `crudelab-lockup-light.svg` / `.png` | 橫式組合（圖標＋字標），淺色背景用 |
| `crudelab-lockup-dark.svg` / `.png` | 橫式組合，深色背景用（建議背景 #1d1d1f） |
| `crudelab-wordmark-light.svg` / `.png` | 純字標，淺色背景 |
| `crudelab-wordmark-dark.svg` / `.png` | 純字標，深色背景 |

PNG 檔名的數字是像素高度，背景透明。網頁、文件優先用 SVG，可任意縮放不失真。

## 規格

- 字標：crude = Nunito ExtraBold 800，#1d1d1f；l·b = Nunito Light 300，#6e6e73；字距 −0.025em
- a 換成油滴：淺底深棕→黑（#5b3a22→#0d0805），深底琥珀（#ffc24a→#e0791c）
- 圖標：C3 分層燒瓶，層色 #ffd66b / #f29a2e / #8a4a1f / #160c06
- 組合：字標升部高約為圖標高的 57%，兩者間距 = 圖標寬的 26%

## 字體授權

Nunito 採用 SIL Open Font License 1.1（Copyright 2014 The Nunito Project Authors，
https://github.com/googlefonts/nunito）。授權允許把字形轉成外框用於標誌，可商用；
這裡只用了字形外框，沒有再分發字體檔本身。

## 平台裡用到的副本

- `src/crudelab/web/favicon.svg`：網站圖標（= crudelab-icon.svg）
- `src/crudelab/web/brand/`：頁首用的橫式組合（淺色／深色自動切換）、iOS 主畫面圖標、512 圖標

## 重新生成

`build_logo.py` 從 Nunito 字體檔重新產生全部檔案，步驟寫在腳本開頭。字體檔不進倉庫。
