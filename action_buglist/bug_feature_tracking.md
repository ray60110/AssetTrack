---
tags: [AssetTrack, TUI]
GitHub version: v0.0.2
Local version: v0.0.3-dev
---

# 🎯 AssetTrack Bug & Feature Tracking

> [!IMPORTANT]
> **維護規範 (Maintenance Rules)**
> 1. **序號唯一且不可變動**：每個項目獲派 `bug#XXXXX` 序號後，絕對禁止修改。
> 2. **狀態控制機制**：
>    - AI Agent 僅能將問題記為 `[open]` 並填寫 root cause / solution / fixed by。
>    - **禁止** AI 自行將狀態修改為 `[closed]`。只有使用者驗證通過後，才能手動改為 `[closed]`。
> 3. **用字精簡**：所有描述、root cause 與 solution 必須精煉、直指核心。

---

## 📋 待處理與進行中項目 (Open Items)

1. [open] [bug#00014] [newfeature] **進階選擇權追蹤與 Black-Scholes 希臘字母監控 (Advanced Option Metrics & Greeks)**
   * **問題描述**：對於選擇權持倉，除了基礎參數外，缺乏希臘字母（Delta, Gamma, Theta, Vega）的估算，難以監控時間值衰減 (Theta Decay) 或價內外狀態 (ITM/OTM)。
   * **root cause**：
   * **solution**：
   * **fixed by**：

2. [open] [bug#00015] [newfeature] **多資產整合擴充（現金與加密貨幣錢包支援） (Multi-Asset Class Support: Cash & Crypto)**
   * **問題描述**：系統定位為統合性資產整合追蹤，但目前僅限於證券與期權。應擴充支援非證券的固定資產/現金科目（如銀行存款、數位穩定幣）手動登錄，以及加密貨幣公鏈餘額與交易所 API 自動同步。
   * **root cause**：
   * **solution**：
   * **fixed by**：

3. [open] [bug#00039] [newfeature] **市場主動式 ETF 績效與持股分析功能 — 三欄式 + Per-ETF 快取系統**
    * **問題描述**：左欄 ETF 排行（美股前20/台股前20，依 AUM 篩選後按 YTD 排序）、中欄持股明細含資訊更新日、右欄歷史買賣紀錄；全數資料必須每日動態刷新且不得寫死。
    * **root cause**：原版多項錯誤：(1) AUM 使用不存在的 `fast_info.total_assets` 而非正確的 `ticker.info["totalAssets"]`；(2) `fetch_etf_holdings` 欄位對應錯誤（`holdingName`/`holdingPercent` 不存在，正確是 `"Name"` 和 `"Holding Percent"` 且 index 為 ticker）；(3) 缺乏 per-ETF 快取機制（與兩週清除）；(4) 中欄缺少 ETF 名稱標頭；(5) 歷史買賣對多數 ETF 無資料（只有少數 ARK ETF 有舊快取）。
    * **solution**：1. **AUM 修復**：將 `_fetch_aum` 改為讀取 `ticker.info["totalAssets"]`，支援台股 ETF 己超兆 TWD 的 AUM（新增 T 幣制格式）。 2. **持股欄位修復**：`fetch_etf_holdings` 改為使用 `idx`（index）作為 symbol、`row["Name"]`、`row["Holding Percent"] × 100`，並同步回傳 ETF `name`。 3. **Per-ETF 快取架構**：`storage.py` 新增 `get_etf_cache_dir()`、`load_etf_symbol_cache(sym)`、`save_etf_symbol_cache(sym, data)`、`etf_symbol_cache_fresh(sym)` + 全局 AUM/perf 快取 `_aum_perf.json`；兩週自動清除 (`cleanup_old_etf_caches(14)`) 。 4. **中欄標頭**：選取 ETF 時動態更新 `#etf-holdings-title` 為 `{symbol} {fund_name} 當下持股細節`，`#etf-history-title` 同步。 5. **歷史買賣**：紺持 `history` 在 per-ETF JSON 內，如無則顯示明確說明（需由外部 scraper 寫入），不再顯示空白。 6. **寫入時機**：`run_background_fetch` 先檢查全局快取新骮度，再對各 ETF 分別檢查 `etf_symbol_cache_fresh()`；唯未更新者才發起 API 呼叫，奇楟先顯示快取資料。
    * **fixed by**：v0.0.3-dev

4. [open] [bug#00061] [newfeature] **Dashboard 首頁新增兩張「交易策略建議」卡片：ETF趨勢結論（多數性+規模性）與期權觀察清單（建倉/價格波動偵測）**
    * **問題描述**：使用者最終目的是在 Summary Dashboard 首頁顯示交易策略建議，要求兩個對話框：(1) 來自主動式ETF頁面的統整結論，需觀察「多數性」（數個ETF同時間區間買入賣出）與「規模性」（單一或多數ETF規模大量市值部位）兩種訊號，給出結論，且此結論也要放在ETF頁面內；(2) 根據使用者實際持有的部位標的，建立相同標訂的期權觀察清單，需具備追蹤功能，每日追蹤大量買權/賣權建倉或大量期權價格漲跌，列出結論並告知使用者。經確認需求細節：選擇權追蹤範圍限定「28-60天到期」合約（使用者明確指定，理由是太近到期的合約價格波動過大不具代表性），結論粒度先做個股層級（不做類股/sector層級，避免額外開發 sector 抓取工程）。**資料現況**：跟 bug#00060 相同的根本限制——yfinance 的 `option_chain()` 只回傳單一時間點即時快照，無歷史序列，選擇權觀察清單同樣必須從「現在開始」真實逐日累積快照才能偵測建倉/價格波動，不能回填或捏造。
    * **root cause**：(1) ETF 結論先前只有「多數性」（跨ETF共識）維度（bug#00060），未涵蓋使用者要求的「規模性」（單一大額基金即使只有 1 檔持有也該被看見）；且結論僅存在於 ETF 頁面內部，未曝露於 Dashboard 首頁。(2) 系統完全沒有選擇權相關的資料收集、儲存或分析機制；yfinance 雖然透過 `Ticker.option_chain(date)`／`Ticker.options` 提供真實的當前選擇權鏈（contractSymbol/strike/lastPrice/volume/openInterest/impliedVolatility 等真實欄位），但同樣不提供歷史序列，且完整鏈條動辄數十到數百張合約 × 使用者可能持有十幾檔標的，若不限縮範圍會產生與 bug#00058 相同等級的 yfinance 請求量／rate limit 風險。
    * **solution**：**(A) ETF結論擴充（規模性 + Dashboard 卡片）**：1. `analysis.py` `compute_symbol_trends()` 新增 `value_delta`（直接以真實 AUM×權重計算的美元金額差，不透過股價猜測，比既有的 `est_total_share_delta` 更可靠）；新增 `rank_scale_events()`——刻意不套用「至少2檔ETF」門檻，讓單一基金的大額真實部位變動（如稽核測試中 500 億美元 AUM 基金單獨大幅加碼 TSLA）也能被看見，門檻採「絕對金額 ≥$5M」與「相對該基金AUM ≥0.5%」雙重條件（避免小基金雜訊）。2. 新增 `generate_etf_conclusions()`，統一輸出多數性＋規模性 bullets，供 ETF 頁面與 Dashboard 卡片共用同一份文字（保證兩處一致）。3. `AdvancedAnalysisScreen` 新增「📝 結論」區塊。4. `DashboardScreen` 新增 `#etf-conclusions-panel`（於 `#strategy-panels` 新列），`_build_etf_conclusions_panel()` 離線讀取本機快照即時運算，資料不足時誠實顯示收集進度。**(B) 期權觀察清單（全新子系統）**：1. `storage.py` 新增 `options_cache/history/{underlying}.jsonl` 真實逐日快照儲存（`append_options_daily_snapshot`／`load_options_daily_snapshots`／`prune_options_history`／`options_symbol_fresh`），架構完全比照 ETF 快照，日期去重、獨立資料夾不受其他清理影響。2. `quotes.py` 新增 `fetch_options_snapshot(underlying, min_dte=28, max_dte=60, strike_band_pct=15.0)`：只抓到期日落在 28-60 天、履約價落在現股價 ±15% 的合約（皆為使用者確認之範圍決策），欄位皆為 yfinance 真實回傳值，無估算。3. 新檔 `assettrack/options_analysis.py`：`compute_options_flow()` 以「合約代碼完全比對」（strike/expiry/type 已內含於 contractSymbol，非用相近值猜測）比較視窗內最早/最新真實快照，只比對兩邊都存在的合約（到期滾出或新進的合約不強行比較，避免偽造變化）；未平倉量變化達「≥200口」或「≥50%」判定為建倉，價格變化達「≥20%」判定為大幅波動；並彙總每檔標的買權/賣權建倉量比例做多空傾向判斷。視窗刻意設為 14 天（比 ETF 的 60 天短很多）——因為被追蹤的合約本身會隨時間自然到期或滾出 28-60 天視窗，視窗太長會導致早期快照與最新快照幾乎沒有可比對的相同合約， 14 天大致能保留足夠重疊率。`generate_options_conclusions()` 產生中文結論 bullets。4. `tui.py` 新增 `OptionsWatchlistScreen`（架構比照 `ActiveETFsScreen`）：觀察清單標的來自 `_underlyings_from_positions()`——即時掃描使用者真實持倉（option 類型取 `underlying`、stock/etf 類型取 `symbol`），非憑空指定；背景 worker 逐一 fetch 並即時寫入真實快照；畫面顯示「結論」區塊＋標的清單（含多空偏向）＋選定標的的合約明細事件表。`DashboardScreen` 新增鍵盤快捷鍵 `8` 與側邊選單「🎯 期權觀察清單」開啟此畫面。5. `DashboardScreen` 新增 `#options-flow-panel`（`#strategy-panels` 第二欄），`_build_options_flow_panel()` 同樣離線運算、與觀察清單頁面共用同一份結論輸出、資料不足時誠實顯示收集進度。已驗證：`py_compile` 全部檔案（含新增的 `options_analysis.py`）通過；以合成資料測試 `rank_scale_events()` 正確捕捉「單一大額基金」規模性訊號（未達多數性門檻但規模達標）；以合成資料測試 `compute_options_flow()` 正確判定建倉/價格波動門檻、正確排除「到期滾出」與「新進合約」的偽比較、正確彙總買權/賣權建倉比例；以 mock `yf.Ticker` 驗證 `fetch_options_snapshot()` 的 28-60 天到期與 ±15% 履約價過濾邏輯精確無誤；以 Textual `App.run_test()` 無頭模式完整掛載 `OptionsWatchlistScreen` 與 `DashboardScreen`（含兩張新卡片）皆無崩潰，過程中抓到並修正一個遺漏定義的 `storage.options_symbol_fresh()` 函式（headless 執行時才會觸發 the runtime ImportError，`py_compile` 無法偵測）；以使用者本機真實持倉資料驗證兩張 Dashboard 卡片在 0 天真實資料時正確顯示誠實的「資料收集中」狀態而非假結論。
    * **fixed by**：v0.0.11-dev（待使用者驗證：需在有網路環境下實際執行 TUI，持續使用數天讓 ETF 與期權快照真實累積，觀察 Dashboard 首頁兩張卡片、「主動式ETF排行→進階分析」的結論區塊、以及新的「期權觀察清單」畫面（鍵盤 `8`）是否如預期運作；請特別留意期權部分因追蹤合約會隨到期日滾動，資料收集進度可能會比 ETF 部分更常在「已就緒」與「未就緒」間變動，屬預期行為）。**微調（同日）**：使用者要求檢視兩張卡片門檻的可參考性，經檢視發現四項問題並依使用者指示調整（其中「多數性」樣本數門檻使用者明確指定為 4）：(1) `analysis.py` `rank_symbol_trends()`／`generate_etf_conclusions()` 的 `min_etfs_evaluated` 由 2 改為 4——原本 2 檔ETF中僅 1 檔加碼、另 1 檔持平即可達到 50% 一致門檻，被誤判為跨基金「多數性」共識，實際上只是單一基金的動作；(2) `compute_symbol_trends()` 的 `flat_threshold_pp` 由 0.3 改為 0.5 個百分點，降低對資料源本身四捨五入/更新雜訊的敏感度；(3) `options_analysis.py` `compute_options_flow()` 的 `oi_buildup_min_contracts` 由 200 改為 500——200口對高流動性標的（如AAPL/SPY類）14天內屬常見雜訊，不足以代表「大量建倉」；(4) 新增 `price_swing_min_abs`（預設 $0.15）並改為與 `price_swing_min_pct` 需同時滿足（AND，非 OR）——修正原本低價合約（如 $0.10→$0.12）僅因相對漲跌 20% 就被判定「大幅波動」、但金額波動微不足道的問題。四項调整皆為函式預設值變更，`tui.py` 呼叫端未覆寫參數，自動套用到 Dashboard 兩張卡片與「進階分析」／「期權觀察清單」完整頁面，兩處文字保證一致。已以合成資料驗證：2檔ETF/1檔動作案例不再產生多數性結論；4檔ETF/3檔動作案例正確產生多數性結論；0.4pp權重變化正確歸類為 flat；低價合約 $0.10→$0.12（20%漲幅、$0.02絕對值）不再觸發價格波動事件；真實案例 $1.00→$1.30（30%漲幅、$0.30絕對值）正確觸發。`py_compile` 通過。
    * **再次微調（同日，代表性審查）**：使用者以「能否提供有效投資建議」為標準再次審查，發現一個更根本的方法論問題：`compute_symbol_trends()` 的方向判定（多數性/規模性皆共用）先前只看「持股權重變化」，無法區分「真實增減倉」與「股價漲跌造成的被動權重變化」——例如某股票60天內大漲30%但ETF完全沒交易，其在基金中的權重與市值占比仍會自然上升，會被誤判為「加碼」。同時發現 `quotes.estimate_shares()` 先前用寫死的假設均價（美股$100、台股$150）反推股數，對股價遠離假設值的標的（如NVDA ~$140）估計股數可能有數倍誤差，「進階分析」頁面表格直接顯示此數字給使用者。經使用者明確決策：(1) 方向判定改為「真實股數變化」與「AUM佔比(權重)變化」兩訊號需同向才判定為增/減持，任一缺席或不同向一律歸類 flat，不計入多數性/規模性統計；(2) 股數估計改用真實股價換算，移除固定假設均價後備方案。**實作**：`quotes.py` `estimate_shares()` 新增必要的 `price` 參數（真實現價），不再有固定均價後備，缺真實價格時回傳 None；新增 `fetch_prices_batch()`，用與 `fetch_active_etf_performance()` 相同的分批 `yf.download()` 手法，一次批次抓取「本次刷新到的所有ETF前十大持股」聯集的真實現價（利用主動式ETF持股高度重疊、去重後遠小於84檔×10檔的特性，控制請求量避免重演bug#00058的rate limit風險）。`tui.py` `run_background_fetch()` 重構為兩階段：先平行抓取所有ETF的持股/AUM/績效，再對聯集持股批次抓真實現價，最後才把 price/shares 寫回每筆持股並呼叫 `append_etf_daily_snapshot()`。`storage.py` `append_etf_daily_snapshot()` 的持股快照新增 `price` 欄位（無真實價格時為 None，不捏造）。`analysis.py` `compute_symbol_trends()` 用每筆快照各自記錄的真實價格反推真實股數（`shares1 - shares0`），與權重方向兩訊號需一致才判定 up/down，否則一律 flat；`rank_scale_events()`／`generate_etf_conclusions()` 沿用同一個 `direction` 欄位，自動套用此修正，無需另外改動。**已驗證**（合成資料）：案例A模擬「ETF持股股數完全不變、股價從$100漲到$130、AUM與權重隨之自然上升」，舊邏輯會判定為多數性加碼，新邏輯正確判定為 flat、不產生結論；案例B模擬「真實加碼（股數100,000→150,000）且權重同步上升」，正確產生多數性結論；案例C模擬「快照缺乏真實價格欄位（例如此功能上線前已累積的舊資料）」，正確因缺乏真實股數訊號而歸類 flat，不猜測。`py_compile`、完整模組匯入、`AssetTrackApp.run_test()` 無頭掛載測試皆通過。**已知取捨**：此修正上線後，此功能上線前已累積的舊快照沒有 `price` 欄位，短期內同一組(ETF,持股)在「舊快照↔新快照」比較時會因為早期那一端缺乏真實價格而被歸類 flat（等同重新從零累積），需要幾天讓新格式快照佔滿60天視窗後，多數性/規模性訊號才會恢復正常出現頻率——這是為了避免用假數據填補缺口所做的刻意選擇，而非bug。

5. [open] [bug#00062] [function] **部位加碼合併時，空單或反向未平倉的平均成本未更新（僅 new_qty > 0 才計算）**
    * **問題描述**：TUI 部位調整以「新增部位」加碼既有同 broker/account/symbol/類型部位時，若合併後淨部位為空單（`new_qty <= 0`），平均成本完全不更新，維持第一筆的舊成本；空單加碼後成本永遠偏差，連帶未實現損益金額/百分比失真（選擇權因乘數放大更明顯）。多單加碼雖正確，但公式用帶號數量，遇反向操作亦不嚴謹。
    * **root cause**：`DashboardScreen._handle_add_position_result()` 合併分支僅在 `if new_qty > 0`時做加權平均，且以帶正負號的 `quantity` 當權重，未涵蓋空單（負量）同向加碼與反向（部分平倉／翻倉）情境。
    * **solution**：改以「方向判斷 + 絕對數量加權」：同方向加碼一律 `(cost0*|q0| + cost1*|q1|)/|q_new|`（多空皆適用）；反向且已翻倉改採新進場成本；反向未翻倉（部分平倉）保留原方向平均成本。已以單元測試驗證 short+short(-10@190,-10@200→-20@195)、long+long、部分平倉、翻倉四情境皆正確。選擇權新增/OCC 代碼自動生成、乘數（US=100 / TW=50）與市值計算亦一併以測試確認無誤。
    * **fixed by**：v0.0.7-dev（待使用者驗證）

6. [open] [bug#00063] [UI] **「近期重大事件」頁面仍顯示已過去的月份事件（如六月），未以當月置頂**
    * **問題描述**：使用者進入重大事件頁面，仍看到已過去（如六月）的財報/總經事件；使用者要求置頂應為「當月」狀況，過去事件不再顯示。
    * **root cause**：`UpcomingEventsScreen.run_calendar_fetch()` 與 `DashboardScreen._fetch_upcoming_events_worker()` 皆以 `start_date = today - timedelta(days=30)` 及 `get_upcoming_macro_events(..., start_days_ago=30)` 回溯 30 天，刻意納入過去一個月的事件。
    * **solution**：兩處視窗起點改為 `start_date = today`、`start_days_ago=0`，只保留今日(含)以後的事件；月份分組升冪排序後，最早的一組即為當月，自然置頂。財報與總經事件皆套用同一過濾。
    * **fixed by**：v0.0.7-dev（待使用者驗證）

7. [open] [bug#00064] [function] **依使用者需求完全移除「歷史績效」(PerformanceHistoryScreen) 功能並重編號快速鍵**
    * **問題描述**：使用者指示「歷史績效」項目全部移除。
    * **root cause**：`PerformanceHistoryScreen`（~344 行）、快速鍵 `4`、側欄選項 `history`、`on_option_list_option_selected` 的 `history` 分支、`action_performance_history()` 與 `scripts/verify_tui.py` 的對應測試/import 皆仍存在。
    * **solution**：移除上述全部程式碼；數字快速鍵改為連續 1–7（4=近期重大事件、5=儲存快照、6=主動式ETF、7=期權觀察清單），同步更新首頁卡片「按 N」提示文字、action docstring、`scripts/verify_tui.py`（刪除 `verify_performance_history_screen`、修正按鍵、移除 import/stub）與 `README.md`（鍵表、Mermaid 圖節點、功能說明、CLI 殘留描述）。`assettrack/tui.py` 語法檢查通過、`scripts/verify_tui.py` 12 項中 11 項通過（唯一失敗為沙盒檔案權限限制，非程式碼問題）。備註：`shared.draw_history_chart()` 現已無 TUI 呼叫點（僅 verify 匯入），為避免非需求範圍改動暫予保留，待使用者決定是否一併移除。
    * **fixed by**：v0.0.7-dev（待使用者驗證）

8. [open] [bug#00065] [review] **ETF／期權觀察清單結論僅「描述現象」，未連結使用者實際部位方向，對 summary page 的「建設性投資建議」目標仍存在盲點**
    * **問題描述**：使用者指出觀察清單判斷邏輯的目標是於 summary page 提供「建設性投資建議」，要求審查是否仍有盲點。審查 `analysis.generate_etf_conclusions` 與 `options_analysis.generate_options_conclusions` 後確認：兩者輸出皆為純描述（「N 檔 ETF 同步增碼 X」「未平倉量 +500 口、價格 +30%」「偏多/偏空」），未提出 any 行動建議，也未與使用者自身持倉／多空方向交叉比對。
    * **root cause**：盲點如下：(1) **零部位感知**——skew（買權堆疊=偏多／賣權堆疊=偏空）已算出卻從未與使用者對該標的的多空立場比對，例如使用者持有多頭買權而機構賣權建倉升溫時，系統不會提示方向衝突；(2) **只描述不建議**——結論句缺少「因此可考慮…」的建設性語句；(3) **未平倉建倉方向具歧義**——OI 增加無法區分開倉/平倉、買方/賣方，直接標記 call-OI 堆疊為「多方增溫」可能誤導（亦可能是賣方賣出買權=偏空）；(4) **僅涵蓋已持有標的**——無法對尚未持有但值得進場的名稱提出觀察（雖為 bug#00061 設計，但仍是覆蓋盲點）；(5) 28–60 DTE ±15% 視窗於低流動性標的易致比對合約過少而結論空白。
    * **solution**：經使用者確認後實作（部位感知建設性建議）：(1) `shared.py` 新增 `position_stance_by_symbol(positions)`，依持倉判斷各標的淨多空立場（股票/ETF 依數量正負；選擇權 long call/short put→看多、long put/short call→看空；同標的多筆累加，得「多/空/混合」）。(2) `generate_options_conclusions()` 與 `generate_etf_conclusions()` 新增選填 `positions` 參數：對每個 skew／共識標的，與使用者立場方向交叉比對後附上建設性語句——「與你目前偏X的部位方向一致」／「⚠️ 與你目前偏X的部位方向相反，留意反向風險/是否調節」／「你尚未持有，可留意是否符合進場條件」。(3) 降低方向誤導：期權 skew 用語由「可能反映多方部位增溫」的斷言改為中性「資金偏多/偏空關注」，並附註「未平倉增減無法區分開/平倉，方向僅供參考」。(4) 呼叫端：Dashboard 兩張 summary 卡片（`_build_etf_conclusions_panel`、`_build_options_flow_panel`）與 `OptionsWatchlistScreen._run_analysis` 均傳入實際持倉；未傳入時（如 `AdvancedAnalysisScreen`，其上層 `ActiveETFsScreen` 未帶持倉）維持原純描述行為，向後相容。已以單元測試驗證多/空/混合/未持有四情境（含股票、選擇權 long put、short 股票）之對齊與衝突提示皆正確，`verify_tui.py` 11/12 通過（唯一失敗為沙盒檔案權限）。備註盲點 (4)（僅涵蓋已持有標的）與 (5)（低流動性視窗合約過少）維持現況，屬 bug#00061 設計範圍，如需擴充另立需求。
    * **fixed by**：v0.0.7-dev（待使用者驗證）

---

## ✅ 已關閉與驗證項目 (Closed Items)

1. [cancel] [bug#00001] [function] **IBKR API 連線逾時與 Client ID 衝突處理**
   * **問題描述**：當多個 Client 連接同一個 TWS/Gateway 時，預設 Client ID 衝突會導致連線失敗且無明確警告。
   * **root cause**：
   * **solution**：
   * **fixed by**：

2. [cancel] [bug#00002] [function] **Firstrade CSV 匯入欄位變動相容性**
   * **問題描述**：若 Firstrade 匯出的 CSV 標頭欄位順序或名稱微調，會導致 `parse_positions_csv` 發生 KeyError。
   * **root cause**：
   * **solution**：
   * **fixed by**：

3. [cancel] [bug#00003] [UI] **ASCII 歷史淨值折線圖在窄版終端機的標籤折行**
   * **問題描述**：當終端機寬度小於 80 字元時，`history` 繪製的折線圖 Y 軸價格與 X 軸日期標籤容易錯位或折行。
   * **root cause**：
   * **solution**：
   * **fixed by**：

4. [cancel] [bug#00004] [newfeature] **基準貨幣 TWD ⇄ USD 切換支援即時匯率自動緩存**
   * **問題描述**：每次執行 `value --refresh` 都要向 Yahoo Finance 請求 `USDTWD=X` 匯率，於無網路或 API 限制時會出錯。
   * **root cause**：
   * **solution**：
   * **fixed by**：

5. [closed] [bug#00005] [newfeature] **互動式登入與自動循環刷新之即時報價 CLI 介面 (市場開市狀態顯示)**
   * **問題描述**：CLI 啟動時應要求使用者輸入帳號登入，認證後調閱持倉並進入每分鐘自動更新報價的循環，不可直接 return，並動態判斷顯示各部位之「開市/未開市」狀態。
   * **root cause**：舊有設計為一次性執行輸出靜態歡迎面板，無法持續即時觀看及自動判斷各地交易所之開關市狀態。
   * **solution**：在 main 入口處加入互動式登入引導，認證後進入無限 while 循環配合 `console.clear()` 每分鐘重新下載最新行情，並以 `zoneinfo` 在台北與紐約時區判斷美股與台股交易所的開關市狀態。
   * **fixed by**：v0.0.1-dev

6. [closed] [bug#00006] [newfeature] **新使用者無持倉之引導精靈與功能選單**
   * **問題描述**：新使用者或持倉空白帳戶登入時，系統缺乏明確的下一步引導（如初始化、手動新增、CSV 導入或範例部位）。應實作互動選單引導。
   * **root cause**：1. 新使用者登入時因無持倉，介面無清晰指引。2. 新增選項選單中使用 `ctx.invoke` 呼叫其他指令時，未提供 `ctx` 實體導致拋出 TypeError。
   * **solution**：1. 實作引導選單與功能選項。2. 將 `ctx.invoke` 替換為直接調用 Python 函數（如 `init_setup(ctx)` 及 `add(ctx, broker="manual")`）避開 parameter check 錯誤。
   * **fixed by**：v0.0.1-dev

7. [closed] [bug#00007] [newfeature] **Keychain 安全憑證儲存與 Touch ID 生物辨識雙因子登入機制**
   * **問題描述**：系統應安全儲存使用者密碼，並於 macOS 環境中支援 Touch ID 生物辨識驗證，指紋驗證失敗或不支援時，無縫降級回鑰匙圈密碼驗證。
   * **root cause**：原系統無使用者身份認證，亦無憑證保存與生物特徵辨識機制。
   * **solution**：以 `keyring` 將使用者密碼儲存於系統 Keychain 中；並使用 Swift 編譯獨立的 macOS Touch ID 驗證輔助程式，於登入時自動執行指紋檢測，失敗或取消時降級回 3 次密碼輸入限制。
   * **fixed by**：v0.0.1-dev

8. [closed] [bug#00008] [newfeature] **即時監控看板非阻塞式互動選單與子指令整合**
   * **問題描述**：即時更新看板會阻塞使用者輸入，使用者無法直接在畫面中執行 any 操作。應實作非阻塞式輸入，允許使用者在看板中直接新增倉位、縮減倉位/登錄交易，且不中斷自動定時刷新報價的行為。
   * **root cause**：1. 看板以 `time.sleep` 阻塞等待，無法接收鍵盤輸入。2. 在循環中直接呼叫 Typer 子指令時，預設 `OptionInfo` 會引起屬性錯誤；且 `ctx.obj` 未動態綁定為登入的使用者，導致寫入預設資料庫/配置檔案。
   * **solution**：1. 使用 Unix/macOS `select.select` 實作非阻塞式 Stdin 輸入輪詢（超時 60 秒），若無輸入則自動更新報價。2. 整合子指令面板，以 `try...except (typer.Exit, Exception)` 隔離子指令退出，並動態綁定 `ctx.obj = user` 與明確處理預設參數以避開 `OptionInfo` 錯誤。
   * **fixed by**：v0.0.1-dev

9. [closed] [bug#00009] [function] **CLI 入口模組原始碼損毀及修復**
   * **問題描述**：`cli.py` 原始碼於系統執行過程中損毀並遭清空（0 位元組），導致 `ImportError: cannot import name 'app' from 'assettrack.cli'` 無法執行 CLI。
   * **root cause**：先前系統檔案寫入異常或中斷，造成核心 `cli.py` 檔案內容遺失。
   * **solution**：從歷史對話 logs 中提取並完整重建 `cli.py` 內容，包含 Keychain/Touch ID 登入流程、非阻塞式輪詢選單與各 CLI 指令，並進行編譯與測試驗證通過。
   * **fixed by**：v0.0.1-dev

10. [closed] [bug#00010] [function] **選擇權持倉參數、自訂券商帳戶與持倉修改功能之優化**
    * **問題描述**：手動新增選擇權持倉功能未完善，需正確輸入到期日與行權價等明細以防 Yahoo Finance 查詢 404 及錯誤警告；此外缺乏修改現有持倉、選取/輸入特定券商子帳戶 (如 FT, IBKR) 等功能。
    * **root cause**：1. 選擇權部分欄位可能在合併、導入或交易登錄時遺失或未正確寫入；當 yfinance 查詢無報價的選擇權合約時，會在終端機輸出 HTTP 404 等垃圾資訊。2. CLI 僅允許新增與登錄交易，無修改介面。3. CLI 新增與修改時未提供設置/寫入 `account` 欄位的引導，且唯一性判定未考量 account 造成同代碼不同券商之覆蓋。
    * **solution**：1. 於 `Position` 加入 Pydantic validator，若符合 OCC 標準格式則自動解析並補全期權明細；同時在 `quotes.py` 實作 stderr/stdout 重導向與 override yfinance logger，阻斷查詢報價 404 等雜訊。2. 於 CLI menu 提供 Option `2`-修改持倉功能，允許修改數量、成本、貨幣、帳戶及期權資訊，且編輯後支援重疊持倉自動合併。3. 於 CLI 及 Streamlit 手動增刪查改中導入券商 `account` (如 FT, IBKR) 選取/填寫，並改以 `(broker, account, symbol)` 為唯一鍵進行重複判定與合併。
    * **fixed by**：v0.0.1-dev

11. [closed] [bug#00011] [newfeature] **工具定位重構：移除下單/交易邏輯，補全持倉參數欄位**
    * **問題描述**：1. 功能選單中「登錄交易(減持/平倉)」是下單軟體邏輯，與資產管理工具定位不符。2. 新增/修改持倉缺少 market、exchange、cost_currency、multiplier aquarium、sector、notes 等完整欄位，stock 與 option 相關欄位無法完整維護。
    * **root cause**：1. 早期設計未明確區分「資產管理工具」與「交易執行軟體」，導致 log_trade 功能混入選單。2. Position model 欄位設計偏向最小可行，未考量多市場（US/TW/HK）、多幣別成本、合約乘數、分類標籤等實際需求。
    * **solution**：1. 移除 CLI 選單 option 3 的 `log_trade` 呼叫，改為 `remove_position`（直接刪除持倉），並新增獨立的 `assettrack remove` 指令。2. `Position` model 新增 6 個 Optional 欄位：`market`、`exchange`、`cost_currency`、`multiplier`、`sector`、`notes`。3. `_interactive_add_one` 完整重寫，涵蓋所有欄位引導（含台股市場自動後綴、非美股選擇權手動代碼輸入）。4. `edit` 重構為全欄位逐一確認模式（Enter 保留原值）。5. ~~`dashboard.py` 相關修改~~（已隨 Streamlit UI 完全移除）。
    * **fixed by**：v0.0.1-dev

12. [closed] [bug#00012] [function] **移除 Streamlit Web UI（dashboard.py），確保工具定位為純 CLI**
    * **問題描述**：AssetTrack 定位為純 CLI 工具，但 `dashboard.py` 為 Streamlit Web UI（含 HTML5/CSS Glassmorphism、Plotly 圖表、瀏覽器 file uploader），與 CLI-only 架構矛盾；`pyproject.toml` 的 `[ui]` optional deps（streamlit, plotly）亦需移除。
    * **root cause**：早期設計同時維護 Streamlit Web UI 與 CLI，後明確決策以純 CLI 來主，但 dashboard.py 及其依賴未同步清除。
    * **solution**：1. 刪除 `dashboard.py`。2. 從 `pyproject.toml` 移除 `[project.optional-dependencies] ui` 群組（streamlit>=1.35, plotly>=5.20）。3. 確認 `cli.py` 完全以 `typer` + `rich` 實作，無 any HTML5 引用。
    * **fixed by**：v0.0.1-dev

13. [closed] [bug#00013] [newfeature] **投資組合 Beta 權重與市場敏感度分析 (Portfolio Beta-Weighting & Analytics)**
    * **問題描述**：目前系統缺乏量化投資組合風險的指標。應實作計算個股/選擇權相對於基準大盤（如 SPY 或 0050）的 Beta 值，並顯示於 CLI dashboard 表頭中。同時可支援歷史最大回撤 (Max Drawdown) 與風險價值 (VaR) 計算。
    * **root cause**：`quotes.py` 無 beta 抓取函數；`cli.py` dashboard 表頭指標列無 Portfolio Beta 欄位。
    * **solution**：1. 在 `quotes.py` 新增 `fetch_beta()`，以 `yfinance ticker.info["beta"]` 取得個股 beta（選擇權自動以 underlying 查詢）。2. 在 `cli.py` `render_dashboard_once` 中以每個持倉的 USD 市值為權重，計算 Weighted Portfolio Beta。3. 在指標欄新增第五格「⚡ Portfolio Beta」Panel，顏色依風險等級變色（≤0.8 綠色、≤1.2 黃色、>1.2 紅色）。
    * **fixed by**：v0.0.1-dev

14. [closed] [bug#00016] [newfeature] **大盤指數基準對比與時間/資金加權報酬率計算 (Portfolio Benchmarking & TWR/IRR Performance)**
    * **問題描述**：歷史淨值折線圖僅顯示自身絕對淨值，缺乏與大盤指數（如 SPY, QQQ, ^GSPC）的相對績效對比；且原設計依賴 SQLite 快照才能運作，使用者需先手動存檔才有圖可看。
    * **root cause**：1. `history` 指令原以快照（Snapshot）資料為數據源，要求使用者必須累積快照才能使用。2. 缺乏嚴苛的前置條件驗證（options 無歷史市價、空持倉、無網路等邊界情境均未處理）。3. 圖表為單純折線圖，無法直觀呈現部位結構與券商比例。
    * **solution**：v2 完整重設計。1. 改為「當下持倉部位 × 歷史股價」回推（Position-based Backtest），不再依賴快照，任何時候都能使用。2. 新增 `fetch_historical_prices_weekly()` 批次下載週頻價格（yf.download 批次拉取）。3. 加入嚴格前置條件：排除 Options（yfinance 無法取得歷史定價）、排除非 USD 持倉、驗證網路下載成功、至少 2 個有效週節點。4. 新增互動選單：期間固定為 60d/180d/YTD，基準選 SPY/QQQ/^GSPC/停用。5. 新增 `draw_history_chart()`：直方圖（`█▓▒░` 按券商分層）+ 折線（`○─` benchmark），X 軸每週切分、Y 軸 USD 市值。6. 新增 `get_upcoming_macro_events()`：hardcoded 2025-2026 FED/NFP/CPI 日程，顯示未來 90 天內事件清單。7. 績效摘要顯示組合回報 / Benchmark 回報 / Alpha / 期間高低點。
    * **fixed by**：v0.0.1-dev

15. [closed] [bug#00017] [newfeature] **互動式全螢幕終端面板與本機/Webhook 警報系統 (Rich TUI Dashboard & Price Alerting)**
     * **問題描述**：目前的 CLI dashboard 為定時 `console.clear()` 刷新，易產生閃爍且無選單分頁。需要為 CLI 引入選單、清單與欄位修改時的鍵盤/游標（上下左右方向鍵）選擇體驗。目前考量以下三種技術解決方案（尚未決定）：
       * **方案一：使用 `questionary` 庫**（推薦，輕量且侵入性最小）：基於 `prompt_toolkit` 封裝，可直接將現有的 `Prompt.ask` 替換為支援方向鍵與 Enter 選擇的互動式清單，極易與現有的 `Rich` 終端輸出整合。
       * **方案二：使用 `prompt_toolkit`**（控管度最高）：可實現高度自訂的鍵盤事件監聽、自動補全與熱鍵綁定，但程式碼複雜度較高。
       * **方案三：使用 `Textual` 庫重構為全螢幕 TUI**（視覺效果與互動最豐富）：Rich 官方推出的全螢幕終端機 UI 框架，支援滑鼠、鍵盤焦點、多視窗分頁等，但需要將整體 CLI 重構為事件驅動架構。
     * **root cause**：
     * **solution**：
     * **fixed by**：v0.0.2-dev

16. [closed] [bug#00018] [UI] **歡迎畫面 Logo 拼寫錯誤與實體圖片 ASCII 轉換**
     * **問題描述**：CLI 啟動時時の ASCII Welcome Page 寫錯字為 `AssetTrak`（漏掉 `c`），且缺乏品牌感。應使用 Pillow 直接將官方圖片 Logo 轉換為精緻的 ASCII 鷹頭標誌，並修復文字拼寫為 `AssetTrack`。
     * **root cause**：1. 舊有 ASCII Art 手工拼寫錯誤。2. 未能整合圖片設計。
     * **solution**：1. 實作獨立的 Pillow 預處理與自適應像素轉換，將 `assettrack_logo.png` 的鷹頭 Logo 部位以 row gap 完美裁切，在 threshold=235 條件下渲染成無雜點的 ASCII 圖示。2. 修復下方 `AssetTrack` 拼字並以 Slant ASCII 樣式展示。
     * **fixed by**：v0.0.1-dev

17. [closed] [bug#00019] [function] **歷史週頻市值 NaN 傳播與單一基準指數解析失效**
     * **問題描述**：1. 當持倉包含近期上市或歷史不全的標的（如 SPCX）時，yfinance 回傳的 NaN 會傳播並破壞整個週期的總值計算，導致回溯大半週數被過濾只剩最近幾天。2. 基準指數（如 QQQ）等單一標的下載時，yfinance 返回 columns 為 MultiIndex 的 DataFrame，造成 `row["Close"]` 被誤解析為 pandas Series，觸發 TypeError 導致 QQQ 歷史資料為空，進而使大盤對比強制降級為「停用」。
     * **root cause**：1. 未對 `float("nan")` 進行過濾與防護。2. 單一 ticker 下載時 columns 同樣是 MultiIndex 形式，原程式碼未做對應判斷。
     * **solution**：1. 在 `fetch_historical_prices_weekly` 下載與 `history()` 計算中，引入 `math.isnan()` 對所有價格進行嚴格的實數與空值過濾，使無歷史價格的標的在該週市值中不作加總傳播。2. 統一單 ticker 與多 ticker 的 DataFrame 解析邏輯，對 `pd.MultiIndex` 的層級（Level 0 & Level 1）進行自適應的 close price 行提取，保證 QQQ/SPY 均能成功解析。
     * **fixed by**：v0.0.1-dev

18. [closed] [bug#00020] [newfeature] **純手動持倉管理與 API/CSV 匯入功能移除**
     * **問題描述**：目前系統中的 IBKR API 連線設定與 Firstrade CSV 匯入在使用時存在不便，需簡化工具定位，移除此二類 API 及 CSV 的相關連線設定與匯入指令，改為純手動部位管理並配合 Keychain/Touch ID 登入認證。
     * **root cause**：依賴外部連線與檔案結構容易因變動或環境問題造成異常，不利於快速輕量化資產記錄與隱私安全性。
     * **solution**：1. 自 CLI 移除 `import-csv`、`set-credential` 及 `clear-credential` 指令，並移除程式中對 `.brokers` CSV 解析器的 import。2. 更新儀表板 header 資訊，隱去 Keychain 串接狀態欄。3. 更新選單，將原本的「連線設定」選項移除，並重編號餘下功能為 1~7。4. 引導精靈在初始化時不再詢問 API/CSV，改為直接引導至手動新增持倉。
     * **fixed by**：v0.0.1-dev

19. [closed] [bug#00021] [newfeature] **功能選單重整與新增取消返回功能 (Action Menu Reorganization & Cancel Options)**
     * **問題描述**：原本的 7 個主選單選項需簡化，並將「新增持倉」與「移除持倉」整合至新選單「1-部位調整」的子選單中。同時，為了防止使用者選錯選項，每個互動式選項均需要有取消或返回主選單的機制。
     * **root cause**：選單項目過多使得介面擁擠，且缺乏在每個輸入詢問時退回主選單的取消機制。
     * **solution**：1. 將主選單簡化為 5 個選項，其中選項 1 改為「部位調整」；2. 點選「部位調整」後顯示「新增部位、修改部位、移除部位、返回主選單」的子選單；3. 在 `_prompt_broker_account` 與 `history` 選擇中新增 `q` 退出機制，利用 `typer.Exit` 拋出以退回主選單；4. 在安全登出與儲存快照前加入確認提示。
     * **fixed by**：v0.0.1-dev

20. [closed] [bug#00022] [newfeature] **Holdings 以券商分區塊顯示並新增今日漲跌欄位 (Broker-Grouped Holdings & Daily P&L Columns)**
     * **問題描述**：原本 Holdings 表格為全體打平排列，不易區分不同券商；且缺少每日持倉淨值變化（今日漲跌金額與百分比）的快速參考欄位。
     * **root cause**：1. `_build_positions_table` 為單一檔案表格，未按 broker 分組。2. `Position` model 缺少 `prev_close` 欄位，`quotes.py` 未拉取前日收盤價。
     * **solution**：1. `models.py` 新增 `prev_close: Optional[float]` 欄位，並新增 `daily_change`（部位今日淨值變動）與 `daily_change_pct`（個股漲跌幅）兩個 property。2. `quotes.py` 在 `enrich_positions_with_quotes` 中改直接操作 yfinance Ticker，從 `fast_info.previous_close` 取得前日收盤，快速失敗時退回 5d history 取倒數第二收盤作為 fallback。3. `cli.py` 新增 `_build_broker_holdings()` 函數：按 broker 分組、每組依 USD 等值市值由大至小排序、印出 Rule 分隔 Header（顯示券商名稱與小計）、表格新增「今日%」與「今日漲跌」欄位（正負以綠紅色標示），並取代原 dashboard 中對 `_build_positions_table` 的呼叫。
     * **fixed by**：v0.0.1-dev

21. [closed] [bug#00023] [UI] **TUI Sidebar 與 Modal 內功能選單 (OptionList) 無法用 Enter 確認選取**
     * **問題描述**：使用上下左右鍵移動至左側功能選單或彈出之操作選單時，按下 Enter 鍵毫無反應，無法觸發對應行為或確認選取的選項。
     * **root cause**：選單使用的 Textual `OptionList` 事件處理器名稱錯誤地宣告為 `on_option_list_selected(self, event: OptionList.Selected)`，但 Textual v0.80+ 的正確命名與事件型別應為 `on_option_list_option_selected(self, event: OptionList.OptionSelected)`，導致事件從未被派送處理。
     * **solution**：將 `tui.py` 中所有的 `on_option_list_selected` 修正為 `on_option_list_option_selected`，並更新型別註解為 `OptionList.OptionSelected`。同步在 `verify_tui.py` 中修改 `verify_logout_modal` 以按鍵模擬 (`down` + `enter`) 方式測試 OptionList 的選取功能。
     * **fixed by**：v0.0.2-dev

22. [closed] [bug#00024] [UI] **安全登出與部位刪除確認對話框支援方向左右鍵切換按鈕**
     * **問題描述**：安全登出與部位刪除確認畫面彈出時，使用者無法利用左右方向鍵在「確認」與「取消」按鈕之間進行焦點切換。
     * **root cause**：對話框 `LogoutConfirmModal` and `DeleteConfirmModal` 內部未捕獲方向鍵，且未為按鈕設定預設焦點。
     * **solution**：1. 在兩者 `on_mount` 方法中，將預設焦點設置於取消按鈕 (`#cancel`)，避免意外誤觸。2. 實作 `on_key` 方法，當捕獲到 `left` 或 `right` 鍵時，於 `#confirm` 與 `#cancel` 按鈕間輪流切換焦點。3. 在 `verify_tui.py` 更新 `verify_logout_modal` 來模擬方向左右鍵的焦點切換與確認選取。
     * **fixed by**：v0.0.2-dev

23. [closed] [bug#00026] [performance] **TUI 渲染效能優化與匯率與時區快照機制**
     * **問題描述**：TUI 每次自動刷新時都會清除並重建 DataTable 欄位，導致使用者的選取游標/滾動位置被重設為左上角；另外，每分鐘自動刷新均重複抓取網路匯率及頻繁執行時區物件初始化，產生不必要的 CPU 與網路負擔。
     * **root cause**：1. 每次刷新呼叫 `clear(columns=True)` 重建欄位而丟失游標座標與焦點。2. 缺乏對 `fetch_usdtwd_rate` 的快取，且 `_is_market_open` 每秒為持倉執行 `zoneinfo.ZoneInfo()` 重複載入時區資料。
     * **solution**：1. 在 `on_mount` 中僅載入一次欄位，刷新時改用 `clear(columns=False)`，並在 `_render_all` 前後保存並恢復游標座標與焦點。2. 實作 1 小時 TTL 的 `_get_cached_usdtwd_rate` 與模組級的 `_TZ_TW`/`_TZ_US` 靜態時區快取。
     * **fixed by**：v0.0.2-dev

24. [closed] [bug#00027] [performance] **TUI 登入後首次顯示資訊未載入最新報價且顯示負值損益**
     * **問題描述**：登入系統後首次進入主看板時，因為未立即觸發報價更新，會直接以無報價的部位資料渲染，導致總市值顯示為 $0 且所有未實現損益均計算並顯示為負值。
     * **root cause**：1. `DashboardScreen` 在 `on_mount` 時並未立即呼叫 `_do_refresh_worker` 背景執行緒以抓取最新行情與匯率，而是直接呼叫 `_render_all` 渲染，並等待 60 秒後的週期性更新。 2. 當報價為 `None` 時，`Position.unrealized_pnl` 沒有做空值判定，而是直接以價值 `0.0 - total_cost` 進行計算，造成損益值被誤判為巨大的負值。
     * **solution**：1. 在 `DashboardScreen.on_mount` 中，於初始化 60 秒定時刷新之餘，立即執行一次 `self._do_refresh_worker(load_from_disk=False)`。 2. 修改 `Position` model 的 `unrealized_pnl` 和 `unrealized_pnl_pct`，當 `market_price` 與 `market_value` 均為空值時，直接回傳 `None`。 3. 修改 TUI 看板與所有相關 Rich 元件的渲染 logic（如 `_build_metrics_panel`, `_build_broker_panel`, `_build_pnl_panel`, `_build_sector_panel` 以及 `_render_all` 內的 holdings table），若行情尚未載入，顯示 `⏳ 載入中...` 或 `—` 佔位符，待背景執行緒完成行情抓取後再更新完整畫面。
     * **fixed by**：v0.0.2-dev

25. [closed] [bug#00028] [newfeature] **結合持倉與 SOX 十大財報之總經重大日曆及主看板整合 (TUI/CLI)**
     * **問題描述**：使用者需要一個日曆功能整合個人持倉、SOX 十大成分股財報與重大總經數據公佈日。日曆需無縫整合至主儀表板：1) 於損益排行旁新增近期一個月事件之摘要面板；2) 左側選單將其排序於「歷史績效」之後並配置快捷鍵 `5`；3) 大日曆畫面改以 side-by-side 視覺化月曆網格與事件清單呈現；4) 重大事件 (FED/NFP/CPI) 須附帶預設時間並換算至本地 GMT+8 時區且相應調整行事曆日期。
     * **root cause**：原系統缺乏此整合，舊日曆為單一縱向 Table 列表且未標示事件時間；且每 60 秒刷新均重複抓取網路財報，引發 yfinance 頻率限制與效能卡頓。
     * **solution**：1. 實作 CLI `calendar` 指令，以併行 `ThreadPoolExecutor` 與移除重導向的執行緒 safe 模式同步財報與總經日程。2. 主看板整合：在 `#side-panels` 中以 `#recent-events-panel` 取代板塊分布，以簡化標籤顯示近期 30 天的前 8 項事件。3. 於 mount 時啟動背景 worker thread 異步抓取並快取於 `self._upcoming_events`，且常規報價刷新時跳過財報抓取；僅於部位異動 (增、改、刪) 時自動使快取失效並重新抓取。4. 重構 `UpcomingEventsScreen` 以 side-by-side 雙欄呈現：left 側為 Sunday-based 月曆網格 (依事件屬性以綠/黃/青反色標示日期相對位置)，右側為該月詳細事件。5. 重大總經事件 (FED 14:00 ET, NFP/CPI 08:30 ET) 透過 `zoneinfo` 在美東時區結合並轉譯為 GMT+8 本地時間 (如美東 14:00 會跨日轉換為本地次日 02:00 或 03:00，依 DST 狀態自動適配)，使行事曆顯示之日期與時間皆為本地正確時刻。6. 調整 sidebar 與快捷鍵 `5` 轉向重大事件，`6` 轉向快照，並於 `verify_tui.py` 通過所有 12 項測試。
     * **fixed by**：v0.0.2-dev

26. [closed] [bug#00029] [UI] **重大事件畫面新增持倉表格 (1/3) 與財報公佈時間 (GMT+8) 顯示 (TUI/CLI)**
     * **問題描述**：重大事件/財報公佈日曆未包含財報的具體發佈時間，且點進去重大事件項目後，使用者希望畫面的上 1/3 顯示目前的持有部位，下方的 2/3 預設顯示近三個月（包含過去一個月與未來三個月）的重大總經與財報事件。
     * **root cause**：原系統未拉取 `yfinance` 的個股財報發佈時間，僅顯示日期；且 `UpcomingEventsScreen` 大日曆畫面為滿版單一元件，未整合持有部位。
     * **solution**：1. 修改 `get_upcoming_macro_events` 支援 `start_days_ago` 參數以取得過去 30 天的事件。2. 修改 yfinance 併行 `fetch_cal` 邏輯，從 `ticker.info` 中抓取 `earningsTimestampStart` 並轉換至本地 GMT+8 時區。3. 更新 CLI `calendar` 指令與 TUI 背景 worker 同步發佈時間與 120 天區間（過去 30 天至未來 90 天）。4. 重構 `UpcomingEventsScreen` 的 layout，上 1/3 嵌入持有部位數據表 `DataTable`，下 2/3 嵌入月曆與事件詳情清單。5. 設定 `UpcomingEventsScreen` 預設聚焦於月曆事件面板 (`#events-right-panel`) 以便直接進行鍵盤上下鍵滾動，並為面板與持倉表格均新增 `:focus` 框線顏色提示。6. 將重大事件月曆表格標題設為向左對齊。7. 依據美東時間判斷財報公佈為「盤前」或「盤後」並加註於時間旁。8. 更新 TUI 主看板下方的簡易重大事件面板元件 (`_simplify_event_label`)，同步支援財報「盤前」或「盤後」時間註記。
     * **fixed by**：v0.0.2-dev

27. [closed] [bug#00030] [function] **PasswordModal 密碼以明文存於 self.correct_pwd**
     * **問題描述**：`PasswordModal(user, pwd)` 建構子接收從 keyring 取出的明文密碼並存為 `self.correct_pwd`，導致密碼在 Modal 整個生命週期內以字串形式留在記憶體中；`_submit()` 直接以 `val == self.correct_pwd` 比對，亦使明文出現於 call stack。
     * **root cause**：設計上為方便比對直接傳入並儲存明文 `pwd`，未考慮最小化敏感資料記憶體存留時間。
     * **solution**：移除 `PasswordModal.__init__` 的 `correct_pwd` 參數與 `self.correct_pwd` 屬性；同步移除 `run_touchid_auth(user, pwd)` 和 `_on_touchid_complete(success, user, pwd)` 呼叫鏈中的 `pwd` 傳遞；`_submit()` 改為即時呼叫 `keyring.get_password(KEYCHAIN_SERVICE, self.user)` 取得並比對後拋棄區域變數。
     * **fixed by**：v0.0.2-dev

28. [closed] [bug#00031] [function] **`service_name = "assettrack_user_auth"` 魔術字串三處散落**
     * **問題描述**：Keychain service name `"assettrack_user_auth"` 硬寫於 `cli.py` L55、`tui.py` L718、`tui.py` L934 三處，若需更改或已錯誤時需逐一修正，維護風險高。
     * **root cause**：缺乏統一常數定義。
     * **solution**：在 `storage.py` 新增模組級常數 `KEYCHAIN_SERVICE: str = "assettrack_user_auth"`（兩檔共同 import 點）；`cli.py` 與 `tui.py` 均改為 `from .storage import ..., KEYCHAIN_SERVICE`，三處硬寫全部移除；同時清除 `RegisterModal._submit` 中的死碼 `service_name = ...`。
     * **fixed by**：v0.0.2-dev

29. [closed] [bug#00032] [performance] **`fetch_cal` 財報抓取函式三處重複定義**
     * **問題描述**：完全相同的 yfinance 財報日期抓取邏輯（`fetch_cal(symbol)`，含 `earningsTimestampStart` 解析與 GMT+8 轉換）各自獨立定義於 `cli.py` L2401、`tui.py` `run_calendar_fetch` L2010、`tui.py` `_fetch_upcoming_events_worker` L2682，共 ~90 行重複代碼。
     * **root cause**：功能分散於 cli 與 tui 兩檔，未抽取至共享模組。
     * **solution**：新增 `fetch_earnings_calendar(symbols: list[str]) -> dict` 至 `quotes.py`；內部以 `ThreadPoolExecutor` 並行抓取，回傳 `{symbol: (dates_list, info_date, time_str, period_str)}`。三處行內函式 + ThreadPoolExecutor 全部移除，統一呼叫。
     * **fixed by**：v0.0.2-dev

30. [closed] [bug#00033] [performance] **Broker 分組排序邏輯四處重複**
     * **問題描述**：「按 broker 分組 → 組內依 USD 市值排序 → broker 間依總值排序」邏輯各自實現於 `tui.py` `_build_holdings_table`、`_render_all`、`UpcomingEventsScreen._render_holdings` 及 `cli.py` `_build_broker_holdings` 共 4 處。
     * **root cause**：缺乏抽象的排序工具函式。
     * **solution**：新增 `group_positions_by_broker(positions, rate) -> list[tuple[str, list[Position]]]` 至 `quotes.py`；四處 15~22 行之行內排序邏輯全部移除，替換為常數層呼叫。
     * **fixed by**：v0.0.2-dev

31. [closed] [bug#00034] [function] **`is_market_open` 在 cli.py 與 tui.py 各自獨立定義**
     * **問題描述**：相同的市場開市判斷邏輯（依 TWD/TW 後綴決定時區，判斷週末與交易時段）分別定義於 `cli.py` L30 與 `tui.py` L93；`tui.py` 版本有模組級時區快取優化，但 `cli.py` 版本缺少此優化，二者行為有輕微差異。
     * **root cause**：TUI 重構時未統一共用 `cli.py` 已有函式，各自獨立實作。
     * **solution**：將帶時區快取的版本（`_TZ_TW`/`_TZ_US` 模組級快取）移至 `quotes.py`；`cli.py` 移除本地定義，兩檔統一從 `from .quotes import is_market_open` import。
     * **fixed by**：v0.0.2-dev

32. [closed] [bug#00035] [UI] **預設依照台幣計價以及美金計價的部位在持倉明細中排版分開**
     * **問題描述**：使用者希望能將 holdings 區域預設依照「美金計價的部位」與「台幣計價的部位」區分開，並且在兩者之間空一行。之前的做法是在同一個券商/帳戶分組中，以空行隔開 USD/TWD，這會造成不同的券商（如 Firstrade 是 USD、元大是 TWD）無法以大區別進行大幣別排版分離。
     * **root cause**：之前的 holdings 表格直接以所有 positions 的券商進行分組，沒有優先根據幣別大類進行劃分。
     * **solution**：將 holdings 表格繪製邏輯（適用於 TUI `_build_holdings_table`、`UpcomingEventsScreen._render_holdings`、`DashboardScreen._render_all` 以及 CLI `_build_broker_holdings` 共 4 處）修改為：先過濾拆分出 `usd_positions` 與 `twd_positions`，分別獲取其券商分組，先渲染 USD 區塊的券商及其部位，再在中間加入一空白行，最後渲染 TWD 區塊。並且在 `DashboardScreen` 中精確對應 `row_data` 與 DataTable 的行號索引。
     * **fixed by**：v0.0.2-dev

33. [closed] [bug#00036] [newfeature] **新增部位對話框 (AddPositionModal) 支援動態選擇權欄位與代碼自動生成**
    * **問題描述**：使用者在新增持倉時，如果商品類型為 `option`，原本的表單缺少選擇權特有必填資訊（標的、到期日、履約價、買賣權類型、乘數），且無法依類型動態切換必填欄位。
    * **root cause**：AddPositionModal 原始設計僅有通用證券欄位，並未整合選擇權特有的欄位輸入與動態 Visibility 隱藏/顯示及專屬驗證。
    * **solution**：1. 在 `AddPositionModal` 中新增 5 個選擇權特有欄位，預設藉由 CSS class `.option-only` 隱藏。 2. 實作 `on_select_changed` 監聽商品類型，當為 `option` 時動態切換該 5 行為 `display: block`，其餘類型切回 `none`。 3. 在欄位巡覽時自動跳過隱藏欄位。 4. 在 `_submit()` 驗證中，當為 `option` 且代碼留空時，要求完整填寫標的、到期日（需為 YYYY-MM-DD）、履約價（需為正數），並於通過後自動組裝成標準的 OCC 美股選擇權代碼或台股代碼。5. 當商品類型為 `option` 時，將最上方的「代碼 (Symbol)」動態改為選填的「合約代碼 (Symbol)」，並更新 Placeholder 說明「留空則依下方明細自動產生」，以解決與下方「標的代碼 (Underlying)」重複的視覺混淆。6. 在「部位調整 (AdjustPositionsModal)」中重新加入「🗑️ 移除部位 (Remove Position)」選項，並提供「RemovePositionSelectModal」供使用者自現有持倉清單中選取部位，再調用確認對話框執行刪除。
    * **fixed by**：v0.0.3-dev

34. [closed] [bug#00037] [function] **空頭部位 (Short Position) 未實現損益百分比計算正負號相反**
     * **問題描述**：當部位為空頭 (如 quantity < 0) 時，其總成本 total_cost 為負值。原始損益百分比計算公式為 (pnl / cost) * 100，由於分母 cost 為負，導致獲利時百分比顯示為負，虧損時顯示為正，正負號與實際損益相反。
     * **root cause**：未對分母的 total_cost 取絕對值，導致當 quantity 為負時，計算出的回報百分比符號被反轉。
     * **solution**：在 `Position.unrealized_pnl_pct` 計算中，將分母改為 `abs(cost)`，確保不論多頭或空頭部位，損益百分比的正負符號皆與未實現損益金額 (unrealized_pnl) 的符號保持一致。
     * **fixed by**：v0.0.3-dev

35. [closed] [bug#00038] [UI] **直接選取持倉項目修改時，第一次按下 Enter 儲存後畫面重複彈出修改框**
     * **問題描述**：使用者在 holdings 表格內選取格子並按下 Enter 鍵開啟修改對話框 (`FieldEditModal`)，完成輸入後再次按下 Enter 鍵儲存。此時對話框雖關閉，但畫面卻會立刻重複彈出同一個修改框，導致使用者必須連續關閉兩次或產生未完成輸入的誤會。
     * **root cause**：在對話框內按下 Enter 鍵時，該鍵盤事件沒有被阻斷冒泡 (bubble up)。當對話框關閉且焦點 (focus) 立即焦點返回到下方的 `DataTable` 時，未消耗的 Enter 按鍵事件被底層 `DataTable` 接收，再次觸發了儲存格選擇事件，因而二度拉起修改對話框。
     * **solution**：在 `FieldEditModal` 的 `on_key` 和 `on_button_pressed` 事件處理方法中，當確認收到 `enter`、`escape` 或儲存/取消按鈕點擊事件時，明確調用 `event.stop()` 和 `event.prevent_default()` 阻斷事件傳遞與預設行為，保證按鍵事件被對話框完全消耗。
     * **fixed by**：v0.0.3-dev

36. [closed] [bug#00040] [function] **TUI 載入持倉時因未解構 `load_manual_positions` 回傳之 Tuple 導致崩潰**
     * **問題描述**：在 TUI 介面啟動或更新部位時，程式會拋出 AttributeError 崩潰。
     * **root cause**：`load_manual_positions` 改為回傳 `(positions, cash_positions)`，但 TUI 未同步解構，導致將 Tuple 傳給 `_calc_weights` 並在疊代時拋出 AttributeError。
     * **solution**：在 TUI 的所有 `load_manual_positions` 呼叫處進行 Tuple 解構，並在 `save_manual_positions` 時傳入對應 of `cash_positions` 以免現金資料被覆蓋抹除。
     * **fixed by**：v0.0.3-dev

37. [closed] [bug#00041] [function] **ActiveETFsScreen 六項 Bug 修復 (AUM / 持股欄位 / 快取)**
     * **問題描述**：(1) 所有 ETF AUM 顯示 —；(2) 排行表最大持股 % 不顯示；(3) 中欄缺少表頭欄讓使用者知道目前是哪一支 ETF 的持股；(4) 中欄持股的權重與股數均無資料；(5) 右欄歷史交易對大多數 ETF 無法顯示；(6) 缺少每-ETF 快取機制（全部重載）。
     * **root cause**：(1) `fast_info.total_assets` 屬性不存在，`fast_info.market_cap` 對 ETF 回傳 `None`；(2)/(4) `fetch_etf_holdings` 內用错誤欄位名 (`holdingName`/`holdingPercent`/`row.name`)，yfinance 實際輸出為以 ticker 為 index、`Name` 欄、`Holding Percent` 欄；(3) compose 中缺少 `#etf-holdings-title` 與 `#etf-history-title` Static 元件；(5) 除少數 ARK ETF 外皆無 `history` 資料；(6) 全部 ETF 共用單一 JSON，無法分別檢查與逐項修復。
     * **solution**：如 bug#00039 solution 所述，全數已一併修復。
     * **fixed by**：v0.0.3-dev

38. [closed] [bug#00042] [newfeature] **TUI 部位調整新增編輯與刪除部位功能 (TUI Edit and Delete Positions Support)**
    * **問題描述**：目前 TUI 中的「部位調整」選項僅支援新增部位。需要額外支援編輯現存部位（在與新增部位相同的表單中載入舊有資料並允許修改儲存）與整筆刪除部位功能。
    * **root cause**：原 `AdjustPositionsModal` 僅綁定 `AddPositionModal`，沒有實作針對現存手動持倉進行修改或整筆刪除的 UI 路由與儲存邏輯。
    * **solution**：1. 修改 `AdjustPositionsModal` 新增「修改部位」與「刪除部位」選項。2. 實作 `ChoosePositionModal` 用於列出並選取現有部位，並在選取後呼叫 `AddPositionModal(pos)` 載入舊數據進行編輯。3. 實作 `DeleteConfirmModal` 用於二次確認刪除。4. 在 `DashboardScreen` 中處理編輯回傳及刪除確認，並重整寫回本機 JSON。
    * **fixed by**：v0.0.3-dev

40. [closed] [bug#00043] [function] **AddPositionModal 新增 option 時 symbol 必填驗證攔截、缺少 OCC 自動組裝、TW 後綴誤套 option**
    * **問題描述**：在 TUI 新增選擇權部位時，若 symbol 欄位留空（應由明細自動生成代碼），程式在進入 option 欄位驗證前即 return 錯誤；即使填入 option 明細，也無任何邏輯將其組裝成 OCC 格式；此外 `market == TW` 的 `.TW` 後綴強制套用於 option，破壞代碼格式，導致新增 option 永遠失敗。
    * **root cause**：1. `_submit()` 早期必填驗證 `if not symbol` 未排除 option 類型。2. option 欄位驗證通過後無 OCC symbol 組裝邏輯。3. TW 後綴邏輯未排除 `inst_type == "option"`。
    * **solution**：1. 改為 `if not symbol and inst_type != "option"` 以允許 option 的空代碼進入後續驗證。2. 在 option 欄位驗證通過後，若 symbol 仍空，以 `underlying/expiry/strike/opt_type` 組裝 US OCC 格式（`{UDL}{YYMMDD}{C|P}{STRIKE*1000:08d}`）或台股格式（`{UDL}{YYMMDD}{C|P}`）。3. `.TW` 後綴改為 `if market == "TW" and inst_type != "option"`。
    * **fixed by**：v0.0.3-dev

40. [closed] [bug#00044] [function] **`_handle_add_position_result()` 合併邏輯缺少 `instrument_type` 比對導致 option 被誤合併至 stock**
     * **問題描述**：新增一個 option 部位時，若現有持倉中已存在同 broker/account/symbol 的 stock，兩者會被錯誤地合併（數量相加），使 option 消失而 stock 數量異常增加。
     * **root cause**：`_handle_add_position_result()` 的合併判斷條件僅為 `broker + account + symbol`，未包含 `instrument_type`，故 AAPL stock 與 AAPL option 被視為同一部位。
     * **solution**：合併判斷條件新增 `p.instrument_type == pos.instrument_type`，確保 stock/option/etf 各自獨立。
     * **fixed by**：v0.0.3-dev

41. [closed] [bug#00045] [function] **`DeleteConfirmModal` 在 tui.py 中重複定義兩次（死碼）**
     * **問題描述**：`tui.py` 中存在兩個同名的 `DeleteConfirmModal` class（L1924 舊版使用 `#delete-dialog`，L2083 新版使用 `#delete-confirm-dialog`），Python 只使用後者，前者為完全無效的死碼，造成維護困惑及潛在 CSS ID 衝突風險。
     * **root cause**：重構時新增完整版 `DeleteConfirmModal` 後，未清除原有的舊版定義。
     * **solution**：移除 L1924 的舊版 `DeleteConfirmModal`（共 54 行），僅保留 L2083 的完整版（含正確 `#delete-confirm-dialog` ID 與左右鍵切換支援）。
     * **fixed by**：v0.0.3-dev

42. [closed] [bug#00046] [function] **修改/合併部位時未清空快取報價欄位，導致市值與損益顯示為修改前的舊值（選擇權因乘數放大尤其明顯）**
     * **問題描述**：使用者反映選擇權部位的市值未能反映真實價值。透過「修改部位」、表格內即時修改（Qty/Avg Cost）或新增時合併至既有同代碼部位後，畫面市值/損益仍是異動前（舊數量/舊成本/舊乘數）算出的快取值，且此問題不會隨背景自動刷新而修正。
     * **root cause**：`Position.value` 優先回傳已快取的 `market_value`；`enrich_positions_with_quotes` 僅在 `market_price`/`market_value`/`prev_close` 皆為 `None` 時才重新抓取並依新數量/乘數重算。但 `AddPositionModal._submit()` 的編輯路徑以 `self.position.model_copy(deep=True)` 保留舊有的三個快取欄位；`_handle_field_edit`、`_handle_add_position_result`（新增合併既有部位）與 `_apply_broker_account_edit`（券商/帳戶合併）在變更 `quantity`/`avg_cost`/`multiplier`/`symbol` 後同樣未清空快取欄位，導致其永遠不會被判定為需要重新抓取。
     * **solution**：於上述四處修改/合併邏輯完成欄位異動後，統一將受影響部位的 `market_price`、`market_value`、`prev_close` 重設為 `None`，確保儲存後下一次刷新會重新抓取報價並以最新數量/成本/乘數正確計算市值與損益。
     * **fixed by**：v0.0.3-dev

43. [closed] [bug#00047] [UI] **AddPositionModal 選擇 option 類型時頂部 Symbol 與 Underlying 欄位重複要求輸入同一代碼（bug#00036 方案5 迴歸）**
     * **問題描述**：使用者反映新增選擇權部位時，需在頂部「代碼(Symbol)」與下方「標的代碼(Underlying)」兩欄分別輸入同一檔股票代碼，且頂部欄位仍固定標示紅色必填星號，造成混淆。
     * **root cause**：`compose()`/`on_mount()`/`on_select_changed()` 僅切換 `#option-fields-container`（Underlying/Strike/Expiry/Type/Multiplier）顯示與否，從未依 `inst_type` 隱藏或調整頂部 Symbol 欄位；此為 bug#00036 方案5（動態改為選填並更新提示文字以解決視覺混淆）的迴歸，目前程式碼中該邏輯已不存在。
     * **solution**：新增 `#symbol-field-row` 容器 id，於 `on_mount`/`on_select_changed` 依 `inst_type == "option"` 隱藏該列並自動聚焦至 Underlying 欄位；同步更新 `on_key` 欄位巡覽清單排除隱藏的 `add-symbol`；`_submit()` 中 option 類型一律以 underlying/strike/expiry/type 重新組裝 OCC/台股代碼，不再讀取（可能過期的）Symbol 欄位值，使用者只需輸入一次標的代碼。
     * **fixed by**：v0.0.3-dev

44. [closed] [bug#00048] [performance] **`_build_metrics_panel` 每次渲染皆對每筆持倉同步呼叫未快取的 `fetch_beta()`，阻塞 TUI 主執行緒**
     * **問題描述**：使用者反映 TUI 整體效能低落、操作卡頓。
     * **root cause**：`_build_metrics_panel()`（計算 Portfolio Beta）於每次 `_render_all()` 對每一筆持倉同步呼叫 `fetch_beta()`；該函式無任何快取，每次皆發出較慢的 `yfinance ticker.info` 網路請求。`_render_all()` 透過 `call_from_thread()` 排程在主 UI 執行緒執行，且每 60 秒背景刷新、以及新增/修改/刪除部位後皆會觸發，導致主執行緒被「N 筆持倉 × 網路延遲」的同步呼叫完全阻塞，介面凍結。
     * **solution**：在 `quotes.py` 的 `fetch_beta()` 加入以 symbol 為 key、6 小時 TTL 的記憶體快取（`_beta_cache`），未過期時直接回傳快取值，避免重複觸發阻塞性網路請求，大幅降低渲染時主執行緒卡頓的頻率與時長。
     * **fixed by**：v0.0.3-dev

45. [closed] [bug#00049] [function] **`_build_holdings_table` 與 `_build_sector_panel` 為死碼，定義後從未被呼叫**
     * **問題描述**：審查 TUI 程式碼流程時發現 `tui.py` 的模組層級函式 `_build_holdings_table()`（Rich Table 版持倉表格）與 `_build_sector_panel()`（Sector 分布面板）皆完整定義，但檔案中沒有任何呼叫點；`DashboardScreen` 實際使用原生 `DataTable.add_row()` 直接渲染，`_render_all()` 亦未呼叫 `_build_sector_panel`。
     * **root cause**：重構為原生 `DataTable` 渲染後，舊版以 Rich Table 字串渲染持倉/Sector 面板的函式未同步移除。
     * **solution**：【更正】進一步搜尋全專案後發現 `_build_holdings_table()` 實際被 `scripts/verify_tui.py` 匯入並呼叫（作為自動化驗證腳本的一部分），並非真正死碼，故予以保留；僅 `_build_sector_panel()` 經確認全專案零呼叫點，已於 bug#00055 一併移除。
     * **fixed by**：v0.0.3-dev（`_build_sector_panel` 部分；`_build_holdings_table` 保留不變，待使用者驗證）

46. [closed] [bug#00050] [function] **選擇權部位 `symbol` 欄位若非正確 OCC 代碼（如僅存純標的代碼 "INTC"），報價會誤抓標的股票價格而非選擇權權利金**
     * **問題描述**：使用者反映 INTC 選擇權（strike 150, 到期 2026-09-18, call, 20 口）畫面顯示的價格/市值與 INTC 股票價格相同，並非合理的選擇權權利金。
     * **root cause**：實際持倉資料中該筆記錄 `instrument_type="option"` 但 `symbol` 僅為裸代碼 `"INTC"`（並非 OCC 格式 `INTC260918C00150000`），研判為先前 bug#00047 修復前，使用者於（當時仍可見的）頂部 Symbol 欄位直接輸入了標的代碼。`quotes.py` 的 `_normalize_symbol_for_yf()` 對 option 類型僅去除空白後原樣回傳 `symbol` 供 yfinance 查詢，未驗證其是否為有效選擇權代碼；`"INTC"` 因此被當成股票代碼查詢，回傳的是 INTC 現股報價。
     * **solution**：於 `models.py` 的 `auto_populate_option_fields` validator 新增反向校正：當 `instrument_type == "option"` 且非台股市場、`underlying`/`expiry`/`strike`/`option_type` 皆已齊備時，一律依此四欄重新計算標準 OCC 代碼，若與現存 `symbol` 不同則覆寫並清空 `market_price`/`market_value`/`prev_close`（避免沿用錯誤標的算出的舊快取值）。由於 `storage.load_manual_positions()` 每次讀取皆會呼叫 `Position.model_validate()`，此修正可在下次刷新/重啟時自動修復既有錯誤資料；另已直接修復 `ray60110_positions.json` 中該筆 INTC 部位的 `symbol`。台股選擇權格式因 `cli.py`/`tui.py` 現有生成邏輯本身不一致（見 bug#00051），本次校正範圍不含台股，避免誤改使用者手動輸入的台股代碼。
     * **fixed by**：v0.0.3-dev

47. [closed] [bug#00051] [function] **台股選擇權代碼自動生成格式在 `cli.py` 與 `tui.py` 不一致**
     * **問題描述**：修復 bug#00050 時發現，台股選擇權代碼的自動生成邏輯在兩處不同：`tui.py`（`AddPositionModal._submit`）產生 `{underlying}{yymmdd}{C|P}`（無履約價後綴）；`cli.py`（互動新增流程）預設建議值為 `{underlying}{yymmdd}{C|P}{strike:05d}`（含 5 位數履約價後綴）。兩者格式不同，可能造成同一份持倉資料在 CLI 與 TUI 之間代碼格式不一致。
     * **root cause**：兩處各自獨立實作台股選擇權代碼組裝，未抽取共用函式，格式定義因此分歧。
     * **solution**：因應使用者需求於 bug#00056 完全移除 `cli.py`，此不一致已隨之消失（僅剩 `tui.py` 一種實作）。順勢將 `models.py` 的 `auto_populate_option_fields` 反向校正邏輯（bug#00050）擴大涵蓋台股選擇權，統一採用 `tui.py` 的 `{underlying}{yymmdd}{C|P}` 格式自動校正/補全 `symbol`。
     * **fixed by**：v0.0.3-dev

48. [closed] [bug#00052] [newfeature] **精簡 cli.py，將共享邏輯抽離至 shared.py，並補足 TUI 缺失的 Add Side 功能 (依據需求已移除不必要的 Log Trade/實現損益計算)**
     * **問題描述**：舊有 `cli.py` 體積龐大（2501行），內含多個已由 TUI 替代的指令。此外，TUI 在部位調整上缺乏 `side`（Long/Short 多空方向）選擇。另外，TUI 中的 `run_save_snapshot` 調用含有 `Confirm.ask()` 的 cli 方法，導致在後台 thread worker 中可能卡死。
     * **root cause**：1. 重構 TUI 時未將總經行事曆、歷史圖表等純邏輯抽離，導致 TUI 依然 import 大量 `cli.py` 代碼。 2. 舊版 `cli.py` 所包含之 `log-trade` 交易登錄與已實現損益計算並非本系統的核心追蹤目標，系統應專注於維護活躍部位的未實現損益。
     * **solution**：1. 新建 `assettrack/shared.py` 遷移 `MACRO_EVENT_NAMES`、`get_upcoming_macro_events` 與 `draw_history_chart`，TUI 改由 `shared.py` import。 2. 直接在 TUI 內實作非阻塞之 `run_save_snapshot`，免除 `Confirm.ask`。 3. 在 `AddPositionModal` 中新增「持倉方向 (Side)」Select，支持做多與放空部位之新增與修改。 4. 依使用者明確指示，不在 TUI 中加入交易登錄與已實現損益計算流程以精簡工作流，專注於管理活躍部位的「未實現損益」。 5. 精簡 `cli.py` 至 ~40 行純 TUI 入口包裝。
     * **fixed by**: v0.0.3-dev

49. [closed] [bug#00053] [performance] **`enrich_positions_with_quotes()` 逐筆序列抓取報價並以 `time.sleep(delay)` 節流，刷新耗時隨持倉數線性增加**
     * **問題描述**：使用者要求全面 code review 並確認效能已優化、無冗餘定義。審查 `quotes.py` 發現報價刷新仍為效能瓶頸來源之一。
     * **root cause**：`enrich_positions_with_quotes()` 對每筆需要報價的持倉逐一呼叫 yfinance 後 `time.sleep(delay)`（TUI 呼叫時 `delay=0.1`），總耗時 ≈ N 筆 × (網路延遲 + 0.1s)，隨持倉數線性增加；即使已在背景 thread 執行不阻塞主執行緒，仍拉長「資料變新鮮」所需時間。
     * **solution**：改為以 `concurrent.futures.ThreadPoolExecutor`（`max_workers=min(10, len(positions))`）併發抓取，比照本檔案已有的 `fetch_earnings_calendar` 併發模式；移除已無作用的 `delay` 參數與呼叫處的 `delay=0.1`。已以 fake yfinance stub 驗證：8 筆持倉、每筆模擬 0.3 秒延遲，序列版需 2.4 秒，併發版僅需 ~0.3 秒。
     * **fixed by**：v0.0.3-dev

50. [closed] [bug#00054] [function] **`silence_output()` 以全域 `sys.stdout`/`sys.stderr` 直接置換非執行緒安全，並行呼叫下會永久性靜音全部輸出**
     * **問題描述**：修復 bug#00053 並行化 `enrich_positions_with_quotes()` 後，以 8 個並行執行緒重現測試時，發現 `sys.stdout` 在函式返回後被永久置換為 `NullWriter`，其後所有 `print()` 均無輸出。追查後確認 `fetch_earnings_calendar`／`fetch_active_etf_performance` 既有的 `ThreadPoolExecutor` 併發呼叫路徑上，此問題本已潛在存在。
     * **root cause**：`silence_output()` 原實作以簡單 `try/finally` 直接置換全域 `sys.stdout`/`sys.stderr`，非執行緒安全。多執行緒併發進入/離開時會發生競態：執行緒 A 尚未離開前，執行緒 B 記錄的「舊值」其實是 A 置換後的 `NullWriter`；A 離開時雖正確還原，但 B 離開時卻把 `sys.stdout` 錯誤地「還原」為 A 的 `NullWriter`，導致真正的 stdout 永久遺失。
     * **solution**：改為以 `threading.Lock` 保護的參照計數（reference count）機制：僅第一個進入的並行呼叫實際置換 `sys.stdout`/`sys.stderr` 並保存真實值，僅最後一個離開的呼叫負責還原；鎖只保護計數與置換瞬間，不包住實際網路 I/O，因此不影響並行效能。已驗證 8 執行緒併發下 `sys.stdout` 皆正確還原且併發耗時不受影響。
     * **fixed by**：v0.0.3-dev

51. [closed] [bug#00055] [function] **`tui.py` 殘留舊架構死碼：未使用的 `_build_sector_panel`、`zoneinfo`/`SimpleNamespace`/`Console`/`Prompt`/`Confirm` 匯入與過時的模組說明文件字串**
     * **問題描述**：延續 code review 要求「無冗餘定義」，於 `tui.py` 中找到數項確認無任何呼叫點的死碼與匯入。
     * **root cause**：1. `_build_sector_panel()` 於全專案（含 `scripts/verify_tui.py`）搜尋皆無呼叫點，為 bug#00049 移除死碼後 UI 已改用 `#recent-events-panel` 等其他面板呈現，此函式從未被接回。2. `import zoneinfo`、`from types import SimpleNamespace`、`from rich.console import Console`（連同其建立的 `_console` 實例）、`from rich.prompt import Prompt, Confirm` 皆為舊版「透過 app.suspend() 呼叫 cli.py Rich/Prompt 互動邏輯」架構的殘留，該架構已在 bug#00052 精簡 cli.py 時被完全取代，但匯入未同步清除。3. 模組頂端說明文件字串仍描述已不存在的 `app.suspend()` 機制。
     * **solution**：移除 `_build_sector_panel()`（`_build_holdings_table` 經確認仍被 `scripts/verify_tui.py` 使用而保留，修正 bug#00049 先前的死碼認定）；移除上述 5 個未使用匯入/實例；更新模組說明文件字串以反映目前純 Textual Modal/Screen 架架構。
     * **fixed by**：v0.0.3-dev

52. [closed] [bug#00056] [function] **完全移除 `cli.py`，改由 `tui.py` 提供唯一命令列進入點**
     * **問題描述**：使用者明確指示：現存所有 `cli.py` 所需功能皆已移至 `tui.py`，`cli.py` 不應再存在，要求徹底檢查並移除。
     * **root cause**：bug#00052 已將 `cli.py` 精簡至 ~36 行的純 Typer 入口 wrapper（僅剩 `app` 與 `main(ctx, user)` callback，呼叫 `tui.run_tui_dashboard`），但檔案本身及 `typer` 依賴並未一併移除；`pyproject.toml` 的 `[project.scripts]`、`entrypoint.py`（PyInstaller 打包入口）與 `scripts/verify_tui.py` 皆仍 import `assettrack.cli`，若直接刪除檔案會導致套件安裝的 `assettrack` 命令與打包執行檔無法啟動。
     * **solution**：1. 於 `tui.py` 新增 `main()` 作為套件唯一命令列進入點：改用標準函式庫 `argparse`（無需 Typer，因全系統僅單一指令、單一 `--user/-u` 選項，不需子指令框架）解析參數後呼叫既有的 `run_tui_dashboard()`。2. 更新 `pyproject.toml`：`[project.scripts]` 改為 `assettrack = "assettrack.tui:main"`，並移除已無用的 `typer` 依賴。3. 更新 `entrypoint.py` 改為 `from assettrack.tui import main`。4. 更新 `scripts/verify_tui.py` 的進入點驗證改為匯入 `assettrack.tui.main`。5. 刪除 `assettrack/cli.py`。6. 更新 `tui.py` 模組說明文件字串、`README.md`（移除已不存在的 CLI 子指令說明、Mermaid 架構圖 CLI 子圖、指令列表，改為 TUI 鍵盤快速鍵對照表）。7. 順勢解決 bug#00051（TW 選擇權代碼格式不一致）：因 `cli.py` 已不存在，`models.py` 的自動校正邏輯現可安全擴大涵蓋台股選擇權。已以編譯檢查、`argparse` 參數解析模擬、以及 `entrypoint.py`/`scripts/verify_tui.py` 匯入測試驗證。
     * **fixed by**：v0.0.3-dev

53. [closed] [bug#00057] [function] **主動式 ETF 持股與歷史交易資料稽核：實際仍大量使用 hardcode 與虛構 mock 資料，違反 bug#00039「不得寫死」要求**
     * **問題描述**：`tui.py` L3204-3206 註解聲稱「All AUM, performance, and holdings come from live yfinance calls... Nothing is hard-coded here」，但實際程式碼稽核發現多處與此矛盾：(1) `quotes.py` `fetch_etf_holdings()` 內建 `special_holdings` dict，為 BRIDGEWATER/CITADEL/SOROS/DESHAW/MILLENNIUM/RENAISSANCE 六個「基金」寫死固定持股清單與權重；(2) `tui.py` `run_background_fetch()` 另建 `special_funds` dict，為其中四檔寫死固定 AUM/price/change_pct/YTD/1Y 報酬率，數值永久不變、不隨時間更新；(3) `estimate_shares()` 於 AUM 缺失時以 symbol 的 MD5 hash 產生確定性但完全虛構的股數；(4) `generate_mock_holdings()` 於 yfinance 與 fallback JSON 皆無資料時，以 symbol 的 MD5 hash 為種子生成虛構持股清單（甚至含虛構公司名稱如 "Mock Company XXX"）；(5) `generate_mock_history()` 於 per-ETF cache 缺少 `history` 欄位時「必定」被呼叫產生虛構買賣交易紀錄（日期/買賣方向/股數/價格皆為 hash 演算生成），而 yfinance 從未真正提供任一 ETF 的底層持股歷史交易明細，故右欄「歷史買賣紀錄」對所有 ETF（含真實上市 ETF 如 ARKK/JEPI）在實務上永遠是 100% 虛構資料；(6) fallback 讀取的 `/Users/rayyj/Projects/AssetTrack/data/active_etf_holdings.json` 為寫死絕對路徑（未使用既有 `get_data_dir()`），且對應寫入函式 `save_active_etf_holdings()` 已於 `storage.py` 被改為 no-op stub，該檔案永遠不存在，此 fallback 分支形同死碼，任何缺乏 yfinance 資料的 ETF 會直接落入 (3)(4) 的虛構資料生成。正向確認：對於 yfinance 能成功回傳 `funds_data.top_holdings` 的真實 ETF（如多數 US_ACTIVE_TICKERS/TWD_ACTIVE_TICKERS 成分），欄位對應（`idx`→symbol、`Name`、`Holding Percent`×100）本身正確，此為 bug#00039/00041 已修復範圍，未發現迴歸。
     * **root cause**：bug#00039要求「全數資料必須每日動態刷新且不得寫死」，其 solution 第 5 點原意為「歷史買賣如無則顯示明確說明（需由外部 scraper 寫入），不再顯示空白」，但目前程式碼並未依此實作外部 scraper 或「無資料」提示，而是改用 hash-seed 偽隨機演算法生成假資料掩蓋缺口；且 BRIDGEWATER 等六檔並非真實可交易 ETF ticker（為對沖基金名稱），yfinance 本就無法查得，早期開發推測直接寫死靜態數字繞過，之後未清理。
     * **solution**：已依建議方案實作並移除全部虛構資料來源：1. 移除 `quotes.py` `fetch_etf_holdings()` 內的 `special_holdings` 寫死字典，以及 `tui.py` `run_background_fetch()` 內的 `special_funds` 寫死字典；BRIDGEWATER/CITADEL/SOROS/DESHAW/MILLENNIUM/RENAISSANCE 六檔非真實 ETF ticker 已自 `US_ACTIVE_TICKERS` 移除（保留 SEQUX，為 yfinance 可查得之真實共同基金）；`_render_one_tab` 的星號標記同步收斂為僅標示 SEQUX。2. 移除 `generate_mock_holdings()`／`generate_mock_history()` 兩函式；`run_background_fetch()` 不再呼叫 `generate_mock_history`，`history` 缺失時維持空值，交由既有的 `_render_history()`「暫無歷史交易紀錄」提示分支顯示（該分支原本存在但因 mock 資料而從未觸發）。3. `estimate_shares()` 移除 MD5 hash fallback，改為 `Optional[int]`：AUM 或 weight 缺失時回傳 `None`，UI 既有邏輯（`_render_holdings`）已會顯示 `—`。4. 移除 `fetch_etf_holdings()` 內失效的 `data/active_etf_holdings.json` 絕對路徑 fallback 分支（該檔案因 `save_active_etf_holdings()` 為 no-op 而永遠不存在）；`storage.py` 內對應的 `load_active_etf_data`/`save_active_etf_holdings`/`etf_cache_needs_refresh` legacy stub 為與本次 hardcode 問題無直接關聯之既有死碼，予以保留未動，僅供使用者後續自行決定是否清理。5. 更新 `tui.py` L3204-3206 註解以反映實際狀況（不足資料時顯示「—」/「暫無資料」而非回填假資料）。已以 `py_compile` 語法檢查、`quotes.estimate_shares()` 行為驗證（AUM 缺失回傳 `None`／AUM 存在回傳正確估算值）、`ast` 解析確認 `US_ACTIVE_TICKERS` 64 檔中已無六個對沖基金假代碼且 SEQUX 保留、以及全專案 grep 確認 `special_holdings`/`special_funds`/`generate_mock_holdings`/`generate_mock_history`/六個對沖基金名稱均無殘留引用。
     * **fixed by**：v0.0.3-dev（待使用者驗證：需在有網路環境下實際執行 TUI 開啟「主動式 ETF 排行」畫面，確認持股與歷史交易面板於無資料時正確顯示「暫無資料」而非假資料，且不影響 SEQUX 與一般 ETF 的既有正確資料流程）

54. [closed] [bug#00058] [performance] **主動式 ETF 排行 YTD/1Y 績效欄位時常顯示空白（—）：單一大批次 yfinance 請求 + 快取新鮮度判斷未涵蓋績效欄位**
     * **問題描述**：使用者反映「主動式 ETF 列表裡，時常跑不出 YTD、1Y 的績效表現」。程式碼稽核發現兩個疊加的根本原因：
       (1) `run_background_fetch()` 對當日所有 stale 符號（US 64 檔 + TW 20 檔，最多 ~84 檔）於 `fetch_active_etf_performance(stale_symbols)`（`quotes.py`）以「單一一次」`yf.download()` 批次請求取得，無分批、無重試、無 backoff；此呼叫外層僅有一層 `try/except`，一旦 Yahoo 對如此大量 ticker 的單次請求觸發 rate limit 或連線失敗（已於稽核環境以 curl 層級實測重現：整批回傳空的 DataFrame，`shape=(0, N)`），程式不會拋出例外（`yf.download` 對逐檔失敗是靜默降級，不是 raise），迴圈會針對每個 symbol 個別判定「查無資料」，最終該次呼叫回傳的是「每一檔`皆為 `{"price": None, "change_pct": None, "return_ytd": None, "return_1y": None}`——也就是一次網路波動會讓「當次刷新的全部 ETF」績效同時消失，而非只影響個別標的。此外 `silence_output()` + `logging.getLogger("yfinance").setLevel(CRITICAL)` 雙重靜音，任何底層失敗訊息（如 `possibly delisted`、rate limit 警告）完全不會被記錄或顯示，使用者與開發者皆無從得知這次刷新其實失敗了。
       (2) 更關鍵的放大效應：`storage.py` 的 `etf_symbol_cache_fresh()` 判斷「今日快取是否新鮮」只檢查 `"holdings" in cached` 與 `"aum" in cached` 兩個 **key 是否存在**，完全不檢查績效欄位（`price`/`change_pct`/`return_ytd`/`return_1y`）是否存在、更不檢查其值是否為 `None`。而 `run_background_fetch()` 寫入快取時，即使 (1) 導致績效整批失敗，`cached[k] = p_item[k]`（`k` 迴圈涵蓋全部 4 個欄位）仍會把值為 `None` 的 4 個 key 寫入 `cached` dict 並以今日時間戳 `save_etf_symbol_cache()` 存檔。因此只要當天「第一次」背景刷新運氣不好卡到 (1)，之後同一天內 `etf_symbol_cache_fresh()` 會持續回傳 `True`（key 都在、時間戳是今天），該 ETF 就不會被排進 `stale_symbols` 重新嘗試，YTD/1Y 便會在畫面上以 `_fmt_pct(None)` → `"—"` 的形式「整天」顯示空白，直到 UTC 隔日快取過期才有機會重抓。使用者唯一的手動補救是按 `c`（`action_clear_cache`）清空全部快取重來，但那又是同一種「一次性全批次」請求模式，容易在網路狀況不佳或請求量大時重蹈覆轍。
       附帶觀察（未列入本次「跑不出」症狀，但屬同一函式的潛在風險）：`fetch_active_etf_performance()` 以 `history_days = (last_date - first_date).days >= 360` 判斷是否計算 1Y 報酬；若 `yf.download` 回傳被截斷的不完整歷史（例如僅回傳近 200 天），即使該 ETF 實際已上市多年，1Y 仍會被誤判為「資料不足」而回傳 `None`；同樣地 YTD 計算中的「inception 判斷」`if first_date > jan_1st` 在資料被截斷時可能誤用截斷後的 `first_date` 當作發行起始價，算出的 YTD 數字可能不是真正的年初至今報酬（此為潛在「算錯」而非「顯示空白」，特此記錄以供後續處理）。
     * **root cause**：1. 效能相關函式 `fetch_active_etf_performance()` 對大量 ticker 採單次無分批/無重試的批次請求設計，任何一次網路層失敗會讓整批結果全數變成 `None`，且錯誤被完全靜音。2. `etf_symbol_cache_fresh()` 的新鮮度判斷邏輯只驗證 key 是否存在，未驗證績效資料是否「實際抓取成功」，導致單次失敗被「快取」下來一整天，無法自我修復。
     * **solution**：已實作前三項建議（在效能與可靠度間取平衡，未過度增加請求次數）：1. `quotes.py` 將原本單一 `yf.download()` 大批次呼叫拆為 `_fetch_active_etf_performance_batch()`（單批次邏輯，內容不變）+ `fetch_active_etf_performance()` 外層分批協調：預設 `chunk_size=15`，將 stale 符號切成多個小批次逐一請求，批次間 `time.sleep(0.3)` 節流；若某批次「全部」回傳 price=None（判定為整批請求失敗，而非個別標的無資料），以 `max_retries=1` 重試一次（間隔 0.5 秒），只對真正整批失敗的情況重試，避免對已成功或個別標的無資料的批次做無謂的重複請求。最壞情況下總請求數上限為 `2 × 批次數`（約 84 檔 / 15 = 6 批 → 最多 12 次請求），仍在背景執行緒（`@work(thread=True)`）執行，不阻塞 UI。2. `storage.py` 的 `etf_symbol_cache_fresh()` 新增 `cached.get("price") is None` 判斷：即使 `holdings`/`aum` 兩個 key 存在，只要 `price` 為 `None`（代表上次績效抓取失敗），即視為不新鮮，下次背景刷新（重新開啟畫面或按 `c`）會自動重試，不再被鎖住一整天。3. `tui.py` `run_background_fetch()` 新增 `perf_fail_count` 計數（依 `stale_perf` 逐一判斷 `price is None`），完成後透過 `_on_fetch_complete()` 傳遞失敗數與嘗試總數；若有失敗，header 會顯示黃色提示「⚠️ 即時數據載入完成，但 N/M 檔 ETF 績效抓取失敗（將於下次刷新自動重試）」，不再靜默顯示綠色成功或單純的「—」。4.（附帶觀察之 1Y/YTD 歷史截斷誤算問題，本次未處理，維持已知風險紀錄，不在使用者本次要求範圍內）。已驗證：`py_compile` 全部檔案通過；以 stub 取代 `_fetch_active_etf_performance_batch()` 模擬「批次1首次全部失敗→重試成功」「批次2一次成功」「批次3兩次皆失敗」情境，確認呼叫次數、重試邏輯與最終成功/失敗分佈完全符合預期（32 檔測試：30 成功、2 失敗、共 5 次批次呼叫）；以暫存目錄測試 `etf_symbol_cache_fresh()` 四種情境（price=None→False、price 有值→True、缺 holdings/aum→False、快取不存在→False）皆正確。
     * **fixed by**：v0.0.3-dev（待使用者驗證：需在有網路環境下實際執行 TUI 開啟「主動式 ETF 排行」畫面，觀察大量 ticker 情境下 YTD/1Y 是否仍時常空白、header 是否於部分失敗時正確顯示黃色警示，並確認整體載入時間未明顯變長至無法接受的程度）

55. [closed] [bug#00059] [newfeature] **「當下持股細節」面板擴充為完整資產配置（股票/債券/現金/特別股/可轉債/其他），不再局限於個股**
     * **問題描述**：使用者反映「主動ETF下持股細節，應調整成不局限於股票，所有部位都顯示出來」。稽核 `fetch_etf_holdings()`（`quotes.py`）發現，中欄「當下持股細節」面板僅顯示 yfinance `funds_data.top_holdings`——這是 Yahoo 自行「精選」的前 N 名個股清單（依實測裝大多數為股票代碼），對於持有現金部位（如 JPST 這類貨幣市場/短債 ETF）、選擇權買權策略疊加（如 JEPI/JEPQ 之 covered-call overlay）或平衡型基金的 ETF，畫面只會顯示其個股名稱與權重，使用者會誤以為該基金 100% 由這些具名個股組成，實際上基金可能有相當比例是現金、債券或其他衍生性金融部位，這些完全沒有被呈現。
     * **root cause**：`fetch_etf_holdings()` 僅呼叫 `funds_data.top_holdings`，未一併呼叫 yfinance 提供的 `funds_data.asset_classes`（回傳整檔基金的 stock/bond/cash/preferred/convertible/other 完整佔比字典，是唯一能反映基金「全部部位類別」的欄位；yfinance 本身不提供逐筆非個股部位明細，如個別債券或選擇權合約，僅有此彙總層級資料，已於稽核時確認）。
     * **solution**：1. `quotes.py` `fetch_etf_holdings()` 新增讀取 `funds_data.asset_classes`，過濾掉 Yahoo 未回報（`None`）的類別後，換算為百分比並以 `asset_classes` 欄位一併回傳（值皆為即時真實資料，非估算/寫死）。2. `tui.py` `run_background_fetch()` 將 `asset_classes` 一併寫入 per-ETF 快取 JSON。3. `_render_holdings()` 重構為兩段式呈現：第一段「▾ 資產配置」依 `_ASSET_CLASS_LABELS`（📈股票/📄債券/💵現金/⭐特別股/🔄可轉債/❔其他）列出整檔基金的完整部位佔比（含依 AUM×佔比估算之市值，無 AUM 時顯示「—」，不捏造數字）；第二段「▾ 前十大持股」維持原本個股持有明細列表。若兩者皆無資料才顯示原本的「持股資料更新中/yfinance 未提供」提示（原本只要 `holdings` 空就會顯示此訊息，即使 `asset_classes` 有資料，已一併修正此邊界情況）。4. 左欄排行表「最大持股」欄位邏輯不受影響（仍只取 `holdings` 個股清單中權重最大者，未混入資產配置列）。已以 `py_compile` 語法檢查通過；以 stub `FundsData`（`asset_classes` 含 `None` 與有效值混合）驗證 `fetch_etf_holdings()` 正確過濾 `None`、正確換算百分比、且不影響既有個股 `shares` 估算邏輯。
     * **fixed by**：v0.0.3-dev（待使用者驗證：需在有網路環境下開啟「主動式 ETF 排行」，挑選現金/債券比例較高的 ETF（如 JPST）確認「資產配置」區塊正確顯示且不再只看到個股清單）

56. [closed] [bug#00060] [newfeature] **新增「進階分析」：跨主動式ETF持股趨勢共識報告（60天視窗，離線運算，真實資料逐日累積）**
     * **問題描述**：使用者要求系統能離線自動整理前幾大主動式ETF的持股資料，告知目前主動式ETF買進/賣出是否呈現相似趨勢（60天內）、列出該趨勢的總股數，並生成統整報告於「主動式ETF排行持股分析」頁面。經確認需求細節：(1) 「相似趨勢」指跨ETF共識——同一檔股票（如NVDA）在多檔主動式ETF中是否同步增/減碼；(2) 報告只需顯示在TUI頁面，不需額外輸出檔案。**重要資料現況稽核**：實際檢查使用者本機 `data/etf_cache/` 84 個快取檔案，全數只有「今天」單一筆快照（每日背景刷新皆為覆寫、非累加），完全沒有任何日期序列的真實歷史；舊的 `data/active_etf_holdings.json`（bug#00057 已判定為死碼並移除其讀取路徑）內持股權重全數為 `0.0`、股數全數為 `null`，是舊版 bug 留下的壞資料，同樣不可用。因此 60 天趨勢在需求提出當下完全沒有可用的真實歷史資料可以計算——此點已與使用者確認：務必只用「從現在開始逐日真實累積」的資料，不得回填或捏造任何缺口。
     * **root cause**：系統過去從未持久化 ETF 持股的「逐日歷史」，`etf_cache/{symbol}.json` 只保存單一份「當前」快照且每日覆寫；bug#00057 移除的 `generate_mock_history()` 原本是用假資料填補這個缺口，移除後缺口原樣保留、從未有真實替代方案。yfinance 本身也不提供任何「基金逐日持股變化」或「基金買賣交易明細」的 API（已於前次稽核確認，`FundsData` 只有 `top_holdings`/`asset_classes` 等靜態當前值，無歷史序列）。
     * **solution**：1. **真實逐日快照儲存**（`storage.py`）：新增 `etf_cache/history/{symbol}.jsonl`（獨立於原本 `etf_cache/*.json`，故不受既有 14 天 `cleanup_old_etf_caches()` 清除邏輯影響），`append_etf_daily_snapshot()` 以「日期去重」方式每日只新增一筆真實記錄（同日重複呼叫不會重複寫入）、`load_etf_daily_snapshots()` 讀取、`prune_etf_history(max_age_days=65)` 於每次開啟畫面時修剪超過 65 天的舊記錄（保留 60 天視窗所需緩衝，不無限增長）。2. **背景刷新自動寫入真實快照**（`tui.py` `run_background_fetch()`）：每次某 ETF 的持股被「真實」重新抓取成功時，同步呼叫 `append_etf_daily_snapshot()` 記錄當天真實持股與AUM；額外在 `on_mount()` 新增一次性回補邏輯——若某 ETF 快取內已有「今天」透過既有真實抓取取得的持股（例如此功能上線當下，使用者稍早已用新版程式碼真實抓過的資料)，但尚未寫入歷史記錄檔，則以該筆快取本身的 `holdings_as_of_date`（真實日期，非憑空指定）補寫一筆，避免這筆已存在的真實資料被平白浪費，但絕不新增 any 非真實日期的資料點。3. **離線趨勢運算引擎**（新檔 `assettrack/analysis.py`，零網路請求）：`compute_symbol_trends()` 對每檔 ETF 比較 60 天視窗內「最早 vs 最新」真實快照，逐檔持股以 ±0.3 個百分點為門檻分類為「增/減/持平」（持股從清單中消失視為權重降至 0，亦視為真實訊號而非臆測）；僅在該 ETF 於視窗內累積 ≥2 筆真實快照時才納入計算（`ready` 旗標），不足者誠實標示於 `etf_coverage`。再跨所有已就緒的 ETF，依同一檔持股股票彙總「共識」（預設 ≥50% ETF 同方向即判定為共識），並用既有已核准的 `estimate_shares()`（AUM×權重估算）換算共識方向的估計總股數變化；`rank_symbol_trends()` 依共識強度排序，且過濾掉只被單一 ETF 持有的股票（不構成跨ETF共識訊號）。4. **「進階分析」畫面**（`tui.py` 新增 `AdvancedAnalysisScreen`）：於 `ActiveETFsScreen` 新增按鍵 `a` 開啟；畫面 header 顯示視窗天數、資料收集進度（「X/84 檔 ETF 已有 ≥2 天真實快照」）與更新時間；表格列出「股票代碼／共識方向（▲買超／▼賣超／分歧）／共識比例／看漲ETF數／看跌ETF數／持平ETF數／估計總股數變化」；若目前完全沒有任何 ETF 就緒（此功能上線當下必然如此），改顯示誠實的空狀態說明文字，告知使用者資料需要真實使用時間累積、不會顯示假趨勢。已驗證：`py_compile` 全部檔案通過；完整 import `assettrack.tui` 無 NameError（過程中抓到並修正一處遺漏的 `rank_symbol_trends` import）；以 Textual `App.run_test()` 無頭模式實際掛載 `AdvancedAnalysisScreen`，確認在使用者本機真實資料（0 天累積齊全，因功能剛上線）下正確顯示空狀態而非假資料或崩潰；以自建合成快照資料（非寫入真實檔案）驗證 `compute_symbol_trends`／`rank_symbol_trends` 的共識分類、门檻、持股消失視為減碼、單一ETF過濾、股數估算等邏輯全數正確；並對使用者本機真實 `etf_cache/` 84 檔既有快取執行一次性真實資料回補，成功為 72 檔（有真實持股資料者）寫入第一筆真實歷史記錄，其餘 12 檔因尚無持股快取暫時略過，回補後 `etfs_ready_count` 仍正確維持為 0（因僅有 1 天資料，尚不足以計算任何趨勢，證明系統不會提前生成假結果）。
     * **fixed by**：v0.0.3-dev（待使用者驗證：需持續在有網路環境下正常使用系統數天，讓每檔 ETF 累積 ≥2 天真實快照後，於「主動式ETF排行」畫面按 `a` 開啟「進階分析」，確認共識判讀與估計股數是否合理；亦請留意 60 天內需持續使用系統以避免資料視窗出現大段真實空缺）。**微調（同日）**：使用者要求將分析視窗由 45 天改為 60 天，已同步更新 `tui.py` 的 `ADVANCED_ANALYSIS_WINDOW_DAYS`、`analysis.py` `compute_symbol_trends()` 預設值、`storage.py` `prune_etf_history()` 修剪緩衝（65 天）及所有相關註解/文件字串，`py_compile` 與參數值皆已重新驗證通過。
