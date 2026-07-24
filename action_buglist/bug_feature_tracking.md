---
tags: [AssetTrack, TUI]
GitHub version: v0.0.2
Local version: v0.0.5-dev
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

**第三次微調（同日）**：使用者發現兩個問題並要求修正：(1) 系統原本用 `datetime.utcnow()` 判斷「今天是否已抓過資料」，但使用者在台灣（UTC+8），UTC換日發生在台灣時間早上8點，導致系統認定的「今天」跟使用者直覺的「今天」不一致；(2) 先前回覆使用者「系統會不會在跨日後自動補抓資料」時，核實 code flow 後發現答案其實是否定的——`ActiveETFsScreen`/`OptionsWatchlistScreen` 的 `run_background_fetch()` 只在該畫面 `on_mount()` 時執行一次，若使用者跨日後只停留在Dashboard、沒有實際切換進那兩個畫面，不會自動產生新一筆真實快照。使用者採納建議，要求兩者一併修正。**實作**：(1) `storage.py` 新增 `taiwan_now()`（以 `zoneinfo.ZoneInfo("Asia/Taipei")` 為基準的 `datetime.utcnow()` 直接替代品，環境缺少時區資料時退回真實UTC而非崩潰），取代 `storage.py`（快取新鮮度判斷、`last_refreshed` 戳記、ETF/期權每日快照日期戳記與修剪基準）、`analysis.py`／`options_analysis.py`（`as_of_date` 預設值）、`quotes.py`（`fetch_etf_holdings()` 的 `holdings_as_of_date`，沿用該檔已existing的 `_TZ_TW`）內所有與此ETF/期權追蹤系統相關的 `datetime.utcnow()`；不影響系統內其他無關的UTC時間戳（如手動部位最後更新時間、財報日曆），保持surgical。(2) 將 `ActiveETFsScreen.run_background_fetch()` 與 `OptionsWatchlistScreen.run_background_fetch()` 內「純抓取與寫入快取/快照」的邏輯抽出為與畫面UI無關的模組層級函式 `_fetch_and_cache_etf_symbols(stale_symbols)` 與 `_fetch_and_cache_options_underlyings(stale)`，兩個畫面原本的方法改為薄包裝（先顯示畫面自己的狀態列文字，呼叫共用函式，再更新畫面自己的UI），行為與先前完全相同。`AssetTrackApp` 新增 `_background_data_refresh()`（`@work(thread=True, exclusive=True)`），在 `_start_dashboard()` 首次進入Dashboard時透過 `set_interval(1800, ...)` 註冊一個每30分鐘執行一次的背景檢查（以旗標防止登出重登入疊加多個計時器），不論使用者當下停留在哪個畫面都會執行；由於新鮮度判斷本身具冪等性（`etf_symbol_cache_fresh`/`options_symbol_fresh` 已抓過當天真實資料就直接跳過），高頻率檢查沒有額外網路成本，實際的抓取動作只會在台灣時間跨日後的第一次檢查時真正發生一次。抓到的新資料不需要額外UI更新邏輯——Dashboard首頁本身既有的60秒刷新週期會自動重新讀取磁碟上的最新快照並更新兩張結論卡片，使用者之後造訪「主動式ETF排行」/「期權觀察清單」時，該畫面重新掛載也會直接讀到最新磁碟資料。**已驗證**：`taiwan_now()` 與真實 `datetime.utcnow()` 相差確認為8小時；`_fetch_and_cache_etf_symbols([])`／`_fetch_and_cache_options_underlyings([])` 空輸入正確no-op不發網路請求；先前所有 bug#00061 合成資料回歸測試（含股價漂移誤判修正案例）重新執行皆通過；`AssetTrackApp.run_test()` 無頭掛載測試以預先帶入部位跳過登入畫面，確認直接進入Dashboard、`_bg_refresh_timer_started` 旗標正確設為True、應用無崩潰；`py_compile` 全部模組通過。

