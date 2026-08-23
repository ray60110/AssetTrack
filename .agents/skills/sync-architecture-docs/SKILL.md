---
name: sync-architecture-docs
description: >-
  Keeps AssetTrack glossary.md and blockDiagram.md paired with the live design.
  Use when adding architecture, a module, screen, protocol, data file, or
  external service; when changing existing design, naming, definitions,
  communication paths, thresholds, or investment advice; when retiring a
  feature from the TUI; or when another skill just changed how AssetTrack
  works.
---

# Sync Architecture Docs

`glossary.md` 是名稱與「實際還做什麼」的對照。`blockDiagram.md` 是架構、協定與建議邏輯。兩者必須描述**同一份現行工作樹**，同一輪改完。

## 何時必須跑

任一項為真就跑，不要等使用者提醒：

- 新架構／新技術／新模組／新畫面／新協定／新外部服務／新資料檔
- 既有設計、溝通路徑、門檻或投資建議邏輯變更
- 命名或定義變更（含中文顯示名、快捷鍵、檔名）
- 功能下線、畫面不再呼叫、搬到 QuantTrade 或改成函式庫 only

**不必跑：** 純 typo、純測試、純格式、不改行為的 bugfix。

**完成標準：** 兩份檔都已改（或已核對無需改的那幾段）、日期已刷新、沒有只改其中一份、沒有把下線功能寫成現行建議。

## 步驟

1. **列出差額。** 對照這次 diff，寫出變更的名稱、溝通路徑、資料契約、建議規則。完成標準：每項都標了要進 glossary、blockDiagram，或兩者。
2. **先改 `glossary.md`。** 英文名、中文名、實際功能必須是現況。新增列；舊名若不再由 TUI 使用，移到「已刪除或不再由本套件實作」，標「（已移除）」或「（函式庫，畫面不呼叫）」。刷新文首「最後依據」日期。完成標準：這次出現的公開名稱在表裡找得到，且定義不再描述舊行為。
3. **再改 `blockDiagram.md`。** 架構圖、§5 溝通協定表、資料檔、背景時序、§11 建議邏輯、§12 檔案對照——只改被這次 diff 碰到的章節，但協定或建議一變就必須改對應段落。圖上的箭頭若代表新溝通，寫明機制與協定（函式呼叫、`push_screen`、JSONL、HTTPS、Keychain、subprocess exit code 等）。刷新文首日期。完成標準：讀者只看這份檔就能回答「誰跟誰說話、用什麼協定、建議怎麼判定」。
4. **交叉核對。** glossary 的新名在 blockDiagram 有歸屬；blockDiagram 的新協定／建議在 glossary 有列。領域詞（Performance Tracking、Champion 等）若定義變了，另改 `CONTEXT.md`。完成標準：兩份檔沒有互相矛盾的「現行」描述。

## 分工（避免寫錯檔）

| 寫進 `glossary.md` | 寫進 `blockDiagram.md` |
|---|---|
| 程式名、中文名、這支東西現在做什麼 | 為什麼這樣接、用什麼協定接 |
| Screen／Modal／action／儲存函式列 | 分層圖、時序、HTTPS／IPC／磁碟匯流排 |
| 「畫面不呼叫」與已刪除舊名 | §11 建議規則、門檻、棄權條件 |

不要把協定細節堆進 glossary 表格；不要只在 blockDiagram 發明一個 glossary 沒有的公開名稱。

## 反例

- 加了新 Screen，只改 `tui.py` → 未完成。
- 期權改回方向建議，只改 `options_analysis.py` → 未完成。
- 換了 SEC／FRED／新 API，圖沒改箭頭與協定列 → 未完成。
- 只更新 glossary、blockDiagram 仍寫舊主頁三卡或已刪的 `INVESTMENT_LOGIC.md` → 未完成。