5. [open] [bug#00062] [function] **部位加碼合併時，空單或反向未平倉的平均成本未更新（僅 new_qty > 0 才計算）**
    * **問題描述**：TUI 部位調整以「新增部位」加碼既有同 broker/account/symbol/類型部位時，若合併後淨部位為空單（`new_qty <= 0`），平均成本完全不更新，維持第一筆的舊成本；空單加碼後成本永遠偏差，連帶未實現損益金額/百分比失真（選擇權因乘數放大更明顯）。多單加碼雖正確，但公式用帶號數量，遇反向操作亦不嚴謹。
    * **root cause**：`DashboardScreen._handle_add_position_result()` 合併分支僅在 `if new_qty > 0`時做加權平均，且以帶正負號的 `quantity` 當權重，未涵蓋空單（負量）同向加碼與反向（部分平倉／翻倉）情境。
    * **solution**：改以「方向判斷 + 絕對數量加權」：同方向加碼一律 `(cost0*|q0| + cost1*|q1|)/|q_new|`（多空皆適用）；反向且已翻倉改採新進場成本；反向未翻倉（部分平倉）保留原方向平均成本。已以單元測試驗證 short+short(-10@190,-10@200→-20@195)、long+long、部分平倉、翻倉四情境皆正確。選擇權新增/OCC 代碼自動生成、乘數（US=100 / TW=50）與市值計算亦一併以測試確認無誤。
    * **fixed by**：v0.0.4-dev（待使用者驗證）

6. [open] [bug#00063] [UI] **「近期重大事件」頁面仍顯示已過去的月份事件（如六月），未以當月置頂**
    * **問題描述**：使用者進入重大事件頁面，仍看到已過去（如六月）的財報/總經事件；使用者要求置頂應為「當月」狀況，過去事件不再顯示。
    * **root cause**：`UpcomingEventsScreen.run_calendar_fetch()` 與 `DashboardScreen._fetch_upcoming_events_worker()` 皆以 `start_date = today - timedelta(days=30)` 及 `get_upcoming_macro_events(..., start_days_ago=30)` 回溯 30 天，刻意納入過去一個月的事件。
    * **solution**：兩處視窗起點改為 `start_date = today`、`start_days_ago=0`，只保留今日(含)以後的事件；月份分組升冪排序後，最早的一組即為當月，自然置頂。財報與總經事件皆套用同一過濾。
    * **fixed by**：v0.0.4-dev（待使用者驗證）

7. [open] [bug#00064] [function] **依使用者需求完全移除「歷史績效」(PerformanceHistoryScreen) 功能並重編號快速鍵**
    * **問題描述**：使用者指示「歷史績效」項目全部移除。
    * **root cause**：`PerformanceHistoryScreen`（~344 行）、快速鍵 `4`、側欄選項 `history`、`on_option_list_option_selected` 的 `history` 分支、`action_performance_history()` 與 `scripts/verify_tui.py` 的對應測試/import 皆仍存在。
    * **solution**：移除上述全部程式碼；數字快速鍵改為連續 1–7（4=近期重大事件、5=儲存快照、6=主動式ETF、7=期權觀察清單），同步更新首頁卡片「按 N」提示文字、action docstring、`scripts/verify_tui.py`（刪除 `verify_performance_history_screen`、修正按鍵、移除 import/stub）與 `README.md`（鍵表、Mermaid 圖節點、功能說明、CLI 殘留描述）。`assettrack/tui.py` 語法檢查通過、`scripts/verify_tui.py` 12 項中 11 項通過（唯一失敗為沙盒檔案權限限制，非程式碼問題）。備註：`shared.draw_history_chart()` 現已無 TUI 呼叫點（僅 verify 匯入），為避免非需求範圍改動暫予保留，待使用者決定是否一併移除。
    * **fixed by**：v0.0.4-dev（待使用者驗證）

8. [open] [bug#00065] [review] **ETF／期權觀察清單結論僅「描述現象」，未連結使用者實際部位方向，對 summary page 的「建設性投資建議」目標仍存在盲點**
    * **問題描述**：使用者指出觀察清單判斷邏輯的目標是於 summary page 提供「建設性投資建議」，要求審查是否仍有盲點。審查 `analysis.generate_etf_conclusions` 與 `options_analysis.generate_options_conclusions` 後確認：兩者輸出皆為純描述（「N 檔 ETF 同步增碼 X」「未平倉量 +500 口、價格 +30%」「偏多/偏空」），未提出 any 行動建議，也未與使用者自身持倉／多空方向交叉比對。
    * **root cause**：盲點如下：(1) **零部位感知**——skew（買權堆疊=偏多／賣權堆疊=偏空）已算出卻從未與使用者對該標的的多空立場比對，例如使用者持有多頭買權而機構賣權建倉升溫時，系統不會提示方向衝突；(2) **只描述不建議**——結論句缺少「因此可考慮…」的建設性語句；(3) **未平倉建倉方向具歧義**——OI 增加無法區分開倉/平倉、買方/賣方，直接標記 call-OI 堆疊為「多方增溫」可能誤導（亦可能是賣方賣出買權=偏空）；(4) **僅涵蓋已持有標的**——無法對尚未持有但值得進場的名稱提出觀察（雖為 bug#00061 設計，但仍是覆蓋盲點）；(5) 28–60 DTE ±15% 視窗於低流動性標的易致比對合約過少而結論空白。
    * **solution**：經使用者確認後實作（部位感知建設性建議）：(1) `shared.py` 新增 `position_stance_by_symbol(positions)`，依持倉判斷各標的淨多空立場（股票/ETF 依數量正負；選擇權 long call/short put→看多、long put/short call→看空；同標的多筆累加，得「多/空/混合」）。(2) `generate_options_conclusions()` 與 `generate_etf_conclusions()` 新增選填 `positions` 參數：對每個 skew／共識標的，與使用者立場方向交叉比對後附上建設性語句——「與你目前偏X的部位方向一致」／「⚠️ 與你目前偏X的部位方向相反，留意反向風險/是否調節」／「你尚未持有，可留意是否符合進場條件」。(3) 降低方向誤導：期權 skew 用語由「可能反映多方部位增溫」的斷言改為中性「資金偏多/偏空關注」，並附註「未平倉增減無法區分開/平倉，方向僅供參考」。(4) 呼叫端：Dashboard 兩張 summary 卡片（`_build_etf_conclusions_panel`、`_build_options_flow_panel`）與 `OptionsWatchlistScreen._run_analysis` 均傳入實際持倉；未傳入時（如 `AdvancedAnalysisScreen`，其上層 `ActiveETFsScreen` 未帶持倉）維持原純描述行為，向後相容。已以單元測試驗證多/空/混合/未持有四情境（含股票、選擇權 long put、short 股票）之對齊與衝突提示皆正確，`verify_tui.py` 11/12 通過（唯一失敗為沙盒檔案權限）。備註盲點 (4)（僅涵蓋已持有標的）與 (5)（低流動性視窗合約過少）維持現況，屬 bug#00061 設計範圍，如需擴充另立需求。
    * **fixed by**：v0.0.4-dev（待使用者驗證）


9. [open] [bug#00066] [newfeature] **期權觀察清單重構：標的自管理 + 價內外≤60天合約希臘字母 + 「排除股價變動因素」的異常震盪/背離投資建議**
    * **問題描述**：使用者要求將「期權觀察清單」升級為三部分：(1) 讓使用者自行新增/刪除觀察標的，介面直接載入當下清單供增刪；(2) 左選標的、右顯示該標的價內外、60 天內合約的「未平倉量」與「IV / Delta / Gamma / Theta / 損益兩平」；(3) 選擇權投資建議須根據每日紀錄的上述資訊計算變化，並「排除當日股價變動因素」後，找出超出股價變動的期權異常震盪或與股價背離的狀況。經使用者確認：標的來源=持倉自動帶入 ∪ 使用者額外新增（持倉標的不可刪）；增刪粒度=標的(ticker)層級；希臘字母無風險利率取 ^IRX。
    * **root cause**：舊 `OptionsWatchlistScreen` 觀察標的完全由 `_underlyings_from_positions()` 自動決定、無法自訂；右欄僅顯示 OI/價格變化事件，不含任何希臘字母（yfinance 不提供 Delta/Gamma/Theta）與損益兩平；結論僅偵測 OI 建倉/價格漲跌，未做「扣除標的價格變動後」的殘差分析，無法區分「純跟隨股價」與「波動率/事件驅動」或「與股價背離」。
    * **solution**：(1) **標的自管理**：`storage.py` 新增 `load/save_options_watchlist(user)`（存 `{user}_options_watchlist.json`）；`tui.py` 新增 `_watchlist_underlyings()` = 持倉標的 ∪ 自訂標的（持倉標的標 📌 不可刪、自訂標 ➕ 可刪）；畫面新增 `a 新增標的`(AddTickerModal 輸入代碼)、`d 刪除標的`(RemoveTickerModal 僅列自訂標的)，`AssetTrackApp` 的每 30 分背景抓取也改用 `_watchlist_underlyings` 以便自訂標的同樣逐日累積快照。(2) **希臘字母**：新檔 `assettrack/greeks.py` 以標準 Black-Scholes 就地計算 delta/gamma/theta(每日)/vega + 損益兩平，IV 缺失時回 None（畫面顯示「—」），已對照教科書值驗證(S=K=100,T=1,r=5%,IV=20%→Δ0.6368/Γ0.0188/Θ-0.0176/day)；`quotes.fetch_risk_free_rate()` 抓 ^IRX(÷100，6 小時快取，失敗回退 4%)；`quotes.fetch_options_snapshot` 範圍由 28–60DTE/±15% 改為 1–60DTE/±20% 以涵蓋價內外；`options_analysis.build_contract_view()` 由最新快照計算每張合約 OI/IV/Δ/Γ/Θ/損益兩平/價內外，右欄改為 9 欄合約表（單日快照即可顯示，不需 ≥2 天）。(3) **排除股價變動的訊號**：`options_analysis.compute_iv_divergence()` 取視窗內最早/最新兩筆真實快照、以 contractSymbol 精確配對，計算 ΔS、ΔP、預期=delta0×ΔS、殘差=ΔP−預期(扣除股價變動)、ΔIV；殘差達 |≥$0.15 且 ≥25%×起始權利金| 判為「異常震盪(波動率/事件驅動)」，ΔP 與預期方向相反且雙方量值皆有意義判為「背離」；`generate_divergence_conclusions()` 產生中文建議並沿用 bug#00065 的 `_stance_note` 附上部位方向提示。缺最早日 IV 無法估 delta0 者一律跳過(不臆測)。已驗證：greeks 對照教科書值；`compute_iv_divergence` 以合成兩日快照正確分別觸發震盪(殘差+1.92)與背離(股漲權利金跌)；Textual `run_test()` 無頭掛載按 7 進入畫面、右欄希臘字母表 2 列×9 欄正確、`a` 新增 NVDA 持久化、`d` 移除、持倉標的 AAPL 不可刪、Esc 返回皆正確；`scripts/verify_tui.py` 11/12 通過(唯一失敗為沙盒檔案權限)。Dashboard 首頁「🎯 期權觀察結論」卡片維持既有 OI-flow(bug#00065 部位感知)輸出不變。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需在有網路環境實際執行 TUI，按 7 進入期權觀察清單，確認右欄希臘字母/損益兩平顯示、a/d 新增刪除標的、以及數天累積後「投資建議」是否正確標出異常震盪與背離）


10. [open] [bug#00067] [function] **首頁「期權觀察結論」卡片與期權頁面投資建議不對齐：卡片用 OI-flow、頁面用 IV 異常/背離，兩套不同邏輯（且 docstring 誤稱共用同一份輸出）**
    * **問題描述**：使用者要求首頁 summary 卡片與期權頁面的投資建議「對齐、同頁一致」。稽核發現 bug#00066 重寫頁面改用 `compute_iv_divergence`（異常震盪/背離）後，首頁 `_build_options_flow_panel` 仍停留在 `compute_options_flow`（未平倉建倉 skew），兩處是不同訊號、可能彼此無關甚至看似矛盾；且卡片 docstring 仍宣稱「與頁面共用同一份 generate_options_conclusions() 輸出」，已與現況不符。經使用者決策：**兩處都同時顯示兩套訊號**（異常震盪/背離 + 未平倉建倉 skew），並共用同一產生器對齐。
    * **root cause**：bug#00066 只改了頁面（`OptionsWatchlistScreen._run_analysis` 改用 divergence），未同步更新首頁卡片，兩者各自呼叫不同的結論產生器；且卡片僅以持倉標的（`_underlyings_from_positions`）為範圍，與頁面的「持倉 ∪ 自訂清單」（`_watchlist_underlyings`）也不一致。
    * **solution**：(1) `options_analysis.py` 新增 `generate_combined_options_advice(flow_report, div_report, positions, top_divergence, top_flow)`——先輸出 divergence（🌀 異常震盪／↔️ 背離），再輸出 OI-flow（🎯 建倉事件／📈📉 skew），兩類 emoji 不同易辨；頁面與卡片皆改呼叫此同一函式，只差取用的 top_n（卡片少、頁面多），確保「共用同一份輸出」重新成立。(2) `OptionsWatchlistScreen._run_analysis` 同時計算 `compute_options_flow` 與 `compute_iv_divergence`，結論區改顯示兩套合併訊號；標題更新為「異常震盪/背離 + 未平倉建倉」。(3) `_build_options_flow_panel` 改用 `_watchlist_underlyings`（與頁面同一標的集合）、同時算兩套 report、以 `generate_combined_options_advice(top_divergence=1, top_flow=1)` 濃縮，並修正誤導的 docstring。(4) 無風險利率對齐且不阻塞 UI：`quotes.py` 新增非阻塞的 `cached_risk_free_rate()`（只讀模組快取、不發網路），首頁卡片用它；`DashboardScreen._do_refresh_worker`（背景執行緒）呼叫 `fetch_risk_free_rate()` 暖化同一份模組快取，使卡片與頁面最終用同一個 ^IRX 值。已驗證：合併產生器單元測試同時輸出 🌀/↔️ 與 🎯/📈 兩類；Textual `run_test()` 無頭實測——首頁卡片與頁面結論區皆同時含「異常震盪」與「未平倉/skew」字樣（同源濃縮，卡片為頁面子集），頁面希臘字母表 2 列×9 欄正常；`scripts/verify_tui.py` 11/12 通過（唯一失敗為沙盒檔案權限）。
    * **fixed by**：v0.0.4-dev（待使用者驗證）


11. [open] [bug#00068] [newfeature] **期權投資建議升級：IV 位階(相對歷史) + 財報感知過濾(避免把 IV crush/run-up 誤判為獨立訊號)**
    * **問題描述**：使用者要求評估系統能否提供有洞見的投資建議，經評估後採納兩項槓桿最高、且用現有真實資料即可做的改善：(1) **IV 位階**——「IV +15pt」若不知現在 IV 相對自身歷史是高是低，就無法判斷選擇權貴/便宜（期權決策最關鍵一條）；(2) **財報感知**——財報後 IV 崩跌(crush)造成「股價漲、買權跌」是可預期的正常現象，但原 divergence 會把它標成「背離/異常」假訊號；系統雖有財報行事曆，期權訊號卻沒用它。
    * **root cause**：(1) 每日 IV 快照已在累積，但從未拿來算相對位階。(2) yfinance 只可靠提供「下次(未來)財報日」，無法直接得知「財報剛過」，故 divergence 分析先前完全沒有財報維度，post-earnings IV crush 會被誤判。
    * **solution**：(1) **IV 位階**：`options_analysis.compute_iv_percentile()` 取每日近價平(±10%)合約 IV 平均為當日代表 IV，算最新值在累積歷史中的百分位；樣本 < 8 天誠實回 ready=False 不強行顯示。頁面左欄新增「IV位階」欄(≥70 紅=偏貴、≤30 綠=偏便宜)，建議 bullet 亦附「IV 位階第 N 百分位（近 M 日，偏高/偏低…）」。(2) **財報感知**：於**每日快照抓取時**把「當時已知的下次財報日」記入 snapshot（`storage.append_options_daily_snapshot` 新增 `earnings_date`、`quotes.fetch_next_earnings_dates` 薄包裝既有 `fetch_earnings_calendar`、`_fetch_and_cache_options_underlyings` 一併寫入）——如此即使財報已過，過去快照仍留有該日期可供判定（解決 yfinance 只給未來日的限制）。`compute_iv_divergence` 掃描視窗內快照記錄的財報日，若落在 [first−2, last+3] 內則事件標 `near_earnings` 並降權排序(非財報訊號優先)；`generate_divergence_conclusions` 對此類事件明確註明「⚠️ 區間含財報(日期)，IV/權利金劇變多屬財報預期反應，非獨立訊號」。(3) 頁面與 Dashboard 卡片皆透過既有共用產生器 `generate_combined_options_advice(iv_pct_by_underlying=…)` 取得同一份輸出，維持 bug#00067 的對齊。已驗證：`compute_iv_percentile` 遞增序列最新值→第100百分位、樣本不足→ready=False；財報跨區間案例(IV 0.55→0.30 crush、股價+3%)正確標 near_earnings 並降權註明，移除 earnings_date 後不再標記；Textual `run_test()` 無頭實測頁面左欄「IV位階」欄正確、iv_pct 計算正確；`scripts/verify_tui.py` 11/12 通過(唯一失敗為沙盒檔案權限)。**已知限制/取捨**：IV 位階需累積 ≥8 天真實快照才顯示；財報感知需系統已運行、把財報日隨快照記錄一段時間後，才對「已過財報」生效(上線初期只對『即將到來』的財報有效)——皆為避免以假資料填補缺口的刻意設計。尚未實作的建議項(投資組合淨 Greeks、訊號回測校準)另待需求。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需累積數週真實快照後，確認頁面左欄「IV位階」顯示合理、財報前後的期權劇變被正確標為「非獨立訊號」）


12. [open] [bug#00069] [newfeature] **投資組合層級淨 Greeks + 同步漲跌情境（把整個帳戶的方向性/波動率敞口彙總成美元）**
    * **問題描述**：使用者評估「能否提供洞見」時同意，對一個股票+選擇權的帳戶,真正有用的是**全帳戶淨敞口**(net Delta/Theta/Vega + 情境),而非單張合約的瑣碎數字。原系統只有單一合約 Greeks,缺投資組合層級視角。
    * **root cause**：從未彙總部位層級 Greeks;且要算選擇權 Greeks 需要標的現價與 IV,而持倉只有選擇權權利金(market_price)、沒有標的現價,IV 也未儲存。
    * **solution**：(1) `greeks.py` 新增 `bs_price()` 與 `implied_vol()`(二分法)——用**已有的真實權利金反解 IV**,只需再補抓標的現價,不必依賴 yfinance 的 IV 欄位。(2) `options_analysis.compute_portfolio_greeks(positions, spot_by_underlying, r)` 以美元計彙總(可跨標的相加):股票 delta$=數量×現價;選擇權 delta$=delta×數量×乘數×標的現價、theta/日、vega/1pt、gamma_cash(供二階);情境 P&L(所有標的同步 ±5%/±10%)≈ delta$·m + ½·gamma_cash·m²(含凸性)。缺現價/無法反解 IV 的部位計入 unpriced 誠實揭露。(3) `OptionsWatchlistScreen` 頂部新增「📐 投資組合淨 Greeks」面板;背景worker以既有 `fetch_prices_batch()` 補抓選擇權標的現價(`_refresh_underlying_spots`)存 `self.spot_by_underlying`。已驗證:`bs_price`↔`implied_vol` 往返能還原 IV(0.30→0.30);合成帳戶(股票+多頭買權+空頭買權)算出淨 delta$、theta<0(淨買方時間衰減)、vega>0、情境呈正 gamma 凸性(+10% 獲利 > -10% 虧損);Textual `run_test()` 無頭實測面板正確渲染;`scripts/verify_tui.py` 11/12 通過(唯一失敗為沙盒檔案權限)。**已知取捨**:反解 IV 需標的現價(每次刷新補抓一次,批次以控制請求量);情境為 delta+gamma 二階近似、且假設所有標的同步等比例變動(未用個股 beta),屬快速估算而非精確 VaR;無法定價的部位誠實列為 unpriced 不臆測。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需在有網路環境開啟期權觀察清單，確認頂部「投資組合淨 Greeks」的 Delta$/Theta/Vega 與情境數字合理）


13. [open] [bug#00070] [newfeature] **訊號回測校準（walk-forward）+ 隨時可查的校準狀態畫面**
    * **問題描述**：使用者指出系統的訊號從未被驗證(無回測、無 base rate、無校準)，要求「訊號回測校準先寫好邏輯，讓使用者可以隨時知道校準狀態」——即使目前真實資料尚未累積，也要先把邏輯與狀態顯示就緒。
    * **root cause**：先前所有訊號(方向性 skew、異常震盪/背離)只在當下計算顯示，從未回頭用累積的真實快照檢驗其預測力，使用者無從判斷可信度。
    * **solution**：(1) 新檔 `assettrack/calibration.py`：`backtest_directional_signals()` 以 **walk-forward、純離線**方式回測——對每個標的、每個歷史日 T，只用 ≤T 的快照透過**與畫面同一套** `compute_options_flow(as_of=T)` 重新推導當日方向性 skew(偏多/偏空)，再看該標的在 T 之後約 5 個日曆日的真實 spot 報酬是否與訊號方向一致(命中)，彙總偏多/偏空命中率、平均前瞻報酬，並與「無條件前瞻上漲日比例(baseline)」比較得出 edge；`calibration_status_label()` 依可評估訊號數給出「尚無可評估訊號(資料累積中)／初步樣本(n<門檻)／可參考」。刻意用 `as_of` 切窗避免前視偏誤,樣本不足(<20)時明確標示僅供參考、不下結論。(2) `tui.py` 新增 `CalibrationScreen`(背景執行緒跑回測、顯示資料累積量/日期範圍、方法、可評估訊號數、baseline、偏多/偏空命中率與 edge、平均前瞻報酬、樣本不足警示、以及自相關/日曆日/僅校準方向性訊號等誠實註記)；於期權觀察清單頁面新增鍵 `k` 開啟(header 提示同步加上「k 校準」)。(3) 修正一個 Textual 陷阱：方法原命名 `_render` 與 Textual `Widget._render()` 衝突導致渲染崩潰,已改名 `_show_report`。已驗證：合成 25 日(上升+買權OI建倉)資料——baseline_n 與可評估訊號數、命中率、ready 門檻閘門皆正確;空資料回 evaluated=0、狀態「資料累積中」;Textual `run_test()` 無頭實測按 7→按 k 進入校準畫面、面板正確渲染(含初步樣本警示與空狀態);`scripts/verify_tui.py` 11/12 通過(唯一失敗為沙盒檔案權限)。**設計說明/限制(誠實顯示於畫面)**：連續多日對同一標的的訊號高度自相關,命中率略樂觀,需大樣本;前瞻以日曆日計、系統沒開的日子無快照;目前僅校準「有明確方向主張」的 skew 訊號,異常震盪/背離本身非方向性預測、ETF 共識校準另待擴充;剛上線必為「資料累積中」——這正是要讓使用者隨時看見的狀態。
    * **fixed by**：v0.0.4-dev（待使用者驗證：按 7 進期權觀察清單、再按 k 開啟「訊號回測校準狀態」，確認狀態顯示；需累積數週真實快照後命中率與 edge 才會逐步出現）


14. [open] [bug#00071] [function] **淨 Greeks 應逐標的分開計算，而非全部混成一個總數（INTC 的期權只算 INTC，不涵蓋 MU/AMD）**
    * **問題描述**：使用者反映 bug#00069 的「投資組合淨 Greeks」把所有標的混成單一組數字,不符需求;應對每一個個股期權各自跑一次計算,例如 INTC 的 buy call 就只算 INTC 自己,不與 MU/AMD 混在一起看。
    * **root cause**：`compute_portfolio_greeks()` 一次把所有部位加總成單一 net delta$/theta/vega,只保留 `per_underlying` 的 delta$ 而無各標的完整 Greeks;面板 `_render_portfolio()` 亦只顯示混算後的總數與「所有標的同步」情境。
    * **solution**：(1) `options_analysis.py` 重構:抽出 `_greeks_for_group()` 計算單一標的(同 underlying 的股票+選擇權)自身淨 Greeks;`compute_portfolio_greeks()` 改為先依 underlying 分桶(股票用自身代碼、選擇權用其 underlying),每檔只用自己的部位與自己的現價計算,回傳 `{"by_underlying": {sym: {delta$/theta/vega/gamma_cash/scenarios/priced/unpriced/has_options}}, "total": {...}}`;情境改為「該標的自身 ±%」而非全體同步。(2) `tui.py` `_render_portfolio()` 改用 Rich Table 逐標的一列(標的／Delta$／Θ日／Vega／自身-10%／自身+10%),依 |Delta$| 由大到小、最多 8 檔,末列為清楚標示的「— 投組合計 —」(僅供參考、非混算);無選擇權的純現股標註「(僅現股)」,有無法定價部位標 `*` 並於註腳說明。已驗證:合成 INTC(股票+買權)/MU(買權)/AMD(空頭買權)——三檔各自獨立計算(INTC 只含自身股票+買權、不含 MU/AMD),AMD 空頭買權正確呈現負 Delta$/正 Theta;Textual `run_test()` 無頭實測面板逐標的表格 + 合計列正確渲染;`scripts/verify_tui.py` 11/12 通過(唯一失敗為沙盒檔案權限)。
    * **fixed by**：v0.0.4-dev（待使用者驗證：開啟期權觀察清單,確認頂部「各標的淨 Greeks」為每檔一列、彼此不混算,且情境為該標的自身漲跌）


15. [open] [bug#00072] [function] **期權觀察清單頁的淨 Greeks 應只分析選擇權部位，不因使用者持有現股就把現股算進來**
    * **問題描述**：使用者指出此頁為「期權分析」,淨 Greeks 不應考慮持倉是現股或選擇權而回頭分析現股;應全部以選擇權方式分析,只計算選擇權部位。
    * **root cause**：`compute_portfolio_greeks()` 會把股票/ETF 部位一併納入(股票 delta$=數量×價),導致期權分析頁把使用者的現股曝險也算進來。
    * **solution**：`compute_portfolio_greeks()` 新增 `options_only` 參數;為 True 時分桶階段直接略過股票/ETF 部位,只保留選擇權。`OptionsWatchlistScreen._render_portfolio()` 以 `options_only=True` 呼叫,面板標題改「選擇權淨 Greeks（僅選擇權部位…）」、合計列改「— 選擇權合計 —」、無選擇權部位時顯示「你目前沒有任何選擇權部位（此頁為期權分析,現股不在此計算）」。已驗證:INTC(股票100股+買權)於 options_only 下 delta$ 僅含買權($20,177,不含 $3,100 現股),純現股標的(AAPL)不出現;`scripts/verify_tui.py` 11/12 通過(唯一失敗為沙盒檔案權限)。
    * **fixed by**：v0.0.4-dev（待使用者驗證：期權觀察清單頂部淨 Greeks 應只含選擇權部位,現股不計入）


16. [open] [bug#00073] [newfeature] **新增「預期波動區間」+ 頂部分析表改以觀察清單標的為列（修正新增標的後上方不同步）**
    * **問題描述**：(需求)使用者要求開發「預期波動區間」。(bug)使用者在期權觀察清單按 `a` 新增標的後,只有左下角清單多了該標的,上方的淨 Greeks 面板沒有同步出現該標的。
    * **root cause**：預期波動未實作;且原上方面板以「使用者選擇權部位」為列(bug#00072),新增的觀察標的若使用者沒有該標的選擇權部位,自然不會出現→與使用者「新增標的應在上方看到」的預期不符;且 `_handle_add_ticker` 只呼叫 `_render_list()`,未重跑上方分析。
    * **solution**：(1) `options_analysis.compute_expected_move()`：由最新快照取「最近一個同時有價平買權與賣權」的到期日,價平跨式價 × 0.85 估算 ±1σ 預期波動($與%),另回傳 DTE 與 ATM IV,100% 用真實權利金。(2) `_render_portfolio()` 重寫為**以觀察清單標的為列**的「各標的期權分析」總表:每列固定顯示預期波動、ATM IV(來自期權鏈,與是否持倉無關),若持有該標的選擇權則多顯示持倉 Δ$/Θ/Vega(◆ 標記,僅選擇權、逐標的),末列為持倉選擇權合計。如此新增觀察標的必定多一列。(3) `_handle_add_ticker` 改呼叫 `_run_analysis()`,新增後上方表立即多一列(新標的先顯示「資料收集中」,背景抓完自動補上預期波動/IV/Greeks)。已驗證:INTC(跨式 1.6+1.4=3.0×0.85)正確顯示 ±$2.55(±8.5%,33d)、ATM IV 41%、持倉 Δ$;新增 MU 後上方即時多一列「資料收集中」;`compute_expected_move` 正確跳過只有買權無賣權的近月;`scripts/verify_tui.py` 12/12 通過。
    * **fixed by**：v0.0.4-dev（待使用者驗證：按 7 進期權觀察清單,確認頂部總表每檔顯示預期波動±，且按 a 新增標的後上方立即多一列）


18. [open] [bug#00075] [UI] **期權觀察清單右欄合約明細表排序混亂（履約價高低跳動）**
    * **問題描述**：合約明細表列出各合約時，上下排列的履約價高低不一，難以閱讀對比。
    * **root cause**：`build_contract_view()` 舊排序鍵為 `(type, expiry, strike)`，先依買/賣權分組再依到期，導致同一到期日的合約與不同到期日的合約交錯，視覺上價格跳動。
    * **solution**：排序鍵改為 `(strike, expiry, type)`，合約由低履約價到高履約價連續排列，同履約價才再依到期日、類型排序。並同步更新 docstring 說明。
    * **fixed by**：v0.0.4-dev


20. [open] [bug#00077] [UI] **期權觀察清單右欄買權/賣權混排，難以比較同履約價的兩側資訊**
    * **問題描述**：合約明細表中 Call 與 Put 混排在同一欄，使用者無法快速對比同一履約價的買權與賣權。
    * **root cause**：`_render_greeks()` 只用一張 `DataTable`，未拆分 Call/Put。
    * **solution**：`#ow-right-col` 改為水平佈局，左側 `#ow-calls-col`（買權，綠框）、右側 `#ow-puts-col`（賣權，紅框），各有獨立 DataTable（`#ow-calls-table` / `#ow-puts-table`）。`_render_greeks()` 拆成兩份各自填入；列標題「合約」改為「履約價」（類型已由欄框隱含）。help text 第四區同步更新。
    * **fixed by**：v0.0.4-dev


19. [open] [bug#00076] [newfeature] **期權觀察清單缺乏各欄位說明頁，進階數值（Greeks、預期波動、IV位階等）對一般使用者不易理解**
    * **問題描述**：使用者要求開一頁 help page，解釋期權觀察清單所有數值的意義，並附簡易說明與舉例。
    * **root cause**：功能缺失。
    * **solution**：新增 `OptionsHelpScreen`（六區塊：左欄清單、頂部分析總表、投資建議、右欄合約明細、快速鍵、資料來源與限制），每項均附白話說明與數值範例。於 `OptionsWatchlistScreen` 新增 `h` 鍵綁定，Footer 顯示「h 說明」入口。
    * **fixed by**：v0.0.4-dev


17. [open] [bug#00074] [function] **`scripts/verify_tui.py` 的 empty_positions 測試以 `os.remove('data/…')` 清檔,在唯讀/沙盒檔案系統下拋 PermissionError 導致長期 11/12**
    * **問題描述**：使用者多次看到 `verify_tui.py` 只過 11/12、唯一失敗為「Operation not permitted: data/testuser_positions.json」,要求修掉這個小 bug。
    * **root cause**：`verify_empty_positions_onboarding_path()` 以硬寫相對路徑 `data/testuser_positions.json` 並 `os.remove()` 做前置清檔——(1) 在沙盒/唯讀掛載下 `os.remove` 會拋 PermissionError,整個測試在還沒跑到斷言前就崩;(2) 亦會對真實 `data/` 目錄產生副作用。
    * **solution**：改為 hermetic 測試——以 `patch("assettrack.storage.get_data_dir", return_value=<tempdir>)` 把資料目錄導向全新臨時目錄,保證起始為空、且不刪除/不碰真實 `data/`,也不再依賴能否 unlink 檔案。移除脆弱的 `os.remove`。已驗證 `scripts/verify_tui.py` 由 11/12 → **12/12** 全數通過。
    * **fixed by**：v0.0.4-dev

20. [open] [bug#00077] [function] **「預期波動 / ATM IV」資料品質與語意修正：改用中間價、修正 ±1σ 定義、期限改 ~30 DTE**
    * **問題描述**：`compute_expected_move`（options_analysis.py:622）與其顯示（tui.py:_render_portfolio）有三個品質問題：(1) 價格用冷門合約可能早已過期的 `lastPrice`，使跨式價/ATM IV/預期波動一起失真；(2) 把「ATM 跨式價×0.85」標成「≈±1σ」並不嚴謹，跨式價其實是到期損益兩平區間，與 1σ 意義不同卻被混用；(3) 到期日永遠取「最近到期」（配合 bug#00066 把 `fetch_options_snapshot` 由原本 bug#00061 的 28–60 天放寬成 1–60 天），可能顯示只剩 1–7 天、被 Gamma/財報/週選嚴重放大的短天期。
    * **root cause**：(1) `fetch_options_snapshot`（quotes.py）原始快照只存 `lastPrice`，沒有 bid/ask 或報價時間戳，計算層無中間價可用；(2) `compute_expected_move` 直接以 `straddle × factor(0.85)` 當預期波動並在 UI 標為「≈±1σ」；(3) 該函式以 `sorted(by_exp)` 取第一個（=最近）到期日。
    * **solution**：**(A) 資料層**（quotes.py:1010）：contract dict 補存 `bid`、`ask`、`lastTradeDate` 三個 yfinance 真實欄位（舊快照無此欄位時計算層自動退回 lastPrice，向後相容）。**(B) 計算層**（options_analysis.py）：新增 `_quote_mid()` 優先用 `(bid+ask)/2` 中間價，無雙邊報價或價差過寬（>30%）時退回 `lastPrice` 並標記 `low_confidence`；重寫 `compute_expected_move()` 主數值改為 `sigma_abs = spot × ATM_IV × √(DTE/365)` 的 ±1σ（年化、無方向），跨式價另以 `breakeven_abs` 分開回傳（到期損益兩平區間，不與 σ 混用），缺 IV 時退回跨式×0.85 近似並標記低可信度；到期日改選「DTE 最接近 target_dte(預設30)」而非永遠最近。**(C) 顯示層**（tui.py:_render_portfolio）：欄位改「預期波動 ±1σ (~30 DTE)」，同格分列「損益兩平」，低可信度以 `⚠` 標記，footer 說明同步改寫。已驗證：三檔 `py_compile` 通過；以合成資料 6 案例測試 `compute_expected_move`（中間價採用、30DTE 選擇、σ 公式、lastPrice 退回標記、價差過寬標記、無 IV 退回、ATM 取最近現價履約價、資料不足回 None）全數通過。**已知取捨**：此修正上線前累積的舊快照沒有 bid/ask，短期內會全部走 lastPrice 退回路徑並標為低可信度，需等新格式快照累積後中間價才會生效——刻意選擇，非 bug。
    * **微調（同日，一致性審查）**：使用者指出三處殘留不一致並要求修正。(1) **說明頁**（tui.py `_OPTIONS_HELP_TEXT`, 約 line 4611）仍寫「最近到期、跨式×0.85、≈±1σ」，與新行為矛盾——改寫為「~30 DTE、現價×ATM IV×√(DTE/365) 的 ±1σ、損益兩平另列、低可信度 ⚠、中間價優先」的正確說明。(2) **`lastTradeDate` 已存但未使用**：新增 `_business_days_between()` 與 `_last_trade_stale()`，`_quote_mid()` 增 `as_of` 參數——當 lastPrice 成交時間超過 1 個交易日（以交易日計，週末不算，故週五成交/週一快照仍算新鮮）即不再當 fallback，回 (None, True) 讓上層視為資料不足而非顯示過時價；無 lastTradeDate 的舊快照維持原相容行為（可用但標低可信度）。(3) **ATM IV 欄未帶品質警示**：tui.py `_render_portfolio` 的 `iv_s` 改為 `41%⚠`，與預期波動欄共用同一 `low_confidence`，兩欄品質訊號一致。已驗證：三檔 `py_compile` 通過；合成資料補測 `_business_days_between`（Fri→Mon=1、Fri→Tue=2）、`_last_trade_stale`（週末新鮮/2交易日過期/None→False）、過期 lastPrice 被丟棄回 None、bid/ask 存在時忽略過期成交價、無 lastTradeDate 舊快照相容——全數通過。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需在有網路環境下實際執行 TUI，讓新格式快照（含 bid/ask、lastTradeDate）累積後，確認「期權觀察清單」總表的預期波動欄顯示 ±1σ 為主、損益兩平分列、低可信度 `⚠` 同時出現在預期波動與 ATM IV 兩欄、到期日接近 30 天、且過期成交價的合約顯示資料不足而非過時數字；說明頁（h）文字與實際行為一致）

21. [open] [bug#00078] [newfeature] **類股板塊分析 (sector_analysis)：預設板塊群組 + 市值加權漲跌排序 + 每日累計「廣度共識」偵測（核心骨架）**
    * **問題描述**：使用者要求新功能「類股板塊分析」，含 8 項需求：(1) 內建預設板塊群組，顯示各群組當日/當週/當月市值漲跌%並依當日%排序（漲最多置頂、跌最多置底）；(2) 預設群組含 CPU、功率半導體、光通訊、存儲記憶體(HBM/DRAM)、SaaS、科技七巨頭等；(3) 使用者可自建群組並增刪成分股；(4) [key] 每日累計追蹤各板塊是否「普遍」上漲/下跌，抓出市場對特定類股族群的共同買進上漲/共同賣出下跌（演算法由 AI 提議）；(5) 參考 finguider 檢視自訂清單是否遺漏/多餘；(6) 資料每日刷新；(7) summary dashboard 開對話框顯示滿足 item#4 條件者，另獨立開類股板塊分析頁；(8) 內層頁面左半邊板塊項目（總市值、當日加權漲跌比例）、右半邊成分股（現價/收盤價/漲跌%/成交量/成交額）。
    * **root cause**：全新功能，系統原無任何板塊/類股族群層級的資料收集、儲存或分析機制。
    * **solution**：**經與使用者確認採「先做核心骨架再擴充」分期交付**，本次完成核心骨架（涵蓋 item 1,2,4,6,8）；item 3(自建群組編輯)、5(finguider 比對，使用者指定參考 https://finguider.cc/concept-list 美股概念清單)、7(Dashboard 卡片) 列為擴充階段。**演算法（item#4，AI 提議並經使用者採納）**：廣度擴散指數 + 持續性過濾——(a)當日廣度 breadth=(#漲−#跌)/#有報價，範圍 −1…+1；(b)市值加權報酬 Σwᵢ·rᵢ（wᵢ=成分股市值權重，同時即 item#1 的板塊「市值漲跌%」）；(c)當日判「普遍上漲」需 breadth≥+0.5 且加權報酬>門檻（兩訊號需一致，沿用本專案 ETF 分析「兩真實訊號同向」紀律）；(d)每日累計：最近 N 天(預設5)中有 ≥K 天(預設3)同向普遍走勢才於 summary 標記，區分持續性共同買賣與單日雜訊；資料不足誠實回 ready=False（「資料收集中」，不回填/不臆測）。**實作**：(1)`storage.py` 新增 `DEFAULT_SECTOR_GROUPS` 六組預設 + `load/save_sector_groups(user)`（預設疊加使用者自訂）、`sector_cache/history/{group}.jsonl` 逐日真實快照 `append/load_sector_daily_snapshot`、`sector_group_fresh`、`prune_sector_history`，架構完全比照 etf_cache/history/（同日去重、Taiwan 時區、獨立資料夾）。(2)`quotes.py` 新增 `fetch_sector_members_data()`：分批 `yf.download`(1mo) 算 day/week/month% + 現價/收盤/成交量/成交額，市值由 per-symbol `fast_info`（成分股總數少，請求量有界）；缺值一律 None 不捏造。(3)新檔 `assettrack/sector_analysis.py`：`summarize_group`(當日群組視圖)、`compute_breadth_history`、`detect_broad_flow`(持續性偵測)、`generate_sector_conclusions`(中文結論 bullets)，純離線。(4)`tui.py` 新增 `SectorAnalysisScreen`（左板塊表：板塊/總市值/當日加權%/廣度▲▼/週%/月%/共識，依當日加權%排序；右成分股表：Symbol/現價/收盤價/漲跌%/成交量/成交額）+ 模組層級 `_fetch_and_cache_sector_groups(user)`（螢幕無關，供頁面 worker 與 App 每30分背景刷新共用）；DashboardScreen 新增鍵 `8`、側欄「📊 類股板塊分析」、`action_sector_analysis`，並接入既有 `_background_data_refresh`（跨日冪等補抓）。**已驗證**：`py_compile` 四檔通過；合成資料單元測試 `summarize_group`(市值加權=0.1、漲跌計數、排序)、`detect_broad_flow`(普遍漲/跌/資料不足/無共識四情境)、`generate_sector_conclusions`、`storage` 快照 round-trip(同日冪等、自訂群組存取) 全數通過；Textual `run_test()` 無頭掛載（mock `fetch_sector_members_data` 避免真網路）——6 群組列渲染、依當日加權%正確排序、成分股表渲染、方向鍵切換板塊即時更新右欄、Esc 返回皆正確；`scripts/verify_tui.py` 12/12 通過（因新增側欄選項使 `verify_logout_modal` 的向下鍵數需由 6 改 7，已同步修正）。**已知取捨/限制**：item#4 的廣度共識需系統實際每日運行、真實累積 ≥3 天板塊快照後才會顯示（上線初期為「資料收集中」，刻意不以假資料填補）；「市值漲跌%」採市值加權成分股報酬（盤中市值變動%=股價變動%，流通股數不變）；預設群組成分為 curated 常見代表名單、非窮舉，使用者後續可於擴充階段自訂增刪。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需在有網路環境按 `8`／側欄進入「類股板塊分析」，確認左欄板塊依當日市值加權%排序、右欄成分股即時明細、連續運行數日後「共識」欄由「收集中」轉為顯示普遍漲/跌；擴充階段將接續 item 3 自建群組、5 finguider 比對、7 Dashboard 卡片）

22. [open] [bug#00079] [newfeature] **類股板塊分析擴充：成分股佔比、群組可自建/編輯/刪除、成分股週/月漲跌、Dashboard 類股共識建議卡片（接續 bug#00078 擴充階段 item 1/3/7）**
    * **問題描述**：使用者於執行 sector_analysis 後列出四項未完成項目：(1) 內頁板塊成分股需提供「佔比」；(2) 預設板塊只是初步項目、不應寫死於系統，使用者需能自由新增/編輯/刪除板塊與成分股並依載入紀錄還原；(3) 成分股需同時提供當日/當週/當月漲跌；(4) summary page 需新增區塊，依 sector_analysis 主要功能（每日累計追蹤各板塊是否「普遍」漲跌、抓出市場對特定類股族群的共同買進上漲/賣出下跌）提供條列式建議。
    * **root cause**：bug#00078 核心骨架刻意將 item 3(自建群組編輯) 與 item 7(Dashboard 卡片) 列為擴充階段未實作；成分股表僅顯示當日漲跌%、未提供佔比與週/月漲跌；且 `storage.load_sector_groups` 每次以內建 `DEFAULT_SECTOR_GROUPS`「疊加」使用者設定，導致使用者刪除預設群組後下次載入又被重新加回，無法真正刪除。`sector_analysis.generate_sector_conclusions` 雖已備妥中文條列結論函式，但從未被任何畫面接入。
    * **solution**：經與使用者確認兩項設計決策——(a) 預設群組採「首次種子、之後全可編輯」（首次無設定檔時把六組預設寫入使用者檔當起點，之後使用者檔為唯一依據，刪除的預設不再回來，空設定 `{}` 亦為合法權威狀態）；(b) 佔比採市值權重、漲跌一律以 % 呈現。**實作**：(1) **item#1 佔比**：`sector_analysis.summarize_group` 為每檔成分股計算 `weight = 市值 / 板塊總市值 × 100`（缺真實市值者 None，總市值 0 時全 None），與模組既有市值加權口徑一致。(2) **item#2 可編輯群組**：`storage.load_sector_groups` 改為「設定檔存在則權威回傳（不再疊加預設）、不存在才種子預設並寫檔」；`tui.SectorAnalysisScreen` 新增 `a/e/d` 鍵與 `SectorGroupModal`（名稱＋成分股空白/逗號分隔輸入，自動大寫去重；編輯模式含「刪除板塊」鈕），存檔後 `save_sector_groups`＋重載群組順序＋重新背景抓取；改名以「刪舊鍵＋寫新鍵」處理，全刪後右欄清空並提示按 `a` 新增。(3) **item#3 週/月漲跌**：成分股表欄位由 Symbol/現價/收盤價/漲跌%/成交量/成交額 擴為 Symbol/現價/收盤價/**佔比/當日%/當週%/當月%**/成交量/成交額（week/month% 由既有 `fetch_sector_members_data` 真實回傳、`summarize_group` 已透傳，無需改抓取層）。(4) **item#4 Dashboard 卡片**：`DashboardScreen` `#strategy-panels` 新增第三張 `#sector-consensus-panel`，`_build_sector_consensus_panel()` 離線讀取 `sector_cache/history/*.jsonl` → `detect_broad_flow` → 與板塊頁共用同一份 `generate_sector_conclusions()` 輸出條列建議，資料不足時誠實顯示「N/M 板塊已就緒」收集進度，兩處文字保證一致。**已驗證**：`py_compile` 四檔通過；新增 21 項合成資料＋Textual `run_test()` 無頭測試全通過（佔比 75/25%/None 計算、week/month 透傳、種子後刪除預設不回填、自訂群組持久化、全刪後空權威狀態、廣度共識條列產生/資料不足不產生、成分股表三新欄存在並渲染、`a` 開啟新增 modal、新群組落盤、Dashboard 第三卡片掛載與建置）；既有 `scripts/verify_tui.py` 12/12 仍全通過（無回歸）。**已知取捨/限制**：群組改名後，舊名的 `sector_cache/history/{舊名}.jsonl` 廣度歷史不隨之改名（等同新名重新累積，孤兒檔 65 天後由既有 `prune_sector_history` 自然清除），刻意不搬移歷史以維持 surgical；item#2 的「依載入紀錄還原」即透過使用者設定檔權威化達成，未另做匯入/匯出。item#5(finguider 比對) 仍未納入本次範圍。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需在有網路環境按 `8` 進入「類股板塊分析」，確認右欄成分股顯示佔比與當日/當週/當月%、以 `a`/`e`/`d` 新增/編輯/刪除板塊與成分股且刪除的預設不再回來、Dashboard 首頁第三張「📊 類股共識」卡片於連續運行數日累積 ≥3 天快照後由「資料收集中」轉為顯示普遍漲/跌條列建議）

23. [open] [bug#00080] [function] **選擇權投資建議出現 -100% 假減倉訊號：週一抓到 OI 未結算的空快照被當成真實持倉歸零**
    * **問題描述**：7/13（週一）「選擇權投資建議」列出 NVDA 2026-08-21 到期多檔合約「未平倉量減少 100%」。合約到期日為 8/21（距當日約 39 天），並非撞到期日；為假訊號，不應出現在投資建議。
    * **root cause**：7/11、7/12 為週末，yfinance 回傳週五(7/10)收盤真實 OI（如 $220 call OI=57,049）；7/13 週一抓取時，該到期日整批合約 OI 尚未結算，yfinance 整列回傳 `openInterest=0、bid=0、ask=0`（`lastTradeDate` 仍停在 7/10）。`compute_options_flow()` 只用 contractSymbol 比對兩端且兩端都存在，於是 57,049→0 被算成 `oi_delta=-57,049 / oi_pct=-100%`，`abs(oi_delta)>=500` 觸發 `is_buildup`，四筆假減倉進入 events → 投資建議。（鏡像風險：隔日當這筆 0 成為視窗內最早快照、真實 OI 回來時，會變成 0→N 的假建倉。）
    * **solution**：`options_analysis.py` 新增 `_no_live_data(c)`——判定一筆合約列是否為「無有效市場資料」（`openInterest`、`bid`、`ask` 全為 0/None；舊格式無 bid/ask 時退化為「OI 為 0/None」）。`compute_options_flow()` 逐一比對前，若最早或最新任一端為 `_no_live_data` 即 `continue` 跳過該合約，不參與 buildup/swing 判定與 call/put skew 統計。此修正同時套用到「期權觀察清單」頁與 Dashboard 卡片（共用同一函式）。已驗證：以使用者本機真實 NVDA 快照（7/11–7/13）重跑，四筆 -100% 假訊號全數消失、NVDA events 歸零；合成資料確認「真實減倉 57,049→30,000（有雙邊報價）」與「真實建倉 1,000→5,000」仍正常觸發、未被誤殺；「0(空列)→57,049 鏡像假建倉」正確被跳過。`py_compile` 通過。**後續可選**：更根本的做法是在資料層（quotes/storage）偵測「整批 OI 未結算」的快照時延後/不寫入，避免污染歷史；此次採分析層守衛為最小外科修正，未動資料層。
    * **fixed by**：v0.0.4-dev（待使用者驗證：7/13 之後實際執行 TUI，確認「選擇權投資建議」不再出現 OI 未結算造成的 -100%/假建倉訊號，且真實的建倉/減倉訊號仍正常顯示）

23. [open] [bug#00080] [newfeature] **類股板塊分析：即時快取（免每次重載）+ 市場時段感知刷新（未開盤/已收盤/開盤中60s）+ 完整每日快照保留180日**
    * **問題描述**：使用者要求：(1) 載入 sector_analysis 應具快取機制，不要每次進入模塊都重新載入一遍；(2) 市場時段感知——當日尚未開盤抓昨日收盤最後資訊、已收盤依本日收盤最後資訊、開盤中走 60 秒一次更新追蹤最新價；(3) 載入最新價前皆保留上一筆最後資料（不清空畫面）；(4) 當下所有板塊與板塊個股資訊都需記錄在系統空間以便日後進一步分析，資訊保留 180 日。
    * **root cause**：bug#00078 骨架的 `SectorAnalysisScreen.run_background_fetch()` 每次進入畫面皆無條件呼叫 `_fetch_and_cache_sector_groups()` 重抓網路（僅用 `sector_group_fresh` 改標頭文字、不影響是否抓取），無即時快取、無 60 秒刷新、無市場時段判定；`append_sector_daily_snapshot()` 僅存精簡欄位（symbol/day_pct/marketcap）、first-write-wins（當日首筆即定、無法反映收盤最後值）、且 `prune_sector_history` 保留 65 日，不符「完整資訊、保留180日」。
    * **solution**：**設計決策（US 交易時段為基準）**：板塊概念股以美股為主（bug#00078 finguider 美股概念清單），故以美股常規時段（09:30–16:00 ET, Mon–Fri）作為刷新節奏基準。**實作**：(1) **即時快取**：`storage.py` 新增 `sector_cache/{user}_summaries.json` 快取——`load/save_sector_summaries_cache(user)`（存整份 `summarize_group` 結果＋`last_refreshed` Taiwan 戳記）；`_fetch_and_cache_sector_groups()` 抓取後寫入快取，且僅在「至少一檔成分股有真實 price」時才寫（避免失敗抓取鎖死空白，沿用 bug#00058 教訓）。(2) **市場時段刷新判定**：新增 `us_market_open_now()`、`last_us_close_dt()`（回傳最近一個已過的美股收盤 16:00 ET，轉為 Taiwan-naive 供與 `last_refreshed` 比較）、`sector_cache_needs_refresh(user)`——開盤中：快取 ≥60s 即需重抓（開盤中 60 秒更新）；收盤中：僅當快取早於「最近一次收盤」才重抓（未開盤沿用上一個收盤、已收盤抓本日收盤各一次，其餘時間全用快取）。(3) **畫面接線**：`SectorAnalysisScreen.on_mount` 先讀快取即時渲染（免空白重載）、`set_interval(60, run_background_fetch)`；`run_background_fetch` 改為 `@work(exclusive=True)` 且先問 `sector_cache_needs_refresh`——不需要時直接用快取（零網路），需要時才抓；`_on_fetch_complete` 在抓取結果為空（無群組/失敗）時「不清空 `self.summaries`」以保留上一筆最後資料。App 層 `_background_data_refresh`（每 30 分）將板塊 gate 由 `sector_group_fresh` 改為 `sector_cache_needs_refresh`，讓 Dashboard 類股共識卡片即使未進板塊頁也維持最新。(4) **完整快照＋180日**：`append_sector_daily_snapshot(group, summary)` 改存完整成分股欄位（symbol/price/prev_close/day/week/month%/volume/turnover/marketcap/weight）＋群組彙總（total_marketcap/capw_day/week/month/breadth/n_up/n_down/n_rated）＋`as_of` 戳記，並改為**同日 upsert 為最新**（開盤中每 60s 覆寫當日線、收盤後自然保有當日收盤最後值，符合「已收盤依本日收盤」）；`prune_sector_history` 預設由 65 改 **180** 日。breadth 引擎（讀 members 的 day_pct/marketcap）對精簡舊快照與完整新快照皆相容。**已驗證**：`py_compile` 通過；新增 22 項合成資料＋Textual `run_test()` 無頭測試全通過（完整快照欄位/群組彙總儲存、同日 upsert 保留最新、breadth 相容、>180日修剪、快取 round-trip 與戳記、開盤 <60s 不抓/≥60s 抓、收盤早於收盤點才抓/晚於則不抓、無快取需抓、畫面掛載即由快取渲染免抓、開盤+過期觸發抓取並落盤完整快照）；bug#00079 既有 21 項與 `scripts/verify_tui.py` 12/12 全數重跑通過（無回歸）。**已知取捨/限制**：(a) 以美股單一時段為刷新基準，群組內少數非美股成分（如 .KS/.DE）不另判其本地時段——與使用者「未開盤/已收盤/開盤中」單一市場心智模型一致；(b) 每日僅保留一筆（收盤最後值），非逐 60s tick 全量存檔（避免檔案爆量，且「日後分析」以日粒度為主）；(c) 群組改名仍沿用 bug#00079 之取捨（舊名歷史不搬移，180 日後自然修剪）。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需在有網路環境下實際執行 TUI——進入「類股板塊分析」應立即顯示上次快取數據而非空白重載；美股開盤中觀察是否約每 60 秒更新一次最新價、收盤後同一日內再次進入不重抓（顯示「已載入快取數據」）；連續運行後檢查 `data/sector_cache/history/*.jsonl` 是否逐日累積完整成分股欄位、且僅保留近 180 日）

24. [open] [bug#00081] [newfeature] **全畫面統一標註「更新時間」時間戳記（格式 yyyy-mm-dd hh:mm）**
    * **問題描述**：使用者要求 AssetTrack 內每一項功能（畫面）都需清楚標註資料更新時間，統一格式為 `yyyy-mm-dd hh:mm`。經與使用者確認範圍為全部畫面（Dashboard、近期重大事件、主動式ETF排行、進階分析、類股板塊分析、期權觀察清單、校準狀態），顯示位置統一為各畫面標題列/頁首。
    * **root cause**：各畫面雖多半已有 `last_refreshed`／`as_of_date`／`holdings_as_of_date` 等內部資料，但只有 `SectorAnalysisScreen` 在讀取快取時於暫時性狀態列文字中附帶顯示過（`tui.py:4407`，格式近似但非固定顯示），其餘畫面完全沒有固定呈現於標題列，且各處格式不統一（ISO 帶 `T`、或省略時分）。
    * **solution**：1. `shared.py` 新增 `format_updated_at(dt)`——全專案統一的「更新時間」格式化函式（`yyyy-mm-dd HH:MM`，`None` 時誠實顯示「—」，不臆測）。2. 七個畫面各自新增 `self._updated_at`（或現有 header 建構函式內就地計算）欄位，並在標題列 Panel 加入 `更新時間：{format_updated_at(...)}` 區塊：`DashboardScreen`（`_tick_header`，於 `_do_refresh_worker` 每次完成部位/報價刷新時以 `taiwan_now()` 標記）、`UpcomingEventsScreen`（`_update_header`，於 `_on_fetch_complete` 標記）、`ActiveETFsScreen`（`_set_header`，快取皆新鮮或即時抓取完成時皆標記）、`SectorAnalysisScreen`（`_set_header`；改用 `load_sector_summaries_cache` 實際 `last_refreshed` 時間，取代原本僅於「顯示快取數據」時才附帶顯示的舊寫法，`on_mount` 初次讀快取時也一併設定，不再與新的統一欄位重複顯示兩次時間）、`AdvancedAnalysisScreen`／`OptionsWatchlistScreen`（各自 `_run_analysis()` 每次呼叫時以 `taiwan_now()` 標記，因兩者皆為每次進入畫面即時重新計算，無額外快取層）、`CalibrationScreen`（`_show_report()` 內以 `taiwan_now()` 標記）。已知取捨：`ActiveETFsScreen`／`SectorAnalysisScreen` 的快取分支以「確認快取為今日最新」的當下時刻標記，而非 84 檔 ETF 各自實際的抓取時間（無單一彙總時間戳可用，避免為此另建機制、過度工程化）。已驗證：`py_compile` 全部檔案通過；`scripts/verify_tui.py` 12/12 通過（無回歸）；另以 Textual `run_test()` 無頭模式逐一掛載全部 7 個畫面，確認標題列/頁首皆正確渲染「更新時間：yyyy-mm-dd HH:MM」格式（`CalibrationScreen` 因背景回測執行緒與其他畫面併發搶佔導致單次批次測試逾時被誤判，改為單獨掛載測試後確認同樣正確顯示）。
    * **fixed by**：v0.0.4-dev（待使用者驗證：依序開啟全部 7 個畫面，確認標題列/頁首皆顯示「更新時間：yyyy-mm-dd hh:mm」且會隨資料重新整理而更新）

25. [open] [bug#00082] [newfeature] **類股板塊分析頁面新增「類股投資建議」條列區塊（畫面內直接呈現每日累計廣度共識）**
    * **問題描述**：使用者要求 sector_analysis 頁面內也要提供一個區塊，把類股板塊分析下的投資建議條列出來。先前 `generate_sector_conclusions()` 的條列建議只出現在 Dashboard 首頁「📊 類股共識」卡片（bug#00079 item#4），進入「類股板塊分析」頁面本身只有左板塊表／右成分股表＋左表的「共識」欄，沒有把整體投資建議完整條列在頁面內。
    * **root cause**：bug#00079 只把 `generate_sector_conclusions()` 接到 Dashboard 卡片，未在 `SectorAnalysisScreen` 內配置對應的結論呈現區塊。
    * **solution**：`tui.SectorAnalysisScreen` 於兩欄 body 下方、Footer 上方新增 `#sec-conclusions` 面板（沿用 ETF「進階分析」頁 `📝 結論` 區塊同一視覺樣式：magenta 邊框 Panel、`• ` 條列）。新增 `_render_conclusions()`：以畫面既有的 `self.flows`（各板塊 `detect_broad_flow` 結果）呼叫 `sector_analysis.generate_sector_conclusions()` 產生條列，與 Dashboard「類股共識」卡片共用同一份輸出、兩處文字保證一致；無足夠真實快照時誠實顯示「資料收集中：N/M 板塊已就緒」而非假結論。`_render_conclusions()` 於 `_render_groups()` 末尾統一呼叫，涵蓋 on_mount 初次渲染、`_on_fetch_complete` 抓取/快取回填、`_reload_after_edit` 群組增刪改後的所有更新路徑（皆先 `_recompute_flows()` 再 `_render_groups()`，故 flows 恆為最新）。CSS 給 `#sec-conclusions` `height:auto; max-height:12`，不擠壓上方兩欄表格（body 維持 `1fr`）。**已驗證**：`py_compile` 通過；新增 5 項 headless 驗證（面板存在、透過 spy 攔截 `Static.update` 確認實際推入的 Panel 內含普遍上漲板塊條列、含 `•` 項目符號、標題含「類股投資建議」、與離線 `generate_sector_conclusions` 輸出一致）全通過；bug#00079（21）、bug#00080（22）回歸與 `scripts/verify_tui.py` 12/12 全數重跑通過（無回歸）。**已知取捨**：頁面條列與 Dashboard 卡片同源，僅呈現位置不同；文字內容取決於真實累積之廣度快照（上線初期顯示收集進度屬預期）。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需在有網路環境進入「類股板塊分析」頁面，確認底部出現「📝 類股投資建議」區塊，且連續運行累積 ≥3 天板塊快照後，該區塊條列出各板塊「普遍上漲（共同買進）/普遍下跌（共同賣出）」的投資建議，內容與 Dashboard 首頁「📊 類股共識」卡片一致）

26. [open] [bug#00083] [bug] **類股板塊分析：畫面顯示「已載入快取數據」但左右表格多數欄位抓不到數值（節流的批次下載被快取鎖死）**
    * **問題描述**：使用者進入「類股板塊分析」，上方顯示「✅ 已載入快取數據」，但下方左欄（各板塊當日%/週%/月%/廣度）與右欄（成分股現價/收盤/當日%…）多數為「—」。實測畫面：光通訊僅 LITE 一檔有完整報價，COHR/POET/CIEN 僅有市值（佔比），AAOI/INFN 全無；其餘板塊（CPU/功率半導體/存儲/SaaS/七巨頭）當日%全為「—」。
    * **root cause**：兩個問題疊加。(1) **批次下載節流掉多數 ticker**：`quotes.fetch_sector_members_data` 的現價/報酬來自單一 `yf.download()` 批次；yfinance 在節流時會只回傳部分（此例整個 union 僅 LITE 有 close），其餘 ticker 不在回傳欄位中而被 `continue` 跳過→price/day/week/month 全 None。但同函式「逐檔 `fast_info`」抓市值卻對 COHR/POET/CIEN 成功（畫面有佔比），證明這些 ticker 有效、fast_info 端點可用，只是批次 download 掉了它們。由於 `capw_day` 需同時有市值與 day_pct，只有 LITE 兩者兼具，故只有光通訊有加權當日%，其餘板塊全「—」。(2) **不完整結果被快取鎖死**：09:25（台灣）美股休市，`sector_cache_needs_refresh` 因快取戳記晚於「上一次美股收盤」而回 False→畫面顯示快取、60s 定時器也 no-op，永不重抓；且 `_fetch_and_cache_sector_groups` 的 `got_real` 只要「任一檔有 price」（LITE）就存快取，於是這份幾乎全空的結果被當成有效快取，使用者在下一次收盤前無法自動更新、也無手動重抓入口。
    * **solution**：(A) **fast_info 價格回退**：`fetch_sector_members_data` 逐檔 `fast_info` 迴圈（本就為抓市值而執行）新增回退——當批次 download 對某 symbol 無 price 時，改以 fast_info 的真實 `last_price`/`previous_close` 補 `price`/`prev_close`/`day_pct`（週/月% 因 fast_info 無歷史仍為 None，不捏造）。fast_info 在批次 download 失效時通常仍可回應，故能把 COHR/POET/CIEN 等「有市值卻無現價」的檔補齊，連帶讓其所屬板塊的加權當日%可算出。(B) **手動重新整理入口**：`SectorAnalysisScreen` 新增 `r`「重新整理」鍵與 `action_refresh_now()`；`run_background_fetch(force=False)` 新增 `force` 參數，`force=True` 時略過 `sector_cache_needs_refresh` 直接重抓——作為「快取在節流時抓到不完整資料、又逢休市無法自動重抓」的逃生口。**已驗證**：`py_compile` 通過；新增 12 項測試（以假 yfinance 模擬「批次 download 僅回 LITE、fast_info 對 COHR/POET/CIEN 可用」）——確認 COHR/POET/CIEN 由 fast_info 回退補上 price 與 day_pct、AAOI/INFN 無資料仍為 None、市值仍正確、週/月%維持 None、群組 capw_day 由原本無法計算變為可計算、n_rated≥3；headless 驗證休市+新鮮快取時正常掛載不發網路請求（用快取），按下 `r`（force）則強制觸發真實抓取。bug#00079(21)/00080(22)/00082(5) 既有測試與 `scripts/verify_tui.py` 12/12 全數重跑通過（無回歸）。**已知取捨/限制**：fast_info 回退無歷史，故被回退的檔僅補當日欄位、週/月%仍為「—」（需批次 download 恢復才會有）；根因之 yfinance 批次 download 節流屬外部服務行為，本修正以「回退補值＋手動重抓」提升可用性與可控性，非消除節流本身。
    * **fixed by**：v0.0.4-dev（待使用者驗證：在有網路環境進入「類股板塊分析」，若左右表格仍多為「—」，按 `r` 手動重新整理應能補上多數成分股的現價/當日%與各板塊加權當日%；若批次下載持續被節流，被 fast_info 回退補值的檔會顯示現價/當日%但週/月%仍為「—」，屬預期）

27. [open] [bug#00084] [newfeature] **「近期重大事件」畫面新增已結束事件結論更新：CPI 月增/年增 + 下次FED會議機率、FED會議實際決議、財報營收/毛利/淨利 YoY**
    * **問題描述**：使用者要求 `UpcomingEventsScreen` 對「已經發生完畢的事件」給出更新結論：(1) 主要總經事件（例如 CPI 通膨公佈）更新月增減、年增減、FED 升降息機率；(2) 財報事件更新營收 YoY、毛利 YoY、淨利 YoY。經釐清後採納三項資料來源決策（使用者確認）：FED 升降息機率改用 Fed Funds 期貨價格推算（而非 CME FedWatch）；CPI 等總經實際值改用 FRED API（需使用者自行申請 FRED_API_KEY）；結論顯示位置為月曆事件清單內原地更新（不新增獨立區塊）。
    * **root cause**：功能缺失。`UpcomingEventsScreen.run_calendar_fetch()` 於 bug#00063 修正為 `start_date = today`，已過去的事件完全不納入清單（當時past事件無附加價值），系統原本也完全沒有任何總經實際值（FRED）或 Fed Funds 期貨資料的抓取管道；財報 YoY 數字雖 yfinance 有提供（`quarterly_income_stmt`），但先前從未被讀取使用。
    * **solution**：**(A) 資料層**（`quotes.py`）：新增 `fred_api_key()`／`fetch_fred_series()` 讀取 FRED 官方 API（`FRED_API_KEY` 環境變數，未設定或呼叫失敗一律回傳 `None`，不臆測）、`compute_cpi_conclusion()`（CPIAUCSL 季調後算月增、CPIAUCNS 未季調算年增，比照 BLS 官方新聞稿慣例）、`compute_fed_decision_conclusion(meeting_date)`（FRED `DFEDTARU`/`DFEDTARL` 官方目標利率區間，比對會議前後真實變動，得出實際升/降息 bp 數，非估算）、`_fedfunds_futures_symbol_candidates()`／`fetch_fedfunds_futures_price()`（CME 30-Day Fed Funds Futures `ZQ` 依會議月份組成 Yahoo 代碼，`yfinance` 抓收盤價）、`compute_next_fed_meeting_probability()`（期貨隱含利率 vs FRED `DFF` 真實有效利率之差距，以 25bp 為單位換算升/降息機率——簡化版方法論、非 CME FedWatch 逐日加權精確值，已於文字註明「僅供參考」）；新增 `fetch_earnings_actuals()`（yfinance `quarterly_income_stmt` 之 Total Revenue/Gross Profit/Net Income，與去年同季（4 欄前）比較算 YoY，任一數字缺失個別回 `None`）與併發版 `fetch_earnings_actuals_batch()`。**(B) 畫面層**（`tui.py`）：`run_calendar_fetch()` 窗口由「只顯示今天以後」改回「過去 30 天至未來 90 天」（`start_date = today - 30d`），對過去 30 天內已結束事件才觸發結論計算（`ev_date < today`）；CPI 過去事件呼叫 `compute_cpi_conclusion()` + 對「下一次即將到來的 FED 會議」呼叫 `compute_next_fed_meeting_probability()`；FED 過去事件呼叫 `compute_fed_decision_conclusion(ev_date)`；過去財報事件批次呼叫 `fetch_earnings_actuals_batch()`。新增三個純格式化函式 `_format_cpi_conclusion()`／`_format_fed_conclusion()`／`_format_earnings_conclusion()`，統一以「🏁 已公佈：...」字樣附加於原事件文字後方（換行＋縮排），缺資料時誠實顯示「暫不可用（需設定 FRED_API_KEY）」而非留白或猜測；`_get_event_type()`／`_render_monthly_calendar()` 沿用既有邏輯，多行 label 不影響其分類判斷。**(C)** `main()` 新增 `load_dotenv()`（`python-dotenv` 原已是相依套件但先前從未實際呼叫），讓使用者可於專案根目錄 `.env` 設定 `FRED_API_KEY`。**已驗證**：`py_compile` 三檔通過；單元測試以 mock 驗證 `fetch_earnings_actuals()` YoY 計算（合成季報：營收/毛利 +10%、淨利 +25%，結果精確相符）、`compute_fed_decision_conclusion()`（合成 4.25–4.50%→4.50–4.75% 正確判定 +25bp）、`compute_next_fed_meeting_probability()`（合成期貨隱含利率高於基準 5bp，正確算出 20% 升息機率）；無 `FRED_API_KEY` 時 `fetch_fred_series`/`compute_cpi_conclusion`/`compute_fed_decision_conclusion` 皆正確回傳 `None`（不假裝有資料）；Textual `run_test()` 無頭掛載 `UpcomingEventsScreen`，以合成的多行結論 label（CPI＋FED機率、FED決議、財報 YoY）呼叫 `_on_fetch_complete()` 與直接呼叫 `_render_monthly_calendar()`，確認畫面/月曆表格正確渲染多行文字不崩潰、Esc 正常返回 Dashboard；`scripts/verify_tui.py` 完整回歸 12/12 全數通過（無回歸）。**已知限制**：(1) Fed Funds 期貨的 Yahoo Finance 代碼慣例（`ZQ{月碼}{年}.CBT`／`=F`）因本次開發沙盒無法連線 finance.yahoo.com 驗證，僅能靜態推導兩種候選格式並於失敗時誠實回傳 `None`，需使用者於有網路環境實測確認代碼格式正確可抓到報價；(2) 機率換算為簡化版方法論（假設變動皆為 25bp 整數倍、未依會議在當月中的日期位置做逐日加權），非 CME FedWatch 官方精確值；(3) NFP（非農/失業率）事件本次未納入結論更新範圍（使用者僅明確要求 CPI 為主要事件範例），如需擴充另立需求。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需在有網路環境下，於專案根目錄 `.env` 設定真實 `FRED_API_KEY` 後執行 TUI，按 4 進入「近期重大事件」，確認過去 30 天內的 CPI/FED 會議/財報事件正確顯示已公佈結論；若 Fed Funds 期貨代碼格式需調整，請回報實際 yfinance 報價結果以便修正 `_fedfunds_futures_symbol_candidates()`）

28. [open] [bug#00085] [bug] **bug#00084 結論功能實際完全不會被觸發：畫面沿用 Dashboard 背景預先抓好的「僅未來事件、無結論」快取，從未執行新版 run_calendar_fetch()**
    * **問題描述**：使用者已設定真實 `FRED_API_KEY` 並於自己電腦上實測 bug#00084，按 4 進入「近期重大事件」後，過去已結束的 CPI/FED/財報事件仍完全沒有出現結論，判斷是「已發生事件被移除」。
    * **root cause**：稽核發現與「事件被移除」無關，而是**新邏輯根本沒被執行到**。`DashboardScreen` 一進場即背景呼叫 `_fetch_upcoming_events_worker()`，把結果存進 `self._upcoming_events`／`self._events_fetched`（此路徑刻意維持 bug#00063 的「只顯示今天以後」窄範圍，供首頁摘要卡片使用，本次未更動）。`action_upcoming_events()`（按鍵 `4`）先前把這份 Dashboard 自己的快取直接傳入 `UpcomingEventsScreen(..., self._upcoming_events, self._events_fetched)`；而 `UpcomingEventsScreen.on_mount()` 只要 `self.events_fetched` 為真，就直接呼叫 `_on_fetch_complete(self.cached_events, ...)` 顯示這份「僅未來、無結論」的舊資料，完全跳過本畫面自己在 bug#00084 新寫的 `run_calendar_fetch()`（過去 30 天窗口＋CPI/FED/財報結論皆在此函式內計算）。由於 Dashboard 幾乎必定比使用者按 `4` 更早完成背景抓取，`events_fetched` 在使用者操作當下幾乎恆為 `True`，等同新結論邏輯自 bug#00084 上線起就從未被實際執行過，這也解釋了為何使用者本機驗證時完全看不到任何結論（並非資料抓取失敗，而是新程式碼路徑根本沒被呼叫）。
    * **solution**：`UpcomingEventsScreen.__init__()` 移除 `cached_events`／`events_fetched` 兩個參數（連同對應的 `self.cached_events`／`self.events_fetched` 屬性）——這兩者代表的資料範圍（未來限定）與本畫面現在需要的範圍（過去 30 天含結論＋未來 90 天）已不相容，繼續共用快取只會製造這個錯誤；`on_mount()` 改為無條件呼叫 `self.run_calendar_fetch()`，確保每次開啟本畫面都執行自己完整的抓取＋結論邏輯，不再有機會被 Dashboard 的舊快取插隊短路。`DashboardScreen.action_upcoming_events()` 同步移除多傳的兩個參數，改為 `UpcomingEventsScreen(self._user, self._positions, self._rate)`。`DashboardScreen` 自身的 `_upcoming_events`／`_events_fetched`／`_fetch_upcoming_events_worker`／`_build_recent_events_panel`（首頁摘要卡片用）維持完全不變，兩份資料來源自此正式解耦，不再互相污染。已驗證：`py_compile` 通過；新增 headless 回歸測試——先手動模擬 Dashboard 已完成背景抓取（`_events_fetched=True`、`_upcoming_events` 為僅未來事件），再以 `patch.object` 攔截 `run_calendar_fetch` 按 `4` 開啟畫面，確認 `run_calendar_fetch` 恰好被呼叫一次（先前為 0 次，因走了舊的 cached 分支）、且畫面物件已無 `cached_events`／`events_fetched` 屬性殘留；`scripts/verify_tui.py` 12/12 全數重跑通過（無回歸）。
    * **fixed by**：v0.0.4-dev（待使用者驗證：於有網路、已設定 `FRED_API_KEY` 的環境重新執行 TUI，按 4 進入「近期重大事件」，確認過去 30 天內已結束的 CPI/FED/財報事件現在確實顯示「🏁 已公佈：...」結論文字，而非停留在原本僅未來事件的畫面）

29. [open] [bug#00086] [UI] **「近期重大事件」月曆下方，月曆圖與事件清單兩個 Panel 的下框線不對齊**
    * **問題描述**：使用者反映月曆版面右側「事件清單」內容區塊的框線沒有對齊。
    * **root cause**：`_render_monthly_calendar()` 把「月曆圖」（固定約 9 列）與「事件清單」（列數隨事件數量、且 bug#00084 起已結束事件會多出縮排結論行而變動）各自包成一個 `Panel`，放進同一個 `Table` 列。兩個 Panel 未指定 `height`，Rich 只會在較矮的儲存格下方補純空白墊高至與較高儲存格齊平，但不會補畫該 Panel 自己的框線——於是較矮的一側框線在畫面中段就先收尾，較高的一側框線落在更下面，兩者的下框線位置對不齊。
    * **solution**：計算兩側內容各自的列數，取較大值 + 2（上下框線）作為共同 `panel_height`，兩個 `Panel(...)` 都明確傳入 `height=panel_height`。Rich 在此模式下會在較矮一側的框線「內部」補空白列，讓兩個 Panel 的下框線永遠落在同一列。已驗證：以「事件清單」明顯較高（多筆已結束事件含結論）、較低（單一事件/無事件）兩種情境呼叫 `_render_monthly_calendar()`，兩側下框線在所有情境下皆對齊同一列；`py_compile` 通過；`scripts/verify_tui.py` 12/12 全數重跑通過（無回歸）。
    * **fixed by**：v0.0.4-dev（待使用者驗證：按 4 進入「近期重大事件」，確認各月份卡片左右兩個 Panel 的框線（尤其下框線）皆對齊同一列）

30. [open] [bug#00087] [newfeature] **「近期重大事件」畫面表頭新增五大總經指標「最新一期已公佈數值」（FRED）：核心CPI、核心PCE、NFP、失業率、聯邦資金利率**
    * **問題描述**：使用者要求整合 FRED API，於 `UpcomingEventsScreen` 追蹤五項重要總經指標的最新實際數值：(1) 核心 CPI、(2) 失業率、(3) NFP、(4) PCE、(5) 聯邦資金利率等。bug#00084 僅對「過去 30 天已結束事件」就地附加 CPI（總體）結論，且明確將 NFP／失業率排除於範圍外；PCE、核心 CPI、聯邦資金利率之最新讀數則從未呈現。
    * **root cause**：功能缺失。既有 FRED 管道（`fetch_fred_series`）僅被 `compute_cpi_conclusion`（總體 CPI，CPIAUCSL/CPIAUCNS）與 Fed Funds 期貨機率（DFF）使用；核心 CPI（CPILFESL）、失業率（UNRATE）、NFP（PAYEMS）、核心 PCE（PCEPILFE）、有效聯邦資金利率（FEDFUNDS）皆無對應 compute 函式，畫面也無任何常駐的「最新讀數」顯示區。
    * **solution**：**(A) 資料層**（`quotes.py`）：沿用既有 `fetch_fred_series` 與「缺 key／失敗一律回 None、不臆測」慣例，新增 `compute_core_cpi_conclusion()`（CPILFESL 季調算 MoM、CPILFENS 未季調算 YoY，比照 `compute_cpi_conclusion` 之 BLS 慣例）、`compute_pce_conclusion()`（核心 PCE PCEPILFE 季調指數算 MoM/YoY）、`compute_unemployment_conclusion()`（UNRATE 最新值與較上期百分點變動）、`compute_nfp_conclusion()`（PAYEMS 千人級距，最新減上期換算「較上月新增就業人數」與總就業水準）、`compute_fed_funds_rate_conclusion()`（FEDFUNDS 有效利率月均與較上期變動）；並新增彙整函式 `fetch_latest_macro_readings()` 回傳 `{core_cpi, core_pce, unemployment, nfp, fed_funds}`，各項缺資料時個別為 None。**(B) 顯示層**（`shared.py`）：新增純格式化 `format_macro_readings()`（＋輔助 `_fmt_signed_pct`），將讀數組成單行 Rich markup（通膨/利率上色：升紅降綠），任一項缺資料即跳過該項、全缺回 None（不以預設值填補）。**(C) 畫面層**（`tui.py` `UpcomingEventsScreen`）：`_update_header` 拆出 `_render_header()`（保存 `_header_status` 與 `_macro_readings_markup`，讀數存在時於標題下方多加一行「最新總經數據 (FRED): …」）；新增 `@work(thread=True) run_macro_readings_fetch()` 背景抓取＋格式化，完成後經 `call_from_thread` 呼叫 `_on_macro_readings()` 更新表頭；`on_mount()` 於狀態列設定後**無條件**呼叫 `run_macro_readings_fetch()`——刻意獨立於事件行事曆的快取路徑（`events_fetched`），故不受 bug#00085 所述「沿用 Dashboard 快取而短路」影響。**已驗證**：`py_compile` 三檔（quotes/shared/tui）通過；以 mock 讀數測 `format_macro_readings()`——五項齊全時輸出含核心CPI/核心PCE/NFP/失業率/聯邦資金利率之單行、全缺與 None 皆回 None（缺 API key 的無資料路徑一致）。**已知限制/取捨**：沙盒無 FRED 網路（api.stlouisfed.org 403）與無 yfinance，故 compute_* 之真實數值需使用者於本機（已設定 `FRED_API_KEY`、已裝相依）實測；核心 PCE 之 YoY 採季調指數計算（與 Fed 慣例一致，非未季調）；聯邦資金利率採 FEDFUNDS 月均有效利率（非目標區間）。
    * **fixed by**：v0.0.4-dev（待使用者驗證：於已設定 `FRED_API_KEY`、有網路之環境執行 TUI，按 4 進入「近期重大事件」，確認表頭下方出現「最新總經數據 (FRED)」單行，含核心CPI/核心PCE/NFP/失業率/聯邦資金利率之最新讀數且數值合理）

31. [open] [bug#00088] [newfeature] **重新設計部位新增/修改/刪除流程：批次新增、表格直接操作 (e/x/space)、表單自動推斷與進階欄位摺疊**
    * **問題描述**：使用者要求依應用目的重新設計部位增/改/刪方式，使填寫更直覺並支援一次新增多個部位，且須相容舊資料結構。原流程痛點：(1) 每按 `1` 只能經「部位調整選單 → 選擇部位清單」兩層跳轉才能到達目標；(2) 新增表單 17 欄全部平鋪，市場/幣別需手動填；(3) 一次只能新增一筆，多筆需重複整個流程；(4) 刪除一次僅能一筆。
    * **root cause**：`AdjustPositionsModal`／`ChoosePositionModal` 為早期選單式架構殘留，操作對象與 Holdings 表格游標脫節；`AddPositionModal` 回傳型別為單一 `Optional[Position]`，儲存管線（合併加碼/寫檔）以單筆為前提。
    * **solution**：（經使用者確認三項設計決策：表單累積批次、表格直接操作、自動推斷+進階欄位摺疊）(1) **批次新增**：`AddPositionModal` 回傳型別改為 `Optional[list[Position]]`，新增「儲存並繼續 ➕」按鈕——每筆驗證後加入待存清單（頂部顯示 `📋 待存清單 (N)`，保留券商/類型/市場等共通欄位、僅清空本筆專屬欄位），「完成儲存」一次回傳整批；取消/Esc 僅丟棄未確認的表單內容，已確認之待存清單仍回傳（每筆皆經明確確認）。(2) **表單直覺化**：Symbol 輸入即時自動推斷市場/幣別（`2330`/`.TW`/`.TWO` → TW/TWD，否則 US/USD；僅在欄位仍為上次推斷值時覆寫，不清掉手動輸入）；帳戶/交易所/幣別/備註/板塊五個選填欄位收進預設摺疊的「▸ 進階欄位」（鍵盤導航同步跳過隱藏欄位），常用情境僅需填 3~4 欄。(3) **表格直接操作**：Holdings 表格新增 `e`（編輯整筆，開啟預填表單）、`x`（刪除游標列；若有多選標記則批次刪除全部標記列）、`space`（切換多選標記，Symbol 前顯示綠色 ✔）；`DeleteConfirmModal` 擴充接受 `list[Position]`（列出前 6 筆+統計）；`1`／側欄改為直接開啟新增對話框，移除 `AdjustPositionsModal`、`ChoosePositionModal` 兩層選單與對應 handler（本次變更所產生之孤兒程式碼）。(4) **儲存管線**：新增共用 `_pos_key()`（券商+帳戶+代碼+類型，與既有比對邏輯一致）與 `_merge_position()`（原加碼合併邏輯原樣抽出，含 bug#00062 空單加權成本修正），`_handle_add_position_result` 逐筆合併後一次寫檔；onboarding 首筆新增路徑同步支援批次。(5) **資料結構相容**：`positions.json`（`ManualPositionsFile`/`Position` schema）完全未動，僅 UI 層與回傳型別改變；已驗證舊版最小欄位 `positions.example.json` 與 OCC 選擇權符號 round-trip 均正常載入。既有格內單欄編輯（Enter→`FieldEditModal`）與持倉操作選單（其餘格子）維持不變。**已驗證**：`py_compile` 通過；`scripts/verify_tui.py` 更新後 14/14 全數通過（含新增之 `add_position_modal` 批次流程、`symbol_auto_inference` 推斷、`table_direct_ops` e/x/space 三項無頭測試）；README 快速鍵表與功能說明已同步更新。
    * **fixed by**：v0.0.4-dev（待使用者驗證：實際執行 TUI 按 `1` 連續新增多筆確認待存清單與一次寫入；於表格按 `space` 標記多列後按 `x` 批次刪除；輸入台股代碼確認市場/幣別自動帶入）

32. [open] [bug#00089] [newfeature] **期權觀察清單新增「分析結論卡」：每檔標的綜合方向結論（看多/看空/觀望）＋ walk-forward 回測命中率就地顯示（1/5/10 天三組前瞻期）**
    * **問題描述**：使用者要求建立分析結論卡提供投資結論，並檢視既有判斷邏輯「是否能提前預測股價上漲下跌」與建立回測方式。經檢視：四組訊號中只有未平倉建倉 skew 有明確方向主張，結論區主打的異常震盪/背離（bug#00066）本身是異常偵測、非方向預測；bug#00070 的回測只校準 skew 單一訊號、單一前瞻期（5 天）；且結論 bullets 與回測完全脫鉤——畫面建議不顯示歷史命中率，也沒有「每檔標的目前綜合結論」的卡片粒度。結論：骨架有、不滿足需求。經使用者確認三項設計決策：(1) 方向判斷採「綜合評分」（skew＋殘差偏向合成，且回測對象即此綜合分數本身）；(2) 回測前瞻期 1/5/10 天三組並列；(3) 結論卡放觀察清單頁＋Dashboard 首頁卡片共用同一份輸出。
    * **root cause**：功能缺失。方向性訊號只有一條（`compute_options_flow` 的 underlying_skew，觸發頻率低）；`calibration.backtest_directional_signals` 僅回測該訊號且不支援多前瞻期；無任何函式把訊號合成「每檔標的的方向結論」，畫面也無結論卡區塊；回測結果只在按 k 的校準畫面出現，與結論文字無連結。
    * **solution**：**(A) 綜合方向結論**（`options_analysis.py` 新增）：`_residual_bias()` 對兩端快照以 contractSymbol 精確配對的全體合約，計算「排除股價變動後」的淨殘差偏向（殘差定義與 compute_iv_divergence 完全相同：ΔP − delta0×ΔS；買權殘差為正＝偏多力量、賣權殘差為正＝偏空力量，bias＝Σ買權殘差−Σ賣權殘差；沿用 bug#00080 的無效資料列排除）；`compute_directional_verdicts()` 把兩條方向性子訊號各記 +1/0/−1 後相加——skew（call_pct ≥70 → +1、≤30 → −1，沿用既有門檻）＋殘差偏向（|bias| ≥ $0.15 且參與合約 ≥2 張）——合計 >0 看多、<0 看空、=0 觀望（兩訊號矛盾時另標 conflict，誠實顯示觀望不硬給方向）；接受 as_of 參數，walk-forward 重算時無前視偏誤；並沿用 bug#00068 財報感知（區間含財報 → 降權註記）。`generate_verdict_cards()` 產生結論卡 bullets：方向、判斷依據（skew 占比／淨殘差金額）、該方向歷史命中率（取樣本最多的前瞻期，樣本 0 或無報告時誠實顯示「樣本累積中」、樣本過少時標「僅供參考」）、財報降權、與使用者部位方向一致/相反提示（共用 _stance_note）、IV 位階建議（共用 _iv_percentile_note）。**(B) 回測升級**（`calibration.py` 改寫）：`backtest_verdicts()` 對「與結論卡同一個 compute_directional_verdicts」做 walk-forward——對每個標的、每個歷史日 T 只用 ≤T 的快照重算綜合結論，再對 1/5/10 天三組前瞻期各自比對 T 之後 ≥h 天第一筆真實快照的 spot 變化是否同向，彙總各前瞻期看多/看空命中率、平均前瞻報酬與基準上漲比例（edge）；觀望日不計命中率、只計基準。因 Dashboard 首頁 60 秒重繪週期會重複呼叫，新增「資料簽章」快取（每檔標的的代碼/快照數/最末日，資料一天只多一筆，同一份資料只算一次）。原單一訊號版 `backtest_directional_signals` 由本函式取代後已無呼叫端，一併移除（本次變更產生的孤兒）。**(C) 畫面接線**（`tui.py`）：`OptionsWatchlistScreen` 頂部新增 `#ow-verdicts`「📋 分析結論卡」面板（include_neutral=True，就緒但無方向的標的也列出觀望卡與矛盾原因）；`DashboardScreen._build_options_flow_panel()` 置頂前 2 張有方向的結論卡（include_neutral=False），與觀察清單頁共用同一份 generate_verdict_cards 輸出（沿用 bug#00067 兩處對齊原則）；`CalibrationScreen` 改用 backtest_verdicts 並排三組前瞻期各自的基準/看多/看空命中率與樣本門檻警示，方法說明改為「校準對象＝分析結論卡的綜合方向結論」；說明頁（h）補結論卡邏輯說明。**已驗證**：合成資料 27 項檢查全數通過——看多（skew＋殘差同向）、看空、矛盾→觀望、單日快照 not ready→卡片為空、財報標記、walk-forward 回測（訊號後上漲情境：h1/h5 命中率 100%、h10 樣本遞減、無看空誤報、基準樣本≥訊號樣本、快取命中回傳同一物件、僅用 ≤T 資料無前視）、卡片文字（命中率引用、反向部位警示、IV 位階、樣本不足標示、無回測報告→樣本累積中、Dashboard 模式不含觀望卡）；`py_compile` 三檔通過；headless 掛載 DashboardScreen（新卡片邏輯）、OptionsWatchlistScreen（#ow-verdicts 存在）、CalibrationScreen（三 horizon 渲染）皆無崩潰；`scripts/verify_tui.py` 13/14 與修改前基準完全相同（唯一失敗項 empty_positions 為沙盒環境既有失敗，修改前後皆同，與本次變更無關）。**已知限制（畫面有誠實揭露）**：連續多日同標的訊號高度自相關、命中率略樂觀，需大樣本才穩健；剛上線時回測樣本必然為 0，結論卡會顯示「樣本累積中」而非捏造命中率——這是刻意的誠實狀態。
    * **版面調整與去重（同日）**：使用者要求 (1) 版面順序改為「各標的期權分析 → 分析結論卡 → 選擇權投資建議 → 左標的清單/右合約報價」；(2) 檢視分析結論卡與選擇權投資建議兩區目的是否重複，如重複則整合並刪除一項。檢視結果：投資建議區的 📈📉 每檔標的 skew 方向 bullets 與結論卡的方向依據**確為重複**（同一份 underlying_skew 資料、同一組 70/30 門檻、同一句 _stance_note 部位提示）；🌀 異常震盪 / ↔️ 背離 / 🎯 建倉與價格波動則為結論卡沒有的合約層級證據，不重複。處理：`generate_options_conclusions()` 移除 skew bullets 段（含不再使用的 positions 參數），只保留 🎯 合約事件；`generate_combined_options_advice()` 由整合版 `generate_analysis_card()` 取代（結論卡在上、「重點異常事件」在下，事件列不再重附 IV 位階避免同標的重複兩次；仍為觀察清單頁與 Dashboard 卡片共用的同一份輸出，沿用 bug#00067 對齊原則）；`OptionsWatchlistScreen` 移除 `#ow-conclusions` 面板與 CSS（「選擇權投資建議」區塊刪除、內容併入結論卡），compose 順序改為 header → `#ow-portfolio`（各標的期權分析）→ `#ow-verdicts`（整合後分析結論卡）→ `#ow-body`（左標的清單/右買賣權明細）；說明頁（h）第二/三區同步改寫。**已驗證**：合成資料 9 項整合檢查通過（結論卡在最前、事件分隔線、📈📉 已刪除、🎯/🌀/↔️ 保留、IV 位階同標的只出現一次、無資料回空 list）；原 27 項綜合結論/回測檢查重跑全過；headless 掛載確認 `#ow-conclusions` 已不存在且版面順序為 header→portfolio→verdicts→body；`py_compile` 通過；`scripts/verify_tui.py` 13/14 與基準相同（empty_positions 為沙盒既有失敗，非迴歸）。
    * **捲動修正（同日）**：使用者回報分析結論卡內容較長時，下方訊息（標的清單/合約明細）會被擠出畫面看不到，且無捲軸可操作。root cause：`#ow-portfolio` 與 `#ow-verdicts` 為固定排版的 `Static`（height:auto）直接堆疊在 `#ow-body`（height:1fr）之上，垂直 layout 下上方內容變長即無上限地佔用高度，把 body 擠出可視範圍；`Static` 本身不具捲動能力，故也不會出現捲軸。修正：compose 改以 `ScrollableContainer(id="ow-top")` 包住「各標的期權分析總表＋分析結論卡」兩個面板，CSS 設 `height:auto; max-height:65%`——內容短時只占實際高度（不浪費空間、無捲軸），過長時封頂 65% 並出現捲軸（滑鼠滾輪或聚焦後方向鍵皆可捲動），`#ow-body` 永遠保有其餘空間顯示左標的清單/右買賣權明細。**已驗證**（headless、44 列終端）：短內容時 top 無捲動範圍（max_scroll_y=0）；灌入 80 行長結論卡後 top 正確封頂 ≤65%、max_scroll_y>0、body 仍保有 ≥8 列高度、scroll_end() 實際捲動成功；`py_compile` 通過；版面順序測試（header→ow-top(portfolio,verdicts)→ow-body）與 `scripts/verify_tui.py` 13/14 基準維持不變。
    * **fixed by**：v0.0.4-dev（待使用者驗證：進入「期權觀察清單」確認頂部出現「📋 分析結論卡」——資料已累積 ≥2 天的標的應有 🟢/🔴/⚪ 結論、附回測命中率或「樣本累積中」；Dashboard 首頁「🎯 期權觀察結論」卡片頂部應出現同樣的方向結論；按 k 確認校準畫面改為 1/5/10 日三組並列。命中率需累積足夠快照日（同標的跨越前瞻期）後才會出現非零樣本，屬預期）

27. [open] [bug#00084] [bug] **類股板塊分析進入即抓取就崩潰：`_fetch_and_cache_sector_groups` 傳 list 給已改為收 summary dict 的 `append_sector_daily_snapshot`（tui.py 版本與 storage.py 不同步）**
    * **問題描述**：使用者回報 sector_analysis 運作起來有問題。實測：進入畫面觸發即時抓取時，`_fetch_and_cache_sector_groups()` 內 `append_sector_daily_snapshot(name, [ {...slim...} ])` 拋出 `AttributeError: 'list' object has no attribute 'get'`，導致背景抓取 worker 每次都失敗，左右表格無法更新（Textual 於 worker 執行緒吞例外，畫面不整個崩潰但資料永遠抓不進來）。
    * **root cause**：`assettrack/tui.py` 被回退到較舊版本（bug#00079 之後、bug#00080/00082/00083 之前的狀態），其 `_fetch_and_cache_sector_groups()` 仍以「精簡 list」`[{symbol,day_pct,marketcap}]` 呼叫快照函式；但 `assettrack/storage.py` 保留較新版本，`append_sector_daily_snapshot(group, summary, ...)` 已改為接收 `summarize_group()` 的完整 **summary dict** 並呼叫 `summary.get("members")`（bug#00080 完整快照/180日）。兩檔版本不同步→傳入 list 時 `list.get` 崩潰。（storage.py 與 quotes.py 保留新版、僅 tui.py 被回退，研判為 tui.py 遭較舊副本覆蓋而非刻意回退。）
    * **solution**：`_fetch_and_cache_sector_groups()` 改為直接傳完整 `summary`：`append_sector_daily_snapshot(name, summary)`；並將 `prune_sector_history` 由 65 對齊為 180 日（符合使用者保留180日需求與 storage 預設）。**已驗證**：`py_compile` 通過；以合成資料重現原崩潰後確認修復——headless 掛載 `SectorAnalysisScreen`（mock `fetch_sector_members_data`）成功抓取並渲染左右表格、逐日快照以完整成分股欄位（含 price）落盤、群組新增/編輯/刪除與方向鍵切換皆無崩潰；`verify_sector_ext`(21)、`scripts/verify_tui.py`(14/14) 全通過。**附註（版本不同步的殘留缺口，待使用者確認是否補回）**：此次 tui.py 回退連帶遺失了先前已交付並經驗證的三項功能——bug#00080（進入即顯示快取、依市場時段刷新、開盤中60s）、bug#00082（板塊頁「類股投資建議」條列區塊）、bug#00083（板塊頁手動 `r` 重新整理鍵；quotes 的 fast_info 價格回退未受影響仍在）。storage.py/quotes.py 對應支援仍在（部分成為未被 UI 使用的函式）。本項僅修復崩潰使功能可用；三項功能是否重新套回由使用者決定。
    * **後續（同次，經使用者確認補回三項功能）**：使用者確認要把回退遺失的三項功能補回，已重新套用至 `tui.py` 並與 storage/quotes 對齊：(1) bug#00080——`SectorAnalysisScreen.on_mount` 先讀 `load_sector_summaries_cache` 即時渲染（免空白重載）＋ `set_interval(60, run_background_fetch)`；`run_background_fetch(force=False)` 改為依 `sector_cache_needs_refresh` 決定重抓/用快取（開盤中60s、休市依上次收盤），`_on_fetch_complete` 抓取為空時保留上一筆、並標記 `_updated_at`（表頭顯示「更新時間」）；`_fetch_and_cache_sector_groups` 於抓到真實 price 時 `save_sector_summaries_cache`（got_real 守門避免鎖死節流失敗）；App `_background_data_refresh` 板塊 gate 由 `sector_group_fresh` 改回 `sector_cache_needs_refresh`。(2) bug#00082——板塊頁底部 `#sec-conclusions`「📝 類股投資建議」區塊，`_render_conclusions()` 於 `_render_groups()` 末尾呼叫，與 Dashboard 卡片共用 `generate_sector_conclusions()`。(3) bug#00083——板塊頁 `r`「重新整理」鍵 + `action_refresh_now()`，`run_background_fetch(force=True)` 略過快取新鮮度強制重抓。**已驗證**：`py_compile` 通過；`verify_sector_ext`(21)、`verify_sector_cache`(22)、`verify_sector_conclusions_panel`(5)、`verify_sector_fallback`(12) 與 `scripts/verify_tui.py`(14/14) 全數通過。
    * **fixed by**：v0.0.4-dev（待使用者驗證：進入「類股板塊分析」確認左右表格能正常抓值、不再靜默失敗；三項功能——進入即顯示快取／依市場時段刷新／60s、底部「類股投資建議」區塊、`r` 手動重新整理——皆已補回並運作）

33. [open] [bug#00090] [function] **本機分析資料暫存保留期統一改為 365 天（單一常數 `ANALYSIS_CACHE_RETENTION_DAYS`）**
    * **問題描述**：使用者要求「所有需要下載到本機端做分析的資料，暫存期限一律改為 365 天」。原本保留窗分散寫死：per-ETF 快取清除 14 天、ETF 歷史 65 天、期權歷史 65 天、類股歷史 180 天，且 tui.py 呼叫端另以顯式參數覆寫。
    * **root cause**：保留天數以字面值散落於 storage.py 四個函式預設值與 tui.py 五處呼叫端，無單一來源；保留窗過短亦限制 walk-forward 回測可用的真實累積樣本長度。
    * **solution**：storage.py 新增單一常數 `ANALYSIS_CACHE_RETENTION_DAYS = 365`，作為 `cleanup_old_etf_caches`／`prune_etf_history`／`prune_options_history`／`prune_sector_history` 四函式預設值；tui.py 五處呼叫端移除顯式覆寫（14/65/180/65/65）改沿用常數。僅調整「本機保留/修剪窗」，未動 in-memory 重抓新鮮度 TTL（beta/無風險利率/FRED 6h、Fed 期貨 1h、報價 60s，屬「多久重抓」非「保留多久」）。**已驗證**：`py_compile` 全模組通過；單元測試確認四函式 `max_age_days` 預設皆為 365；`scripts/verify_tui.py` 無頭 pilot 13/14（唯一 FAIL empty_positions 經與未修改 pristine 版本對照為既有沙盒環境問題，非本變更造成）。
    * **fixed by**：v0.0.4-dev（待使用者驗證）

34. [open] [bug#00091] [newfeature] **投資建議一律以美股為主：四大分析與回測移除台股（保留台股持倉追蹤）**
    * **問題描述**：使用者決策「以美股為主，台股的各種投資建議設定全部移除」。範圍限「投資建議」層（四大分析：主動式ETF／期權觀察清單／類股板塊／跨模型總結及其回測）；台股/TWD 的持倉追蹤、報價、基準幣別換算一律保留照常運作。
    * **root cause**：投資建議交叉比對（`shared.position_stance_by_symbol`，供 ETF 與期權結論引用）與期權淨 Greeks 分桶（`options_analysis.compute_portfolio_greeks`）先前以 `.replace(".TW"/".TWO")` 直接把台股部位併入判斷，未排除台股。
    * **solution**：shared.py 新增唯一判定來源 `is_taiwan_position(p)`（口徑與 `models.Position` 內建 is_tw 一致：幣別 TWD／代碼 .TW/.TWO 結尾／market==TW）；`position_stance_by_symbol` 與 `compute_portfolio_greeks` 分桶前先 `continue` 排除台股。持倉追蹤層（幣別、USDTWD 匯率、報價、基準幣別切換）完全未動。**已驗證**：`py_compile` 通過；單元測試確認 is_taiwan_position 對 TWD/.TW/.TWO/market==TW 皆判台股、對美股為 False；stance 僅留美股（AAPL 多、TSLA 空），台股（2330.TW/2317）不進入；`compute_portfolio_greeks(options_only=True)` 的 by_underlying 僅含美股、排除台股；`scripts/verify_tui.py` 13/14（同上）。**待續（後續 TUI 接線階段）**：期權觀察清單標的來源（`tui._watchlist_underlyings`）、ETF 頁面 TWD 表格移除、類股/跨模型輸入的台股過濾。
    * **接線完成（同項，後續）**：台股剩餘接線已全部完成——(1) 期權觀察清單標的來源 `_watchlist_underlyings` 以 `is_taiwan_position` 排除台股持倉、並過濾額外清單中 .TW/.TWO 代碼；(2) ETF 排行頁（ActiveETFsScreen）移除「🇹🇼 台股主動型」TabPane 與 `#etf-twd-table`，compose/on_mount/焦點導覽（右/左/上/下）/渲染/列選/清除/抓取宇集全部改為僅美股，`US_ACTIVE_TICKERS + TWD_ACTIVE_TICKERS` 三處（ActiveETFsScreen on_mount、run_background_fetch、App._background_data_refresh）改為僅 `US_ACTIVE_TICKERS`，`TWD_ACTIVE_TICKERS` 常數保留但已不用於任何路徑；(3) 類股 `summarize_group` 於進入廣度/市值加權/共識前排除 .TW/.TWO 成分股（新舊快照皆一致）；跨模型卡片因取用上述已過濾的 report/flows，自動為美股。**台股/TWD 的持倉追蹤、報價、USDTWD 匯率、基準幣別切換、代碼推斷（2330→TW/TWD）一律保留照常運作。** **已驗證**：單元測試——期權觀察清單排除 2330.TW 持倉與 2317.TW 清單代碼、保留 AAPL/NVDA/TSLA/AMD；summarize_group 丟棄 2330.TW 成分股、n_rated 由 3→2；Textual 無頭——ETF 排行頁僅單一美股 tab、上下左右導覽不崩潰、`#etf-twd-table` 確認不存在、Esc 返回 Dashboard；`scripts/verify_tui.py` 斷言 `#etf-twd-table` 已移除且 `active_etfs_screen` 通過，整體 13/14（唯一 FAIL empty_positions 同前，既有沙盒問題）；`py_compile` 全通過（含 device）。
    * **fixed by**：v0.0.4-dev（部分完成，待使用者驗證；TUI 接線待續）

35. [open] [bug#00092] [newfeature] **主動式ETF共識新增 walk-forward 回測驗證（命中率就地顯示於結論卡）+ 投資建議 ETF 宇集改美股**
    * **問題描述**：使用者要求「每一項功能的結論都要有回測方式驗證」。四項中僅期權觀察清單已具 walk-forward 回測（`calibration.backtest_verdicts`），主動式ETF只有結論、無回測。並依 bug#00091「投資建議以美股為主」，ETF 趨勢共識宇集需改為僅美股主動式 ETF。
    * **root cause**：ETF 的「多數性」共識（`compute_symbol_trends`）自上線以來只描述現象，從未有「此共識訊號預測後續股價的命中率」驗證層；且 Dashboard 卡片與進階分析頁宇集為 US+TWD。
    * **solution**：analysis.py 新增 `backtest_etf_consensus()`——完全比照 calibration（期權）紀律：100% 離線、零網路、不回填；逐一把每個歷史日 T「當作當下」，只用 ≤T 的真實 ETF 快照重算「多數性」共識（與畫面同一個 `compute_symbol_trends`，無兩套標準、無前視偏誤），再看該共識標的自身「跨ETF 真實持股價中位數」在 T 之後 1/5/10 天的前瞻報酬是否與共識方向一致（命中）；命中率與同宇集基準上漲率相比得出超額（edge）；樣本 < 門檻誠實標「資料累積中」（剛上線必為 0）。回傳結構與 `calibration.backtest_verdicts` 一致，故直接沿用 `calibration_status_label()`。新增 `etf_backtest_note()` 把命中率接成一句就地顯示於每則「多數性」結論（風格比照期權結論卡「▶ 回測…」）。`generate_etf_conclusions()` 新增 `backtest` 參數。tui.py 之 Dashboard「ETF趨勢結論」卡片與「進階分析」頁皆計算回測並帶入，進階分析頁另顯示「回測校準狀態」；兩處投資建議宇集由 `US_ACTIVE_TICKERS + TWD_ACTIVE_TICKERS` 改為僅 `US_ACTIVE_TICKERS`（ETF 排行的 US/TWD 顯示表格未動，屬後續 UI 步驟）。**已驗證**：`py_compile` 全通過；合成資料單元測試——真實加碼且股價續漲的標的其「up 共識」前瞻1日命中率 100%（n=12、超額 +15pp），足量樣本 ready=True、短資料 ready=False（誠實）、`calibration_status_label` 可直接套用；`generate_etf_conclusions` 帶回測時每則多數性結論尾端出現「▶ 回測…命中率」、不帶時顯示「樣本累積中」；空資料/單日資料不崩潰且回報未就緒；`scripts/verify_tui.py` 無頭 13/14（唯一 FAIL empty_positions 同前，既有沙盒環境問題）。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需持續使用讓 ETF 快照逐日累積，觀察 Dashboard 卡片與進階分析頁每則多數性結論尾端命中率、及進階分析頁「回測校準狀態」是否隨樣本增加由「資料累積中」轉為可參考）

36. [open] [bug#00093] [newfeature] **類股板塊共識新增 walk-forward 回測驗證（命中率就地顯示於類股共識結論）**
    * **問題描述**：延續 bug#00092，四大功能中「類股板塊分析」也只有結論（`detect_broad_flow` 的普遍上漲/下跌）而無回測。使用者要求每一項都要有回測方式驗證。
    * **root cause**：類股「廣度共識」自上線只描述現象，無「此共識方向預測該類股後續走勢的命中率」驗證層。
    * **solution**：sector_analysis.py 新增 `backtest_sector_flow()`——比照 calibration（期權）與 analysis（ETF）紀律：100% 離線、零網路、不回填；逐一把每個歷史日 T「當作當下」，只用 ≤T 的真實類股快照重算 `detect_broad_flow` 方向（與畫面同一函式、walk-forward 無前視偏誤），再以「該類股每日市值加權報酬 capw 複利成的類指數」量測 T 之後 1/5/10 天前瞻報酬是否與方向一致（命中）；命中率對比同宇集基準上漲率得出超額；樣本 < 門檻誠實標「資料累積中」。回傳結構與 `calibration.backtest_verdicts` 一致，沿用 `calibration_status_label()`。新增 `sector_backtest_note()` 就地顯示命中率（風格同期權/ETF）。`generate_sector_conclusions()` 新增 `backtest` 參數。tui.py：Dashboard「類股共識」卡片與「類股板塊分析」頁的 `_recompute_flows` 皆先一次讀入 `snapshots_by_group` 再同時算 flows 與回測（`self._sector_backtest`），`_render_conclusions` 帶入回測並顯示「回測校準狀態」。**已驗證**：`py_compile` 全通過；合成資料單元測試——廣義普遍上漲的類股其「up 共識」前瞻1日命中率 100%（n=12、超額 +25pp），足量 ready=True、短資料 ready=False、`calibration_status_label` 可套用；`generate_sector_conclusions` 帶回測時每則類股共識尾端出現命中率、不帶時顯示「樣本累積中」；空資料不崩潰；Textual 無頭實際掛載 `SectorAnalysisScreen`（stub 網路 worker）確認 `_recompute_flows` 有算出 `_sector_backtest`、`#sec-conclusions` 正常渲染、`Esc` 返回 Dashboard 無崩潰；`scripts/verify_tui.py` 13/14（唯一 FAIL empty_positions 同前）。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需持續使用讓類股快照逐日累積，觀察 Dashboard 類股卡片與類股頁每則共識尾端命中率、及類股頁「回測校準狀態」）

37. [open] [bug#00094] [newfeature] **回測「本身」的驗證層：Wilson 信賴區間 ＋ 對基準二項檢定（多重比較調整）＋ 前後子區間穩定性，三套回測共用**
    * **問題描述**：使用者提問「回測本身也有準確性，如何驗證回測模型是否精準？」。原本三套 walk-forward 回測（期權/ETF/類股）只顯示命中率與 n，無法分辨「真 edge」與「運氣」——命中率高但樣本少、或高得只是因為基準本來就高，都會被誤讀為可信。
    * **root cause**：回測只給點估計（命中率、edge），沒有：(1) 命中率的信賴區間；(2) 對基準的顯著性檢定；(3) 多前瞻期×多空多次檢定造成的資料窺探（挑最好看那組）之校正；(4) 樣本外/子區間穩定性（防單一盤勢過擬合）。
    * **solution**：新檔 `assettrack/backtest_stats.py`（純離線、零相依），對三套回測共用的同一種 `by_horizon` 聚合值計算：(1) `wilson_interval()` Wilson score 信賴區間（小樣本穩健）；(2) `binom_sf()` 精確二項上尾（log-space，n>2000 退常態近似）＋ `direction_significance()` 對基準（H0:命中率=基準上漲率）單尾檢定，並以 Bonferroni 對「前瞻期×多空」的檢定次數調整（`significant_adj`）；(3) `_stability()` 把逐訊號 records 依日期切前後兩半，檢查命中是否兩段都成立。`attach_significance()` 一次寫入 report（相容 ETF/類股 up_/down_ 與期權 bullish_/bearish_ 兩種命名）；`significance_phrase()` 供結論卡就地顯示；`validation_label()` 供校準狀態列。三套回測（calibration/analysis/sector_analysis）各自累積逐訊號 records 並於回傳前呼叫 `attach_significance`；三個結論卡的 note 函式與 `calibration_status_label` 一併升級。**已驗證**：`backtest_stats` 單元測試——`binom_sf` 對照手算精確值（10/10、5/10、8/10 皆吻合）、Wilson 8/10=0.49–0.94 且樣本越多區間越窄、`direction_significance` 對強 edge（p=0.001）判顯著、弱 edge（p=0.53）判不顯著；整合測試——ETF 前瞻1日命中率 100%但 n=12、基準85%→正確標「與基準無顯著差異(p=0.135)」；類股同 n=12→「優於基準(未過多重檢定，偏參考)(p=0.032)」；期權 n=120/72%/基準55%→「顯著優於基準(p=0.000)」且前後子區間 90%/90% 一致；`scripts/verify_tui.py` 13/14（唯一 FAIL empty_positions 同前，既有沙盒環境問題）。**設計說明**：對「連續日訊號自相關 → 有效樣本 < n、二項檢定略高估顯著性」的已知限制，以多重比較調整與子區間穩定性作部分防禦並於模組 docstring 誠實揭露，判定顯著一律偏保守。
    * **fixed by**：v0.0.4-dev（待使用者驗證：需累積足量真實快照後，觀察三處結論卡尾端的「95%CI／基準／顯著性(p)」與各頁「回測校準狀態」是否隨樣本增加由「與基準無顯著差異」轉為「顯著優於基準」、以及前後子區間是否一致）

38. [open] [bug#00095] [newfeature] **每雙週/每週自動重算校準引擎（提案顯著性把關、參數改動需使用者確認才套用）**
    * **問題描述**：使用者需求「具備每雙週/每週修正投資建議設定的能力」，決策「自動排程重算並顯示、參數改動需你確認」。需要一個排程機制:定期用最新累積快照重跑回測、據此提出投資建議「項目設定（門檻）」的調整,但一律等使用者確認才套用。
    * **root cause**：系統原本推薦門檻（ETF 多數性一致門檻/最少檔數、類股廣度門檻/持續天數等）皆為寫死預設,無任何「依回測結果定期檢討並建議調整」的機制,也無狀態保存。
    * **solution**：新檔 `assettrack/calibration_schedule.py`（純離線、零網路）。(1) **時間點判斷**:預設每雙週（`cadence_days=14`）,並可切每週（7）——理由:回測前瞻期最長 10 天,一週的最新訊號多半尚未結算,且有效樣本受自相關拖累,每週重調多屬雜訊;雙週已結算證據約翻倍,提案才有意義（「重算＋顯示」仍是每次刷新持續進行,只有「提出需確認的調整」設為雙週事件）。(2) **狀態檔** `data/{user}_calibration.json`:active_params / last_calibrated / cadence_days / pending / history。(3) **提案邏輯 `propose_adjustments()` 一律用 bug#00094 統計驗證把關**:樣本不足（n<20）不提案;樣本充足但「未顯著優於基準」→ 建議收緊主要門檻一步;「已顯著有效但前後子區間不一致」→ 保守收緊;「已顯著有效且門檻高於預設」→ 放寬一步回預設;皆以 min/max 夾限。(4) `run_recalibration()` 到期才算,把提案存入 pending 並蓋 last_calibrated,**絕不自行套用**;`apply_pending()`（使用者確認）才把 pending 寫入 active_params 並記錄 history;`dismiss_pending()` 略過並記錄。(5) 顯示輔助 `format_status()`/`format_proposal()`。**已驗證**（暫存目錄 + mock get_data_dir 單元測試）:預設初始化、未校準即到期;未顯著訊號→收緊(0.5→0.55)、健康且在預設→不動、樣本不足→不提案;run_recalibration 存 pending 且 14 天內不再到期、**確認前 active_params 維持 0.5 不自動套用**;apply_pending 後門檻改變且 history 記錄、持久化往返;健康且已收緊→放寬回預設;dismiss 不改參數只記錄;每週/雙週切換。`py_compile` 通過。
    * **接線完成（同項，後續）**：tui.py 已完成校準排程接線——(1) 新增 `_active_params(user)`，Dashboard 三張卡片（ETF/類股/跨模型）與「進階分析」頁、SectorAnalysisScreen 皆改讀使用者已確認的 `active_params`（consensus_threshold/min_etfs_evaluated、breadth_threshold/min_days），確認後的門檻即生效；AdvancedAnalysisScreen 補上 `user` 參數以讀取。(2) 新增模組函式 `_run_calibration_cycle(user, force)` 與 App `_maybe_recalibrate()`，於 `_background_data_refresh` 末尾呼叫（登入即抓取後、到期才真的產生提案），新提案以 `self.notify` 提醒。(3) 新增 `CalibrationModal`（按鍵 `k` 開啟）顯示校準狀態、待確認提案與目前生效門檻，按鈕【套用調整】(apply_pending)/【略過建議】(dismiss_pending)/【立即重算】(force)/【切換週期】(每週↔雙週)；無 pending 時 apply/dismiss 停用。側欄 footer 顯示「⚙️ N 項校準待確認 (k)」。**已驗證**：Textual 無頭——`_active_params` 讀回已套用門檻；側欄顯示待確認數；`k` 開啟 modal 顯示提案 0.5→0.55、apply 啟用；按【套用】後 active_params 變 0.55、pending 清空、history 記 applied、ETF 卡片以新門檻計算；【切換週期】14↔7；【略過】清 pending 且不改參數。修正一處命名衝突（modal 方法 `_render` 會覆蓋 Textual 內部 `Widget._render`，改名 `_refresh_body`）。`scripts/verify_tui.py` 13/14（唯一 FAIL empty_positions 同前）；`py_compile` 全通過（含 device）。
    * **fixed by**：v0.0.4-dev（引擎完成;**待接線**:tui.py 讀取 active_params 使確認後的調整生效、背景到期檢查 hook、狀態列顯示與「確認/略過」互動 UI——列為本項後續。待使用者驗證）

39. [open] [bug#00096] [newfeature] **登入後即開始抓取＋常駐狀態列，以及跨模型總結建議卡（主頁，用各訊號回測可信度加權）**
    * **問題描述**：使用者第二步需求——(A) 更新資料時間由「使用者成功登入後」開始抓取;(B) status bar 持續顯示目前系統正在抓什麼;(C) 跨模型分析建議:橫跨四大功能統整出一個最佳投資建議放在主頁,用各訊號回測命中率加權。
    * **root cause**：(A) 分析快照（ETF/期權/類股）原本只在進入各頁時、或 App 每 30 分鐘背景週期才抓;`_start_dashboard` 只註冊 30 分鐘計時器、不在登入當下立即抓,故登入後第一筆最久要等 30 分鐘。(B) 全系統無常駐狀態列,只有短暫的表頭訊息。(C) 系統完全沒有跨模型/總結卡片（前面稽核已確認 grep 不到任何 跨模型/總結）。
    * **solution**：**(A/B) tui.py**：`AssetTrackApp._start_dashboard()` 在登入後立即呼叫一次 `_background_data_refresh()`（不再只等 30 分鐘週期）。新增 `_fetch_activity` 字典與 `_set_fetch_active/_clear_fetch_active`;`_background_data_refresh` 於抓 ETF/期權/類股前後登記/清除項目（含 finally 保底清除）,DashboardScreen 的 `_do_refresh_worker`（報價匯率）與 `_fetch_upcoming_events_worker`（財報/總經）同樣登記。DashboardScreen compose 新增常駐 `#status-bar`（dock 於 Footer 上方）,`_tick_header`（每秒）從 `app._fetch_activity` 更新為「⏳ 正在抓取：X、Y…」或「✓ 資料已是最新（背景閒置）」。**(C) 新檔 `assettrack/cross_model.py`**：`synthesize_cross_model()` 把三項有回測背書的方向訊號（主動式ETF/期權/類股）各自的淨方向分數（多−空正規化到 −1…+1）以「該項回測可信度」加權合成整體傾向——可信度直接沿用 bug#00094 統計驗證:樣本不足→權重0（棄權）、未顯著→0.2、顯著→0.5、過多重檢定→1.0;所有可信權重為 0 時誠實回報「資料累積中」不硬湊方向。「近期重大事件」不投方向票（維持資訊性）,改作謹慎度修正——近 3 日內有 FED/CPI/NFP 提示降低把握。DashboardScreen 新增 `#cross-model-panel`（主頁 strategy 列上方全寬）與 `_build_cross_model_panel()`,與各分項卡片共用同一份 report/回測。**已驗證**：`cross_model` 單元測試——三項皆顯著看多→強烈偏多/把握度高、皆不顯著→把握度低、樣本不足→資料累積中（不給方向）、近3日事件→FED 謹慎提示、單一顯著看空（其餘棄權）→偏空;Textual 無頭掛載——`_build_cross_model_panel` 正確渲染誠實空狀態、狀態列在有/無抓取項目時分別顯示「正在抓取…」與「背景閒置」、登入確實觸發 `_background_data_refresh`;`scripts/verify_tui.py` 擴充斷言 `#cross-model-panel`/`#status-bar` 且 stub 登入抓取保持 hermetic,`dashboard_mounts` 通過、整體 13/14（唯一 FAIL empty_positions 同前,既有沙盒環境問題）;`py_compile` 全通過（含 device 端）。
    * **fixed by**：v0.0.4-dev（待使用者驗證：實機登入後確認狀態列即時顯示正在抓取的項目、主頁出現「🧭 跨模型總結建議」卡,並隨真實快照累積由「資料累積中」轉為帶把握度的加權方向）

40. [open] [bug#00097] [newfeature] **期權方向殘差改為 theta/gamma/DTE-aware 重定價分解；跨模型總結對齊 14 天預測區間**
    * **問題描述**：使用者於整體架構下提三點修改：(1) 期權方向分析結論須考慮 theta decay，排除單純由物理凸性(gamma)與時間流逝造成的偏差；(2) 期權樣本比對須考慮每日 DTE 不同，據此做期權價值分析；(3) 跨模型總結各模型原生預測區間不同(ETF 60天/期權 14天/類股 5天)，設定跨模型建議為 14 天，各模型應調整成同一時間維度。
    * **root cause**：(1)(2) 原殘差為一階近似 `殘差 = ΔP − delta0×ΔS`（`_residual_bias` 與 `compute_iv_divergence` 共用），只扣一階 delta，未扣 gamma 凸性與 theta 時間衰減，且以最早日 DTE 計算、未反映合約每日 DTE 縮短——導致買權因時間流逝天然減值被誤讀為偏空、大幅move 的凸性被誤讀為訊號。(3) `synthesize_cross_model` 以 `_best_direction_sig` 取「所有前瞻期中樣本最多」那組做可信度加權，未對齊統一時間維度，不同原生尺度的模型不可比。
    * **solution**：**(1)(2)** `options_analysis.py` 新增共用 `_repricing_decomp(c0,c1,date0,date1,spot0,spot1,r)`：以 t0 的 IV（缺則由 t0 權利金 `implied_vol` 反解）為基準，用 Black-Scholes 在「新現價 spot1、**縮短後的到期天數 dte1**、IV 不變」下重新定價 expected_p1，殘差 = 實際 p1 − expected_p1——delta、gamma(凸性)、theta(時間衰減)、每日不同的 DTE 全部被精確扣除（非泰勒近似），殘差僅剩 IV/需求重定價（vega×ΔIV）。`_residual_bias`（供 `compute_directional_verdicts` 方向結論 + 回測）與 `compute_iv_divergence`（頁面異常/背離事件）皆改用此分解；後者 expected_move 亦改為含 theta/DTE 的理論變動。**(3)** 三套回測（calibration/analysis/sector）前瞻期由 (1,5,10) 擴充為 **(1,5,10,14)**；`cross_model.py` 新增 `TARGET_HORIZON=14` 與 `_best_direction_sig_at(bt, horizon)`，`_reliability(bt, horizon=14)` 一律以 14 天前瞻期的顯著性評估各模型權重，讓 ETF/期權/類股在同一時間維度上比較（某模型在 14 天前瞻樣本不足即棄權，權重 0）；卡片標題標示「預測區間 14 天」。方向分數仍為當前立場（時間維度中性），僅可信度權重對齊 14 天。**已驗證**：合成資料單元測試——(1) 純時間流逝(spot/IV 不變)價格實跌 −0.548(theta) 但新殘差 = 0.0000；大幅move(+8) 舊 delta 殘差 +0.647(受 gamma+theta 汙染) 新殘差 = 0.0000；真實 IV +8pts 殘差 +0.984(方向訊號保留)；`_residual_bias` 買權需求升、賣權平→淨偏多且 theta-clean；`compute_directional_verdicts` 對「買權 IV 升+時間流逝+DTE 縮短」正確給「多」(bias 1.51)。(3) 某模型 10 天顯著但 14 天樣本不足→於 14 天目標正確棄權(權重 0)、14 天顯著→full 權重；端到端 options(僅 10 天顯著)棄權、ETF(14 天顯著)驅動偏多、卡片標示「預測區間 14 天」；三套回測 by_horizon 確含 14。`scripts/verify_tui.py` 13/14（唯一 FAIL empty_positions 同前）；`py_compile` 全通過（含 device）。**取捨**：`_repricing_decomp` 每合約做 BS 定價/反解，較舊一階近似略貴，但回測結果有資料簽章快取、合約數受 28–60 DTE×±15% 履約價限縮，實務可接受；14 天前瞻樣本累積最慢，跨模型總結需較久才由「資料累積中」轉為帶把握度方向（誠實反映 14 天預測本就需要 14 天後果驗證）。
    * **fixed by**：v0.0.4-dev（待使用者驗證：實機累積足量真實期權快照後，觀察方向結論不再因時間流逝/大幅move 系統性偏空、以及主頁跨模型卡標示「預測區間 14 天」且各模型權重依 14 天命中率浮動）

41. [open] [bug#00098] [function] **Summary 主看板畫面精簡：移除與 1–8 快捷鍵重複的左側功能選單，及 Positions/Brokers 計數框與損益排行框**
    * **問題描述**：Summary 主看板左側功能選單（新增部位／立即重整／近期重大事件等）與 Footer 的 1–8 快捷鍵完全重複；metrics 列的 Positions、Brokers 計數框與 Holdings 表格、券商資產分布面板資訊重疊；損益排行框亦屬冗餘。使用者要求精簡畫面並重新排列。
    * **root cause**：早期版面同時提供側邊 OptionList 選單與鍵盤快捷鍵兩套入口，且 metrics 列塞入 5 格（含與其他面板重複的 Positions／Brokers 計數）。
    * **solution**：`tui.py` `DashboardScreen` 移除整個 `#sidebar`（`sidebar-logo`／`sidebar-nav` OptionList／`sidebar-footer`）及其 `_render_sidebar()`、`on_option_list_option_selected()`、`on_key` 左右鍵切換側欄邏輯；`on_mount` 焦點改設 `#holdings-table`。`_build_metrics_panel` 由 5 格縮為 3 格（Total Value／Unrealized P&L／Portfolio Beta），移除 Positions、Brokers 兩格與其 `broker_set` 計算，欄寬比改 (3,3,2)。移除 `#pnl-leaderboard`（損益排行）面板與 `_build_pnl_panel()`，`#side-panels` 改為 broker-dist 與 recent-events 兩格等寬 (1fr/1fr)。CSS 同步移除 `#sidebar`／`#sidebar-nav`／`#pnl-leaderboard` 規則。所有動作入口統一由 Footer 的 1–8 快捷鍵提供。已驗證：`ast.parse`／`py_compile` 通過；grep 確認無殘留 `sidebar`／`pnl-leaderboard`／`_build_pnl_panel`／`_render_sidebar`／`broker_set` 參照；`OptionList` 仍供其他 Modal 使用。**已知取捨**：原 `sidebar-footer` 的「N 個持倉」統計與「校準待確認 (k)」提示一併移除（功能仍可按 `k` 進入）；如需保留該提示可另行 relocate 至狀態列。 **版面重排（同批，依使用者要求）**：(1) 券商資產分布 `#broker-dist` 移至頂部新 `#top-row`，置於 Total Portfolio metrics 左側（固定寬 48）；(2) 近期重大事件 `#recent-events-panel` 移至 Holdings 右側，與 `#holdings-scroll` 併入新 `#holdings-row`（事件欄固定寬 36、滿高）；(3) 跨模型總結建議 `#cross-model-panel` 獨立整行並上下 `margin: 1 0` 各留一格空白；(4) 三張結論卡改為各自獨立整行由上而下堆疊（順序：類股共識→期權觀察結論→ETF趨勢結論），移除 `#side-panels`／`#strategy-panels` 併排容器。已驗證 `ast.parse`／`py_compile` 通過、無殘留 side-panels／strategy-panels 參照、十個面板 id 於 compose 與 CSS 一一對應。 **寬度調校（同批，消除不必要跳行）**：依各框實際內容量測（East-Asian 寬度）設定寬度、高度一律 auto——`#broker-dist` 62（券商列最長約 58 格，含 9 位數金額不跳行）、`#recent-events-panel` 50（事件列最長約 44 格，含 5 字母代碼＋盤後時間不跳行）、`#metrics-row` 1fr、`#holdings-scroll` 1fr（唯一保留彈性高度的可捲動資料表區）、跨模型卡與三張結論卡皆滿寬 1fr；所有資訊框 `height: auto`。已量測確認最長行均 ≤ 內容可用寬度。
    * **fixed by**：v0.0.4-dev（待使用者驗證：實機執行 TUI 確認左側功能選單、Positions／Brokers 計數框、損益排行框皆已移除，畫面焦點落在 Holdings，1–8 快捷鍵運作正常）

42. [open] [bug#00099] [function] **期權「分析結論卡／期權觀察結論」改為逐標的分組＋縮排，且每檔標的用自己的獨立回測給不同結論**
    * **問題描述**：使用者要求 `_build_options_flow_panel`（Dashboard 卡片）針對每一項標的都有獨立的回測模型、給出不同的回測結論；並希望投資建議依標的分類、善用縮排，不要所有資訊混在下面一串。
    * **root cause**：原 `generate_analysis_card` 把所有標的的方向結論攤成一串 bullet，且每則引用的回測命中率來自 `calibration.backtest_verdicts(snapshots_by_underlying)`——那是把**所有標的的訊號彙總成同一份 by_horizon 統計**，因此同一方向的每檔標的顯示的都是同一個命中率，不是各自獨立的回測；合約層級事件（🌀/↔️/🎯）也全部平鋪在方向結論下方，未依標的歸類。
    * **solution**：`options_analysis.py` 新增 `generate_grouped_analysis_card()`：以標的為單位輸出「已完成排版」的多行區塊——表頭為方向結論（🟢看多/🔴看空/⚪觀望＋期間），其下以 `　　· ` 縮排列出「依據（skew／殘差）、**該標的自己的** walk-forward 回測命中率、財報降權、部位方向、IV 位階」，再以更深縮排列出**只屬於該標的**的合約層級重點事件。關鍵：回測改為對每檔各呼叫一次 `backtest_verdicts({u: snaps_u})`（只餵該標的快照），命中率因此逐標的獨立、彼此不汙染（`backtest_verdicts` 有資料簽章快取，逐檔呼叫仍廉價）。新增 `_clean_note()` 去除 helper 既有 `　▶ ` 前綴改用縮排項目符號。`tui.py` 兩處（`_build_options_flow_panel` Dashboard 卡片＝只列有方向者 `include_neutral=False`；`OptionsWatchlistScreen` 分析結論卡＝完整檢視 `include_neutral=True` 含觀望）改用同一函式並以 `"\n".join` 直接輸出（不再逐行加 `• ` 前綴，以免破壞縮排）；移除 tui.py 對 `generate_analysis_card` 的呼叫與已無用的 import（該函式本體保留於 options_analysis.py，供既有測試/文件參照，未刪）。已驗證：`py_compile` 兩檔通過；合成資料測試——結構（表頭 emoji、`· ` 縮排子項、事件更深縮排、組間空行、`include_neutral` 開關正確含/不含觀望）、**獨立性**（同一檔 BBB 的回測結論在「單獨」與「與其他標的同組」時逐字相同，證明未被彙總汙染）、**不同結論**（BBB 前瞻1日命中率 0% vs CCC 100%，來自各自快照）皆通過。**已知取捨**：`generate_analysis_card` 及 tui 的 `generate_verdict_cards` import 於本次後成為未使用（前者由本次改動孤立、後者為既有未使用 import），基於 surgical 原則保留未刪、於此註記。
    * **fixed by**：v0.0.4-dev（待使用者驗證：實機執行 TUI，確認 Dashboard「期權觀察結論」卡片與「期權觀察清單」分析結論卡皆改為逐標的分組＋縮排、每檔顯示自己的回測命中率、事件歸於各標的之下；資料累積足夠天數後不同標的命中率確實各異）

43. [open] [bug#00100] [UI] **Summary 首頁「期權觀察結論」卡片只顯示每檔總結、明細移回期權觀察清單頁（避免卡片被完整明細佔滿）**
    * **問題描述**：bug#00099 上線後，Dashboard 首頁「期權觀察結論」卡片顯示每檔標的完整明細（依據、回測、財報、部位、IV、合約事件、縮排多行），標的一多整張首頁被佔滿。使用者要求首頁只顯示總結結論，細節進「7 期權觀察清單」頁再看。
    * **root cause**：`_build_options_flow_panel`（Dashboard 卡片）與 `OptionsWatchlistScreen`（頁面）共用同一份 `generate_grouped_analysis_card()`，且卡片直接取用完整明細版面——首頁與明細頁定位不同（首頁應為總覽、頁面才是細節），共用完整版面導致首頁資訊過載。
    * **solution**：`options_analysis.py` `generate_grouped_analysis_card()` 新增 `summary_only` 參數與 `_verdict_backtest_short()` 極簡回測提示：`summary_only=True` 時每檔只輸出**一行**——方向結論＋該檔獨立回測命中率（`· 回測命中率 X%（前瞻N日 n=…）`，樣本不足顯示「回測樣本累積中」），不列依據/財報/部位/IV/合約事件、無縮排、無組間空行。`tui.py` `_build_options_flow_panel` 改傳 `summary_only=True`（仍 `include_neutral=False` 只列有方向者）；`OptionsWatchlistScreen` 頁面維持 `summary_only=False` 完整明細不變。逐標的獨立回測（bug#00099）在總結模式下保留——每檔命中率仍由各自 `backtest_verdicts({u:snaps})` 而來。已驗證：`py_compile` 兩檔通過；合成資料測試——總結模式只有 2 行方向總結、無任何縮排行/組間空行、且各帶該檔自己的回測命中率（BBB 0% vs CCC 100%）；完整模式（頁面）縮排子項、合約事件、觀望標的一切照舊。
    * **fixed by**：v0.0.4-dev（待使用者驗證：實機執行 TUI，確認首頁「期權觀察結論」卡片每檔只有一行總結、不再被明細佔滿，按 `7` 進入期權觀察清單頁仍看到逐標的分組＋縮排的完整明細）

44. [open] [bug#00101] [newfeature] **主動式 ETF 每日交易紀錄自動抓取與解析管道 (Automated ETF Trade History Pipeline)**
    * **問題描述**：使用者要求補足主動式 ETF 歷史交易紀錄的功能缺口。由於 yfinance 不提供歷史交易明細，系統需透過自動化流程從公開資料源（如 ARK Invest 每日交易檔、發行商持股 CSV/快照前後日比對）解析買賣明細並存入本機 `etf_cache/{symbol}.json` 的 `history` 欄位中。
    * **root cause**：yfinance `FundsData` 僅提供當前持股與資產配置，不提供任何基金交易歷史明細；且系統先前未將每日持股快照的變化自動比對轉換為交易動作。
    * **solution**：1. 新檔 `assettrack/etf_trades.py`：實作 `derive_trade_history_from_snapshots()` 讀取本機每日持股快照，自動比對前後日持股（股數與權重變動），精確推算 `BUY`/`SELL` 動作、標的、股數、價格與權重增減；實作 `fetch_ark_daily_trades()` 解析 ARK 官方每日交易檔。2. 實作 `update_etf_trade_history()` 統合兩來源、去重並寫入 `cached["history"]`。3. `tui.py` 在 `_fetch_and_cache_etf_symbols()` 與 `ActiveETFsScreen.on_mount()` 中掛載該流程，讓全系統 84 檔 ETF 自動產出 3,191 筆真實歷史交易明細並呈現於 TUI 界面。已通過語法編譯與 84 檔 ETF 交易導出測試。
    * **fixed by**：v0.0.4-dev（待使用者驗證：開啟 TUI「6 主動式ETF排行」，選取任一 ETF 查看右下角「歷史買賣紀錄」是否已呈現正確的交易動作與明細）

45. [open] [bug#00102] [newfeature] **重點經濟指標期對期變動比較與經濟意涵動態解析 (Period-over-Period Economic Indicators Comparison & Dynamic Macro Analysis)**
    * **問題描述**：使用者要求在「重大事件與經濟日曆」中，針對重點經濟指標（Core CPI, Core PCE, NFP, 失業率, 聯邦資金利率）進行期對期（本期 vs 上期）變動比較與經濟意涵解析（如：Core CPI 月增率較上期下降 0.2%，代表通膨壓力減緩及對 Fed 貨幣政策之影響）。
    * **root cause**：原 `fetch_latest_macro_readings()` 僅抓取單期數據，未保留前期歷史以進行跨期變動比較，且畫面缺乏針對指標變動量的經濟影響解讀。
    * **solution**：1. `quotes.py` 擴充五大指標計算函式（`compute_core_cpi_conclusion` 等），取前 3~14 期 FRED 數據算出上期數值、本期較上期變動量 (pp/MoM/YoY)，並生成動態經濟意涵解析 (`interpretation`)。2. `shared.py` 新增 `format_macro_analysis_lines()` 將各指標期對期變動與意涵格式化為 Rich 多行標註。3. `tui.py` `UpcomingEventsScreen` 表頭與日曆面板新增「📊 重點經濟指標動態解析」展示區塊。4. 更新 `INVESTMENT_LOGIC.md` 文件。已通過語法編譯與單元測試。
    * **fixed by**：v0.0.4-dev

46. [open] [bug#00103] [newfeature] **主動式 ETF 跨基金大類資產 (股票/債券/現金/黃金) 輪動與個股雙向共識分析 (Cross-ETF Asset-Class & Symbol Trend Consensus)**
    * **問題描述**：使用者要求主動式 ETF 分析除了既有的個股共識外，須能分析跨基金是否在特定期間同時買入/賣出特定標的，並擴充「大類資產輪動共識」——分析多數主動式 ETF 在特定區間下是否普遍增持或減持股票、債券、現金、黃金/大宗商品等大類資產，提供資產配置層級的投資建議。
    * **root cause**：原 `storage.append_etf_daily_snapshot()` 僅儲存個股持股與 AUM，未保存 `asset_classes` 歷史；且 `analysis.py` 僅進行單個股票代碼的共識計算，缺少跨 ETF 大類資產 (Stock/Bond/Cash/Other) 的趨勢共識統計。
    * **solution**：1. `storage.py` 的 `append_etf_daily_snapshot()` 擴充保存 `asset_classes` 欄位歷史。2. `analysis.py` 新增 `compute_asset_class_trends()` 計算 60 天內各大類資產在各大基金間的增減比率與共識（股票/債券/現金/黃金與其他）；更新 `generate_etf_conclusions()` 分成「🌐 【大類資產輪動】」、「📊 【同步買入標的】」、「📉 【同步賣出標的】」三大層級輸出。3. `tui.py` 與 `INVESTMENT_LOGIC.md` 同步更新展示與技術文件。已通過語法編譯與單元測試。
    * **fixed by**：v0.0.4-dev

47. [open] [bug#00104] [function] **主動式 ETF 分析觀察視窗改為 14 天（更緊湊的時間尺度）**
    * **問題描述**：使用者要求主動式 ETF 分析觀察視窗由原本 60 天縮短改為 14 天（與期權觀察 14 天及跨模型對齊預測區間 14 天一致），以在更緊湊的時間尺度下精確捕捉機構短期同向建倉/減倉與大類資產輪動操作。
    * **root cause**：原 `tui.py` 預設 `ADVANCED_ANALYSIS_WINDOW_DAYS = 60` 且 `analysis.py` 預設 `window_days = 60` 視窗過長，無法反應機構 14 天內的緊湊調倉動作。
    * **solution**：1. `tui.py` 將 `ADVANCED_ANALYSIS_WINDOW_DAYS` 設為 14。2. `analysis.py` 將 `compute_symbol_trends`、`compute_asset_class_trends` 與 `backtest_etf_consensus` 預設 `window_days` 設為 14。3. 更新 `INVESTMENT_LOGIC.md` 與相關註解。已通過語法編譯與單元測試。
    * **fixed by**：v0.0.4-dev

48. [open] [bug#00105] [optimization] **期權觀察 OI Skew 改採 Delta 權重名義金額曝光 (Dollar Delta OI Exposure Skew)**
    * **問題描述**：使用者指出舊有 OI Skew 純以「合約張數」計算，極易被幾百張幾美分的遠價/末日 Call（如 $0.05 遠價彩票合約）大量張數扭曲，誤導產生偏多共識。
    * **root cause**：原 `options_analysis.py` 的 `compute_options_flow` 單純累加 `oi_delta` 合約口數，未納入現價 Spot 與 Black-Scholes Delta 進行曝光金額加權。
    * **solution**：1. `options_analysis.py` 中的 `compute_options_flow` 改用 Black-Scholes 就地計算 Delta 權重曝光金額 ($\text{Dollar Delta OI} = \Delta\text{OI} \times \text{Spot} \times |\text{Delta}| \times 100$)。2. `underlying_skew` 之 `call_pct` 改由兩端 Dollar Delta OI 比例決定。3. 更新 `generate_verdict_cards` 結論卡標示與 `INVESTMENT_LOGIC.md` 文件。已通過語法編譯與單元測試。
    * **fixed by**：v0.0.4-dev

49. [open] [bug#00106] [optimization] **回測模型擴充 30/60 天長線前瞻期與有效獨立樣本數 (ESS) 二項檢定顯著性優化**
    * **問題描述**：使用者要求：1. 在回測中加入 30 天與 60 天長線前瞻期 (Horizons)，以符合機構調倉週期的真實發酵時間；2. 在計算二項檢定顯著性 $p$ 值時，引入有效獨立樣本數 ($\text{ESS} = \lfloor N / \text{Horizon} \rfloor$)，解決每日連續重複訊號在長前瞻期下重疊報酬自相關導致 $p$ 值虛假過高（顯著性高估）的問題。
    * **root cause**：原 `calibration.py` / `analysis.py` / `sector_analysis.py` 預設 `horizons` 僅至 14 天；`backtest_stats.py` 在算 `binom_sf` 時直接套用原始全量樣本 $N$，未除以前瞻天數估算有效獨立樣本數。
    * **solution**：1. 全全系統回測模組擴充預設前瞻期 `horizons = (1, 5, 10, 14, 30, 60)`。2. `backtest_stats.py` 之 `direction_significance()` 引入 `horizon` 參數，計算 $\text{ESS} = \max(1, \lfloor N / \text{Horizon} \rfloor)$ 與 $\text{hits}_{\text{ess}} = \text{round}(\text{hit\_rate} \times \text{ESS})$ 進行二項檢定與 Wilson CI，防止重疊視窗自相關誤導。3. 更新 `INVESTMENT_LOGIC.md`。已通過語法編譯與單元測試。
    * **fixed by**：v0.0.4-dev

50. [open] [bug#00107] [optimization] **ETF 多數性共識「平手偏多」——2 上 2 下的平手被判為 up，注入系統性多頭偏誤**
    * **問題描述**：使用者審查四大功能建議邏輯（第 1 點）。`analysis.compute_symbol_trends` 與 `compute_asset_class_trends` 的共識判定為 `pct_up >= consensus_threshold and pct_up >= pct_down`，當某標的「2 檔 ETF 增碼、2 檔減碼」時 `pct_up == pct_down == 0.5` 落入第一分支被判為「同時買入(up)」；空方則需嚴格過半，形成多頭偏誤，並經 `cross_model` ETF 淨方向分數放大。
    * **root cause**：`pct_up >= pct_down` 讓平手一律歸多；down 分支缺 `pct_down > pct_up` 對稱條件。
    * **solution**：兩函式共識判定改為需嚴格多於反向（up 需 `pct_up > pct_down`、down 需 `pct_down > pct_up`），平手（相等）一律歸 `mixed`，多空對稱。已以合成資料驗證：4 檔中 2 上 2 下 → mixed（舊版為 up）；3 上 1 下仍正確為 up。回測 `backtest_etf_consensus` 因共用同一函式自動套用。`py_compile` 通過。
    * **fixed by**：v0.0.4-dev（待使用者驗證）

51. [open] [bug#00108] [optimization] **期權殘差偏向未依 OI 加權、方向門檻非尺度不變——單一高價/低 OI 合約可主導、跨標的共用絕對美元門檻**
    * **問題描述**：使用者審查（第 2 點）。`_residual_bias` 以「逐張等權加總」`Σ買權殘差 − Σ賣權殘差` 得方向，單一高價或極低 OI 合約的殘差即可主導整檔標的；`compute_directional_verdicts` 又以跨所有標的統一的絕對門檻 `bias_min_abs=0.15` 美元判定，$600 與 $15 的標的採同一判準，不具尺度不變性。
    * **root cause**：殘差聚合等權且以絕對權利金加總；方向門檻為固定美元、未相對現價正規化。
    * **solution**：`_residual_bias` 改為**以未平倉量 OI 為權重的加權平均**（每股美元，缺 OI 退回權重 1），並分別回傳買/賣權側加權平均殘差供 bug#00109 使用。`compute_directional_verdicts` 門檻改為 `max(bias_min_abs, bias_min_pct/100×現價)`（新增 `bias_min_pct=0.03`）。結論卡文字改為「OI 加權殘差 $X/股」。已以合成資料驗證：高 OI(1000, 殘差 0.05) 與低 OI(1, 殘差 5.0) 兩合約，舊版加總 5.05 偏多主導、新版 OI 加權平均 ≈0.055 且低於門檻不誤判方向。`py_compile` 通過。
    * **fixed by**：v0.0.4-dev（待使用者驗證）

52. [open] [bug#00109] [optimization] **建倉 skew 無法分辨買方/賣方發起——賣出買權(covered call)、賣出賣權(sell put)會被誤判方向**
    * **問題描述**：使用者審查（第 3 點）。`compute_options_flow` 的 `underlying_skew` 僅由「新增未平倉」落在買權或賣權推方向，OI 增加無法區分買方或賣方發起——賣出買權（偏空/中性）會被當偏多、賣出賣權（偏多）會被當偏空。
    * **root cause**：OI 變化本身不含買/賣方向資訊；skew 子訊號單獨採信 call_pct 門檻。
    * **solution**：以「同側重定價殘差」交叉確認 skew——買權集中但買權側 OI 加權殘差為負（被壓價，多屬賣出買權）→ 不確認偏多；賣權集中但賣權側殘差為負（賣出賣權＝偏多）→ 不確認偏空；不確認時 skew 不計入方向並標 `skew_unconfirmed=True`，結論卡（`generate_verdict_cards`／`generate_grouped_analysis_card`）就地註明。雜訊帶 eps 取現價 0.02%。已以合成資料驗證：偏多 skew ＋ 買權殘差 −1.0 → 觀望且 skew_unconfirmed；＋1.0 → 看多。`py_compile` 通過。
    * **fixed by**：v0.0.4-dev（待使用者驗證）

53. [open] [bug#00110] [optimization] **方向訊號僅比視窗頭尾「兩點」快照，單一異常端點即翻轉整個結論**
    * **問題描述**：使用者審查（第 4 點）。`compute_symbol_trends` 僅以視窗最早 vs 最新各一筆快照計算方向，任一端點快照異常（報價髒、AUM 延遲）就翻轉整檔訊號，無平滑。
    * **root cause**：兩點端點估計，對端點雜訊零抵抗力。
    * **solution**：新增 `_median`／`_endpoint_view` 助手，`compute_symbol_trends` 改取視窗兩端各 `k=min(3, 天數//2)` 筆的中位數（權重／價格／AUM）作代表值，日期標籤仍取真實頭尾 span；僅 2 筆時 `k=1` 退化為原兩點比較（向後相容）。已以合成資料驗證：6 筆中僅末筆權重異常噴高 → 新版判 flat（不被單點誤導）、舊式真實兩點 5→9 仍為 up。期權端（`_residual_bias`／skew／IV 背離）同型穩健化列為後續，見 `INVESTMENT_LOGIC.md` 第十節。`py_compile` 通過。
    * **fixed by**：v0.0.4-dev（待使用者驗證）

54. [open] [bug#00111] [optimization] **跨模型可信度「就緒門檻用原始 n、顯著性用 ESS」尺度不一致——可能以 1 個有效樣本給權重**
    * **問題描述**：使用者審查（第 5 點）。`cross_model._reliability` 以原始 `n≥20` 判就緒給權重，但顯著性用 `ESS=floor(n/14)`；在 14 天前瞻期下 n=20 → ESS≈1，Wilson CI 近乎 (0,1)，卻仍可能給 0.2 權重。
    * **root cause**：就緒與顯著性判準採不同樣本尺度。
    * **solution**：`_reliability` 就緒門檻除 `n≥20` 外，另要求 `ESS≥3`（新增 `_READY_MIN_ESS`）；ESS 取自既有 `significance.ess`。已以合成資料驗證：n=20/ESS=1 → 權重 0（資料累積中）；n=40/ESS=3/未顯著 → 0.2；顯著且過多重檢定 → 1.0。`py_compile` 通過。
    * **fixed by**：v0.0.4-dev（待使用者驗證）

55. [open] [bug#00112] [optimization] **回測前後子區間穩定性以固定 0.5 判「一致」，會把低於基準的劣訊號標為穩定**
    * **問題描述**：使用者審查（第 7 點）。`backtest_stats._stability` 以 `hit>0.5` 判「前後一致」，非「兩段都贏基準」；基準本就 60%、命中僅 52/53% 的劣訊號會被標為「前後子區間一致」而誤導。
    * **root cause**：一致性門檻寫死 0.5，未對齊各方向的無技能基準。
    * **solution**：`_stability` 新增 `by_h` 參數，改以各半段依方向組成加權的無技能期望命中率為門檻（up→baseline_up_rate、down→1−baseline_up_rate），缺基準時退回 0.5（向後相容）；`attach_significance` 傳入 `by_horizon`。輸出新增 `early_expected`／`late_expected` 以利判讀。已以合成資料驗證：命中 0.6 而基準 0.62 → 判不一致（舊版誤判一致）；無基準 → 退回 0.5。`py_compile` 通過。
    * **fixed by**：v0.0.4-dev（待使用者驗證）

56. [open] [bug#00113] [review] **四大功能建議邏輯審查：第 6/8/9/10 點經評估維持現行設計（記錄理由）**
    * **問題描述**：使用者要求逐一檢視建議與回測邏輯的優化空間，共 10 點。除 bug#00107–00112 已改程式碼外，其餘 4 點經評估維持現行設計，於此與 `INVESTMENT_LOGIC.md` 第十節記錄決策理由，供日後追溯。
    * **root cause**：（非缺陷）屬設計取捨與內生性質。
    * **solution**：(6) 期權 skew／殘差維持等權——依各自回測可信度加權會破壞「結論卡邏輯＝被回測邏輯為同一函式」的核心紀律（見 `options_verdict_card.md`），且 conflict／skew 未確認機制已提供保護。(8)「近期重大事件」維持資訊性、不投方向票——沿用既有使用者決策；事件消化期與總經軟性傾向列為未來可選增強（需接線 TUI、改變事件定位）。(9) 類股回測目標（前瞻市值加權報酬）維持——與訊號同源的動能自相關為任何動能訊號的內生性質，已由基準＋超額與 ESS 部分抵銷。(10) 二項檢定 Bonferroni 維持跨前瞻期校正——畫面同時呈現多前瞻期，全體校正為正確保守作法，無樣本前瞻期不計入檢定數。已更新 `INVESTMENT_LOGIC.md` 第十節。
    * **fixed by**：v0.0.4-dev（待使用者驗證）







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

110. [open] [bug#00110] [UI|function] **期權分析結論卡介面升級：動態尋找最佳預測期 (+1~+60天)、60% 信心水準門檻控制與未來預期波動範圍 (Expected Move)**
     * **問題描述**：目前期權分析結論卡標題僅顯示「(2026-07-11~2026-07-22)」，使用者反應此格式讓投資建議看起來像是「舊數據紀錄」而非「針對未來的預測」。使用者明確要求：Output 必須動態導出未來 +1~+60 天中信心水準最高的前瞻預測期（例如 +12 天/14 天），信心水準需以 % 格式導出，且信心水準低於 60% 者（與 50/50 隨機賭博無異）必須自動降級為觀望。
     * **root cause**：結論卡標頭將歷史數據採樣視窗（Data Input Window）作為主標題時間戳，未顯式標註動態導出的「未來預測展望期 (+N 天 Target Horizon)」，且未導入 $>60\%$ 信心水準門檻防禦機制。
     * **solution**：1. `backtest_stats.py` 新增 `confidence_percentage_info` 與 `find_best_horizon_confidence` 函式：基於二項檢定 p 值計算 % 格式信心水準，並跨 $h \in \{1, 5, 10, 14, 30, 60\}$ 動態搜尋最佳前瞻天數 $h_{best}$ 與過濾門檻 (`confidence_pct >= 60.0`)。2. `options_analysis.py` 更新 `generate_grouped_analysis_card` 與 `_verdict_backtest_short`：標題動態標註 `預估展望：未來 +{h_{best}} 天`；整合 `compute_expected_move` 導出未來 30 天 $\pm 1\sigma$ 預估價格區間；若最高信心水準 $\le 60\%$，卡片自動降級為 `⚪ 觀望` 並提示未達 60% 有效門檻。已以 `py_compile` 與單元測試驗證通過。
     * **fixed by**：v0.0.4-dev（待使用者驗證）

111. [open] [bug#00114] [newfeature] **ETF 分析新增「每日主動選股多空」中間層；首頁 ETF 趨勢結論卡改為截取此結論**
     * **問題描述**：使用者審查現行 ETF 分析邏輯後指出，目前只有「個股跨基金共識」與「跨模型整體」兩端，缺少中間層——每一檔 ETF 自己這段視窗在主動加/減什麼、淨傾向偏多還偏空，也沒有一個「透過各 ETF 主動選股、觀察趨勢、by daily check 顯示多空建議」的每日方向讀數。另外首頁「📊 ETF趨勢結論」卡片與進階分析頁雖共用 `generate_etf_conclusions()`，但卡片並未明確呈現為「截取自 detail 分析的結論」。
     * **root cause**：`analysis.compute_symbol_trends` 只做「個股 × 跨 ETF」的彙總，未提供「per-ETF 主動選股淨傾向」與「跨基金傾向廣度 → 每日多空 stance」的中間聚合；首頁卡片缺少此每日方向的 headline。
     * **solution**：1. `analysis.py` 新增 `compute_etf_selection_tilt(report)`——完全從 `compute_symbol_trends()` 已算好的同一份 report 的 `raw_contributions`（雙真實訊號同向的個股加/減碼事件）衍生，不重算、不打網路：對每檔 ETF 取 `net_score=(加碼數−減碼數)/評估數`（分母含持平以稀釋雜訊，偏保守），分類 long/short/neutral，並附各檔 top_buys/top_sells；再聚合成每日廣度 `breadth=(偏多ETF−偏空ETF)/就緒ETF`，映射為每日 stance（long/short/neutral，就緒 ETF 為 0 時誠實回 insufficient「資料累積中」）。2. 新增 `etf_stance_phrase(tilt)` 供卡片與分析框共用同一顯示字串。3. 新增 `backtest_etf_selection_tilt()`（設計決策 C-b）：walk-forward，把每日 stance 對照「持有宇集市場代理」的前瞻報酬（該日所有有真實價格個股 `price_{T+h}/price_T−1` 的橫斷面中位數）驗證，`by_horizon` 形狀與 `backtest_etf_consensus` 相同、同接 `attach_significance`、資料簽章快取。4. `tui.py` `_build_etf_conclusions_panel` 於卡片最上面加一行每日主動選股多空（`etf_stance_phrase`），導引改指向「按 6 → 下方分析框」看完整依據，使卡片明確為「截取」關係。已以合成資料單元測試（long/short/insufficient 三情境＋回測命中）、`py_compile`、`scripts/verify_tui.py` 無頭（13/14 基準）驗證。
     * **fixed by**：v0.0.4-dev（待使用者驗證。決策 D2 選定 C-b；D3 每日多空視窗暫沿用 14 天，是否另設較短「日檢」窗待使用者定案）

112. [open] [bug#00115] [newfeature|UI] **ActiveETFsScreen 下方內嵌 detail 分析框（不分頁），成為主要 ETF detail 分析入口並顯示原因**
     * **問題描述**：使用者指出 `_build_etf_conclusions_panel` 有顯示，但真正的 detail analysis 結論與原因藏在 `ActiveETFsScreen` 再按一次 `a` 的 `AdvancedAnalysisScreen`；`ActiveETFsScreen`（按 6）本身只有排行/持股/歷史三張表，沒有結論與原因，導致卡片「按 6 查看完整報告」對不上。使用者要求：保留原 `ActiveETFsScreen`，在下方空白處提供一個方框內容呈現 detail 分析並顯示原因，不分頁。
     * **root cause**：detail 分析（結論＋原因）與 ETF 明細頁分屬兩個 Screen，`ActiveETFsScreen` 未內嵌任何分析結論，原因與明細不同頁。
     * **solution**：`tui.py` `ActiveETFsScreen` 版面於 `#etf-body`（改 3fr）下方、`Footer` 上方新增全寬可捲動的 `#etf-analysis-box`（2fr，`ScrollableContainer`，不分頁）；`run_analysis_compute()` 背景 worker 離線算好跨 ETF 共識 report＋其回測、每日主動選股 tilt＋其回測後，`_render_analysis()` 繪出三區塊：(A) 每日主動選股多空整體＋回測命中率＋覆蓋率（永遠顯示），(B) 跨 ETF 持股趨勢共識結論（原因，與首頁卡片、`a` 頁同一 `generate_etf_conclusions()`），(C) 選中左欄某檔 ETF 時顯示該檔主動選股明細（傾向/加減碼/top buys/sells）與其達跨基金共識的持股。選取 ETF 透過既有 `_handle_row` 觸發只重繪不重算。原 `AdvancedAnalysisScreen`（按 a）此次保留未動（D1 待使用者定案）。已以 `py_compile`、`scripts/verify_tui.py` 無頭（13/14 基準，`active_etfs_screen` 通過）、以及 Textual `run_test` pilot 驅動 worker 完成後兩條 render 路徑＋選取 hook 皆無例外驗證。
     * **fixed by**：v0.0.4-dev（待使用者驗證：需累積數日真實快照後，開啟「主動式ETF排行」確認下方分析框的每日多空、共識依據與選 ETF 明細正確顯示）

113. [open] [bug#00116] [function] **期權觀察清單 `c` 鍵由「刪除全部歷史重抓」改為「只重抓今日、保留歷史累積」**
     * **問題描述**：使用者檢視 `c 重載` 實際行為，確認它其實是刪除 `data/options_cache/history/` 內**全部** `*.jsonl`（所有標的的歷史，非僅當前清單）再只補回今天一筆——等於把 flow/背離/IV 位階/回測所依賴的多日累積一次歸零，且需再花數日重建。使用者要求改為只刷新今天、保留歷史。
     * **root cause**：`OptionsWatchlistScreen.action_clear_cache` 直接 `get_options_history_dir().glob("*.jsonl")` 全數 `unlink()`。因系統設計為「只逐日真實累積、不回填」，此全清屬破壞性重置；且 Footer/綁定標籤寫「重載/清除快取重載」與實際破壞性行為不符，易誤觸。又因 `append_options_daily_snapshot` 同日去重，單純重新抓取無法刷新今天已存的那筆，才會演變成「先全刪再抓」。
     * **solution**：`storage.py` 新增 `remove_options_daily_snapshot(underlying, date_str)`——只重寫該標的 jsonl、濾掉指定日期那一行，保留其餘歷史（比照 `prune_options_history` 的安全重寫）。`tui.py` `OptionsWatchlistScreen.action_clear_cache` 改為：對**目前清單**各標的移除「今天」那一筆（不動其他標的、不動歷史），移除後 `options_symbol_fresh()` 轉 False，既有 `run_background_fetch()` 便重新抓當天最新資料再 append 回去。綁定標籤「清除快取重載」→「重抓今日」、Footer 提示「c 重載」→「c 重抓今日」、`OptionsHelpScreen` 說明同步改為「重抓今日快照（只刷新今天、保留歷史累積）」。已驗證：`py_compile`（storage/tui）通過；以臨時資料夾單元測試 `remove_options_daily_snapshot`——3 日歷史移除今日後只剩前兩日、其他標的（AMD）完全不受影響、移除不存在日期為 no-op、移除今日後 `options_symbol_fresh` 正確轉 False（確保 run_background_fetch 會重抓）。**附帶**：此行為也讓使用者可在盤中用 `c` 覆蓋掉早上 OI 未結算的空快照（呼應 bug#00080 情境）。
     * **fixed by**：v0.0.4-dev（待使用者驗證：實機執行 TUI 於期權觀察清單按 `c`，確認只重抓今日、過往歷史仍在，且 flow/IV 位階/回測不再被歸零）

114. [open] [bug#00117] [newfeature] **投資建議寫作格式全面重構為三層（結論／判斷依據／公式 breakdown），主頁收斂為一句**
     * **問題描述**：五類投資建議（ETF 共識、每日主動選股多空、類股板塊、期權方向、近期重大事件、跨模型總結）各自的生成函式只輸出**單行字串 bullet**，同一份輸出同時餵主頁卡片與 detail 頁面，把方向結論、判斷依據、回測、財報、IV 全擠在一句。使用者要求全部改為三層寫作格式：(1) 先點出結論與多空方向；(2) 1–2 句「如何判斷此結論」；(3) 縮排 breakdown 解釋為何、給公式與帶入本標的數字的計算方式。主頁 `DashboardScreen` 卡片只顯示第一＋二層，儘量一句話結束。
     * **root cause**：呈現層與生成層耦合——結論字串在各 `generate_*` 函式內即已排版死，無結構化中介，無法對主頁/detail/公式頁做不同層級投影。
     * **solution**：`shared.py` 新增 `Recommendation` dataclass（rec_id/category/direction/verdict/basis/detail_sections）＋ `dashboard_line`（主頁一句話投影）/`detail_headline`/`render_detail_recs`（detail 投影，含 @click 連結）為單一真理來源。各模組新增結構化生成函式並把原字串函式改薄 wrapper：`analysis.generate_etf_recommendations`＋`etf_stance_recommendation`（`generate_etf_conclusions`/`etf_stance_phrase` 改 wrapper）、`sector_analysis.generate_sector_recommendations`（`generate_sector_conclusions` wrapper）、`options_analysis.generate_options_recommendations`、`cross_model.synthesize_cross_model` 回傳新增 `recommendation`、`shared.macro_recommendations`（`format_macro_analysis_lines` wrapper）。detail_sections 每 section 含公式／帶入數字／計算方式說明，並收納回測命中率＋顯著性、財報降權、部位一致性、IV 位階等全部量化附註。主頁三張卡片與跨模型卡改一句話；ETF/類股/期權/事件 detail 畫面改渲染結構化 recs。維持「結論＝被回測＝同一函式」紀律：主頁/detail/公式頁皆同一份 rec 的三種投影。**已驗證**：六模組 `py_compile` 通過；合成單元測試涵蓋五類（verdict/basis/detail_sections 非空、方向正確含平手歸 mixed 與 conflict→觀望、`dashboard_line` 單行、rec_id/token 唯一、wrapper 仍回字串）；`scripts/verify_tui.py` 無頭 13/14（`empty_positions` 為既有沙盒環境失敗基準，非迴歸）。
     * **fixed by**：v0.0.5-dev（待使用者驗證：實機執行 TUI，確認 ETF/類股/期權/事件/跨模型五類建議皆呈三層寫作格式，主頁卡片為一句話結論＋判斷依據）

115. [open] [bug#00118] [newfeature] **每則投資建議後可點選「🔍 查看公式細節」開啟公式細節頁（RecommendationDetailScreen），Esc 返回**
     * **問題描述**：三層寫作格式的第三層 breakdown（完整公式、帶入本標的數字、計算方式說明、全部量化附註）不宜擠在 detail 畫面內文，使用者要求做成「點選進去下一頁確認細節」的獨立頁面；且避免固定 hotkey 被各畫面既有按鍵佔用，改用每則建議後的可點選連結。
     * **root cause**：原 detail 畫面把建議渲染成 Rich `Panel`/`Group` 物件（Textual `@click` markup 會被 Rich 吞掉，無法點選），且無承載第三層完整公式的專屬頁面。
     * **solution**：`tui.py` 新增 `RecommendationDetailScreen(Screen)`——頂部顯示結論＋判斷依據，下方 `VerticalScroll` 逐 section 顯示「公式／帶入此標的數字／計算方式說明」，純顯示零計算，Esc/q 返回。新增 `_FormulaDrillMixin`（`action_show_formula(token)` 推入細節頁），六個畫面（Dashboard 跨模型卡、ActiveETFs、AdvancedAnalysis、Sector、OptionsWatchlist、UpcomingEvents）皆混入。各建議 render 站點改用 `render_detail_recs()` 輸出 **markup 字串**（非 Rich Panel，使 `[@click=screen.show_formula('r{i}')]🔍 查看公式細節 ›` 可點選），並把 `{token: rec}` 存 `self._recs_by_id`；以 ASCII 安全 token（r0/r1…）避開 Chinese/引號破壞 markup 與 hotkey 佔用問題。原 Rich Panel 邊框改由 Textual CSS `border` ＋ `border_title` 保留框樣。事件總經解析另放 `#events-macro` markup Static。**已驗證**：無頭 pilot 端對端測試——點選連結確實推入 `RecommendationDetailScreen` 且綁定正確 rec、Esc 正確返回；Advanced/Sector/Options 三畫面以 stub 背景 worker 掛載無誤、`_recs_by_id` 正確生成；`verify_tui.py` 13/14 基準不變。
     * **fixed by**：v0.0.5-dev（待使用者驗證：實機執行 TUI，於各 detail 畫面點選「🔍 查看公式細節」開啟公式頁看到公式與帶入數字、Esc 返回）

116. [open] [bug#00119] [UI] **期權觀察清單版面微調：各標的期權分析表改單行呈現、下方買/賣權合約表不再被上方壓縮到過小**
     * **問題描述**：使用者要求「各標的期權分析」表盡量單行呈現、避免縮排換到第二行；並指出下方買權/賣權合約明細呈「固定面板」被上方佔滿、可視範圍過小，要求最佳化高度、避免使用固定面板壓縮閱讀空間。
     * **root cause**：(1) `_render_portfolio` 的「預期波動」格內用 `\n` 塞入第二行「損益兩平 ±$…」，使每列變成兩行、整表變高。(2) `OptionsWatchlistScreen` CSS 的 `#ow-top`（期權分析總表＋分析結論卡的可捲動容器）`max-height: 65%`——上方內容一長就吃掉最多 65% 高度，`#ow-body`（買/賣權合約表，1fr）只剩約 35%，可視列數過少。
     * **solution**：(1) `_render_portfolio` 把「損益兩平」由第二行改為**獨立欄位**（表頭新增「損益兩平」欄），預期波動格恢復單行 `±$X (±Y%,Nd) ⚠`；合計列同步補一欄空白對齊；footer 說明壓成一行精簡版。(2) CSS `#ow-top` `max-height` 由 65% 降到 **42%**（內容超出時於該容器內捲動即可），並給 `#ow-body` 加 `min-height: 14` 保底，確保下方買/賣權合約表恆有足夠可視高度、不被上方壓縮。已驗證：`py_compile` 通過；單元檢查預期波動/損益兩平兩欄輸出皆為單行（無 `\n`）；以 Textual `Stylesheet` 解析器確認 `max-height:42%` 與 `min-height:14` 語法有效。
     * **fixed by**：v0.0.5-dev（待使用者驗證：實機執行 TUI 進入「期權觀察清單」，確認各標的期權分析表每列單行、損益兩平獨立成欄，下方買/賣權合約表可視範圍明顯變大不再被壓縮）


