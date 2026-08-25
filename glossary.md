# AssetTrack 功能名稱對照表

本文件對照 `assettrack/tui.py` 與相關模組中的 feature、英文程式名稱、中文名稱與**目前實際功能**。
最後依據：2026-08-25（已同步架構文件 [`blockDiagram.md`](./blockDiagram.md)：績效追蹤期間回補／平空單從現金扣款，且加碼合併必須含 `instrument_type`）。

`compose`、`on_mount`、`on_key` 與 `on_*` 為 Textual 框架生命週期／事件處理名稱，維持原名；本表不逐一重複列出。已刪除或不再由 TUI 呼叫的名稱標「（已移除）」或「（函式庫，畫面不呼叫）」，避免舊文件把研究引擎當成現行建議。

領域詞彙（Performance Tracking、Forecast Record、Champion 等）見 [`CONTEXT.md`](./CONTEXT.md)。溝通協定與資料流見 [`blockDiagram.md`](./blockDiagram.md)。

架構、技術或既有設計一改，必須**同一輪**更新本檔與 `blockDiagram.md`。強制規則：`.cursor/rules/architecture-docs.mdc`。更新步驟：`.agents/skills/sync-architecture-docs/SKILL.md`。

## 名稱慣例

| 程式命名模式 | 中文意義 | 實際用途 |
|---|---|---|
| `*Screen` | 畫面 | 一個完整可切換的 TUI 畫面。 |
| `*Modal` | 彈出視窗 | 暫時要求使用者輸入、選擇或確認的視窗。 |
| `*Editor` | 編輯視窗 | 本質是 Modal，用來編輯一份清單（例如 ETF 觀察清單）。 |
| `action_*` | 使用者操作 | 由快捷鍵、按鈕或選單觸發的公開動作。 |
| `_render_*` / `_build_*` | 呈現／建立畫面 | 將現有資料格式化、繪製為表格或卡片；不應做長時間網路請求。 |
| `_fetch_*` / `run_*` | 抓取／執行工作 | 讀取外部資料或執行完整背景工作。 |
| `_handle_*` | 結果處理 | 接收使用者選擇或背景工作的結果，決定下一步流程。 |
| `_apply_*` | 套用變更 | 將已驗證的修改寫入持倉或設定資料。 |
| `_` 前綴 | 內部方法 | 僅供同一 module 或 class 內部使用，不是對外 API。 |
| `Recommendation` | 結構化建議 | 三層寫作格式的單一真理來源：結論／依據／公式細節。 |

## 溝通與執行機制（不是畫面，但是命名會出現在程式與文件）

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `push_screen` / `dismiss` | 畫面堆疊／回傳結果 | Textual 導航協定。沒有內部 HTTP、沒有 `post_message` 自訂訊息。 |
| `@work(thread=True)` | 背景工作執行緒 | 網路與重計算離開 UI thread。 |
| `call_from_thread` | 回 UI 執行緒 | Worker 更新畫面的唯一合法通道。 |
| `_fetch_activity` | 抓取狀態列文案 | `AssetTrackApp` 記憶體 dict；`_tick_header` 每秒讀取。 |
| `_FormulaDrillMixin` | 公式細節點選混入 | Rich `[@click=screen.show_formula('rN')]` → `RecommendationDetailScreen`。 |
| `_active_params` | 生效門檻參數 | 優先讀 `{user}_champion_params.json`，否則 legacy 校準，再否則預設。 |
| `_DashboardAnalysisInputs` | 主頁分析一次載入包 | 同一輪渲染共用 ETF／期權／板塊快照，避免三張卡各讀三次。 |
| Champion 契約 | QuantTrade 參數檔 | `data/{user}_champion_params.json` 的 `params`。AssetTrack 只讀不寫。 |
| 磁碟匯流排 | 快照共享 | 背景 worker 寫 `history/*.jsonl`，各畫面稍後用同一 `load_*` 讀取。 |
| `ATENC1:` / `ATENC1\n` | 保險庫密文前綴 | 文字檔與 SQLite 的 Fernet 包裝。 |
| `touchid_helper` | Touch ID 子行程 | `subprocess` + argv 帳號 + **exit code**（0/1/2）；不讀 stdout。 |

## 1. 應用程式啟動與流程控制

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `main` | 命令列主入口 | 載入 `.env`、解析 `--user/-u`，並啟動 TUI。 |
| `run_tui_dashboard` | 啟動 TUI 儀表板 | 建立並執行 `AssetTrackApp`。 |
| `AssetTrackApp` | AssetTrack 主應用程式 | 管理登入、初始導覽、Dashboard 進入／離開與跨畫面背景維護。 |
| `_handle_login_complete` | 處理登入完成 | 登入後依持倉是否存在，導向 Dashboard 或新手導覽。 |
| `_handle_onboarding_choice` | 處理新手導覽選擇 | 建立範例持倉、手動新增第一筆持倉，或直接進入空白看板。 |
| `_start_dashboard` | 啟動主儀表板 | 讀取匯率、保存目前使用者與持倉狀態，推入 Dashboard；啟動 30 分鐘全域補抓。 |
| `_background_data_refresh` | 全域背景資料更新 | 每 30 分鐘依新鮮度冪等補抓當日 ETF、期權與板塊快取。 |
| `_kickoff_research_ingest_once` | 首次研究資料補抓 | 第一次報價刷新成功後觸發一次全域補抓。 |
| `_handle_dashboard_exit` | 處理儀表板離開 | 依登出或結束程式的結果，`lock_vault` 後回登入畫面或關閉 App。 |

## 2. 登入、帳號與保險庫

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `LoginScreen` | 登入畫面 | 顯示帳號登入入口，支援 Touch ID、密碼登入與註冊。 |
| `run_touchid_auth` | Touch ID 背景驗證 | 在背景執行 `touchid_helper`，避免阻塞介面。 |
| `_login_success` | 登入成功處理 | `seal_user_files`、載入持倉；必要時開 SEC 身分視窗。 |
| `PasswordModal` | 密碼驗證視窗 | 輸入並驗證既有帳號密碼（最多 3 次）。 |
| `RegisterModal` | 註冊帳號視窗 | 建立帳號、解鎖保險庫；可同時 opt-in 績效追蹤。 |
| `OnboardingModal` | 新手導覽視窗 | 讓新使用者選擇範例資料、手動新增或空白開始。 |
| `LogoutConfirmModal` | 登出確認視窗 | 避免誤觸造成登入狀態結束。 |
| `SECIdentityModal` | SEC 身分視窗 | 依 Fair Access 收集名稱＋信箱並取得明示同意。 |
| `SECIdentityDeleteConfirmModal` | 刪除 SEC 身分確認 | 二次確認後從 Keychain 刪除；該帳號停止自動更新 13F。 |
| `account_exists` / `register_account` / `verify_password` | 帳號 API | `auth.py`：Keychain 存 PBKDF2 雜湊。 |
| `unlock_vault` / `unlock_vault_with_touchid` / `lock_vault` | 開關保險庫 | 把 32-byte 資料金鑰載入或清出行程記憶體。 |
| `seal_user_files` | 封存使用者檔 | 登入後把舊明文持倉／偏好／SQLite／績效帳本改寫成 Fernet。 |
| `read_protected_text` / `write_protected_text` / `protected_sqlite` | 受保護 I/O | 保險庫已解鎖才加密寫入。 |

## 3. 主儀表板與投資組合總覽

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `DashboardScreen` | 投資組合主儀表板 | 顯示持倉、總資產、損益、券商分布、近期事件及三張分析卡。 |
| `_get_cached_usdtwd_rate` | 取得快取美元／台幣匯率 | 以一小時快取減少重複查詢匯率；渲染路徑不打網路。 |
| `_calc_weights` | 計算持倉權重 | 依美元等值計算每個持倉在投資組合的比例。 |
| `_build_metrics_panel` | 建立投資組合總覽卡 | 呈現資產總值、未實現損益與 Portfolio Beta 三格（已移除持倉數／券商數）。 |
| `_build_holdings_table` | 建立持倉表格 | 依券商整理標的、數量、成本、報價、市值、損益；支援表格內直接操作。 |
| `_build_broker_panel` | 建立券商分布卡 | 呈現各券商／帳戶分布、`cash_ratio`、總／淨曝險與槓桿。 |
| `_build_recent_events_panel` | 建立近期事件摘要卡 | Holdings 右側常駐，未來 30 天最多 8 筆（另見第 5 節）。 |
| `_build_etf_conclusions_panel` | 建立 ETF 趨勢結論卡 | **只顯示觀察清單**上、本視窗有確認買賣的標的（最多 3 行）。未設定清單則提示去鍵 `6`。不再呼叫 `generate_etf_conclusions`。 |
| `_build_options_flow_panel` | 建立期權觀察卡 | 已觀察市場階段 + ATM 貴賤檔數。**不輸出股價漲跌預測。** |
| `_build_sector_consensus_panel` | 建立類股預測卡 | 與板塊頁共用 `generate_sector_recommendations`：2-of-3 多方或 breadth+SMA5/20 空方，預測未來 10 個交易日。 |
| `_load_dashboard_analysis_inputs` | 一次載入三卡資料 | 離線讀 Champion 參數與三套 JSONL。 |
| `_refresh_analysis_panels` | 節流重算分析卡 | 持倉簽名不變且 5 分鐘內不重算。 |
| `_marked` | 表格多選標記集合 | Holdings 表格 `space` 多選、`e` 編輯、`x` 刪除。 |
| `_render_all` | 重繪整個儀表板 | 依目前記憶體資料更新 Dashboard 所有表格與卡片。 |
| `_tick_header` | 更新頁首時鐘與常駐狀態列 | 每秒更新時間、刷新狀態並重繪 `#status-bar`。 |
| `_do_refresh_worker` | 背景更新投資組合 | 讀取持倉、匯率、無風險利率與即時報價，完成後重繪；可觸發日曆與首次研究補抓。 |
| `action_refresh_now` | 立即更新 | 使用者主動觸發報價與看板更新。 |
| `action_save_snapshot` / `run_save_snapshot` | 儲存投資組合快照 | 在背景將目前投資組合保存為每日歷史快照。 |
| `action_calibration` | （已移除）投資建議校準 | 2026-08-06 隨策略實驗室搬到 QuantTrade；Dashboard 不再有 `k`。 |
| `PerformanceTrackingScreen` | 使用者績效比較頁 | 完整資產的現金流調整報酬，以及 QQQ／VT 影子基準等值、美元差額與領先／落後百分比。 |
| `PerformanceTrackingCancelConfirmModal` | 取消績效追蹤確認視窗 | 停止目前追蹤區間但保留歷史；重新啟用時標示追蹤斷層。 |
| `CashFlowModal` | 出入金宣告視窗 | 記錄入金來源或出金用途、管道、券商帳戶、幣別、金額與備註。 |
| `PortfolioPerformanceTracker` | 投資組合績效追蹤 module | 管理 opt-in、追蹤斷層、JSON 帳本、週日估值、影子 benchmark 與追蹤期間的持倉資金守恆。買進與回補空單扣現金；平多單把現值轉回現金；平空單從現金扣回補成本。合併部位時比對券商+帳戶+代碼+`instrument_type`。 |
| `action_performance_tracking` | 開啟績效比較（`9`） | 從 Dashboard 進入完整資產與 QQQ／VT 的比較頁。 |
| `action_deposit` / `action_withdrawal` | 宣告入金／出金（`i`／`o`） | 調整現金並以相同資金流同步 benchmark。 |
| `action_logout` | 安全登出 | 開啟確認視窗並結束目前登入。 |

## 4. 持倉管理

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `AddPositionModal` | 新增／編輯持倉視窗 | 輸入或修改股票、ETF、期權與 USD／TWD 現金部位；支援「儲存並繼續」、Symbol 自動推斷市場／幣別。 |
| `PositionActionsModal` | 持倉操作選單 | 提供備註、類別、幣別、券商帳戶與刪除等操作。 |
| `FieldEditModal` | 單一欄位編輯視窗 | 編輯一個文字或選項欄位。 |
| `DeleteConfirmModal` | 刪除持倉確認視窗 | 要求使用者確認刪除動作。 |
| `Holding` | 持倉聯合型別 | `Position \| CashPosition`。 |
| `action_add_position` | 新增部位（`1`） | 開啟 `AddPositionModal` 批次新增。績效追蹤開啟時走 `apply_position_purchase`（正數買進／回補；數量加總為 0 則移除該筆）。 |
| Holdings 表格直接操作 | 就地編輯／刪除／多選 | `Enter` 就地編輯、`e` 編輯整筆、`x` 刪除、`space` 多選。 |
| `_handle_field_edit` | 處理持倉欄位修改 | 驗證並更新代號、類型、數量、成本或市場。 |
| `_apply_metadata_edit` | 套用持倉附加資料 | 寫入備註、類別、計價幣別與成本幣別。 |
| `_apply_broker_account_edit` | 套用券商／帳戶修改 | 修改券商與帳戶，必要時合併重複持倉。 |
| `_handle_delete_confirm` | 處理持倉刪除確認 | 確認後移除持倉（追蹤中視為賣出轉現金）、保存並刷新。 |

## 5. 重大事件與經濟日曆

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `UpcomingEventsScreen` | 近期重大事件畫面 | 持倉／SOX 財報與 FED/NFP/CPI 月曆；已發生事件帶實際值。 |
| `TimezoneInputModal` | 事件時區輸入視窗 | 鍵 `T`：任意 IANA 時區，預設 `Asia/Taipei`，寫入使用者偏好。 |
| `RecommendationDetailScreen` | 公式細節畫面 | 投影一則 `Recommendation` 的第三層公式／帶入數字／說明。 |
| `_fetch_upcoming_events_worker` | 背景抓取近期事件摘要 | 為 Dashboard 抓未來財報與總經事件（6 小時新鮮、失敗 15 分鐘重試）。 |
| `run_calendar_fetch` | 抓取完整事件日曆 | 過去 30 天至未來 90 天的財報與總經；歷史只留當月與上月。 |
| `run_macro_readings_fetch` | 抓取總經實際值 | FRED：核心 CPI／PCE、NFP、失業率、聯邦資金利率。 |
| `macro_recommendations` | 總經資訊性建議 | `direction=None`，不投多空票。 |
| `_format_cpi_conclusion` | 格式化 CPI 結論 | 已公布 CPI 及下次 Fed 會議機率。 |
| `_format_fed_conclusion` | 格式化 Fed 決策結論 | 已發生的 Fed 利率決策。 |
| `_format_earnings_conclusion` | 格式化財報結論 | 已公布財報的營收、CAPEX、EBIT、FCF 與 YoY。 |
| `_render_monthly_calendar` | 繪製月曆事件表 | 將事件依月份排成 Rich 月曆表格。 |
| `action_upcoming_events` | 開啟近期重大事件 | 從 Dashboard 進入完整事件日曆。 |

## 6. 主動式 ETF 分析與 13F

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `ActiveETFsScreen` | 主動式 ETF／13F 機構畫面 | AUM > US$5B 美國主動式 ETF（依真實持股分類、分類內按 AUM 排序）；預設頁是觀察清單建議。 |
| `EtfHelpScreen` | ETF 說明畫面 | 鍵 `h`：解釋雙真實訊號、觀察清單與 13F 限制。 |
| `EtfCacheClearModal` | 清除 ETF 快取確認 | 鍵 `c`：刪本機 ETF 快取後重抓。 |
| `EtfWatchlistEditor` | ETF 觀察清單編輯視窗 | 鍵 `w`：決定主頁與建議頁要追蹤哪些美股標的。 |
| `AdvancedAnalysisScreen` | ETF 進階趨勢分析畫面 | 鍵 `a`：研究全表（個股買賣＋13F）。**不是** Recommendation 卡片。 |
| `_fetch_and_cache_etf_symbols` | 抓取並快取 ETF 資料 | 批次取得績效、AUM、持股及價格並 append 每日 JSONL。 |
| `run_background_fetch` | ETF 背景資料載入 | 由 ETF 畫面觸發資料取得與快取更新。 |
| `run_analysis_compute` | ETF 建議重算 | 離線跑 `compute_symbol_trends` + `backtest_etf_consensus` + `render_etf_advice_view`。 |
| `_render_ranking_tables` | 繪製 ETF／機構排行表 | 依持股分類與 AUM／13F 申報市值更新追蹤清單。 |
| `_render_holdings` | 繪製 ETF 持股明細 | 顯示所選 ETF 的成分、權重、估算股數與資產配置。 |
| `_render_history` | 繪製 ETF 歷史資料 | 顯示已累積的 ETF 持股變化紀錄。 |
| `_run_analysis` | 執行 ETF 趨勢分析 | 進階頁：`compute_symbol_trends` + `compute_institution_trends`。 |
| `compute_symbol_trends` | 計算持股趨勢 | 14 日窗、股數Δ與權重Δ（0.5pp）必須同向。 |
| `watchlist_etf_activity` | 觀察清單活動列 | 把趨勢報告過濾成使用者關心的標的。 |
| `render_etf_advice_view` | 繪製 ETF 建議頁 | 每檔觀察標的做成 `Recommendation`（含來源未更新／觀望）。 |
| `generate_etf_recommendations` | （函式庫）完整 ETF 建議 | 個股多數性＋大類輪動＋規模性大額。**TUI 主路徑不呼叫。** |
| `generate_etf_conclusions` | （函式庫）完整 ETF 一句話 | 上一列的薄 wrapper。主頁已改走觀察清單。 |
| `compute_institution_trends` | 計算 13F 趨勢 | 申報股數、5% 相對門檻、兩季比較；期權不推論多空。 |
| `ensure_active_etf_universe` | 維持主動式宇宙 | Yahoo screener，每日最多一次。 |
| `ensure_hedge_fund_filings` | 維持 13F 快取 | EDGAR HTTPS + Fair Access User-Agent。 |
| `action_advanced_analysis` | 開啟 ETF 進階分析 | 從 ETF 排行頁進入研究全表。 |
| `action_sec_identity` | SEC 身分（ETF 頁 `s`） | 查看遮罩聯絡資訊、修改或刪除。 |

## 7. 期權觀察與風險（無股價方向建議）

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `OptionsWatchlistScreen` | 期權觀察清單畫面 | 預期波動、ATM 貴賤（IV vs RV）、持倉淨 Greeks。波動貴賤欄可 ↑↓／Enter 看每日 IV−RV。**不預測股價方向。** |
| `OptionRichnessHistoryScreen` | 波動貴賤走勢畫面 | 單檔每日 ATM IV、RV、差距與財報剩餘天數（<10 天才註記）。 |
| `_run_analysis` | 執行期權觀察 | 計算預期波動、權利金貴賤與部位 Greeks 並重繪。不呼叫方向建議函式。 |
| `_render_portfolio` | 繪製各標的期權分析 | 預期波動、IV/RV、Call/Put 溢價與持倉淨 Greeks。 |
| `compute_observed_regime` | 計算已觀察市場樣態 | 近 6 個 session、±2% 門檻、60% 廣度；只描述已發生狀態。 |
| `richness_from_history` / `richness_series` | 每日 ATM IV − RV | RV＝當日往前 20 個交易日；走勢列近 90 個日曆天。 |
| `format_richness_history` | 格式化波動貴賤走勢 | 把每日 IV、RV、差距與走勢畫成明細。 |
| `compute_expected_move` | 計算預期波動 | ATM IV 推 ±1σ（係數預設 0.85）。 |
| `compute_portfolio_greeks` | 計算持倉淨 Greeks | 僅選擇權部位。 |
| `_underlyings_from_positions` | 從持倉取得期權標的 | 取出股票、ETF 或期權持倉的標的代號。 |
| `_watchlist_underlyings` | 建立期權觀察標的清單 | 合併持倉標的與手動加入的標的，排除台股。 |
| `_fetch_and_cache_options_underlyings` | 抓取並快取期權資料 | 批次抓期權鏈、財報日期，保存每日真實快照。 |
| `run_background_fetch` | 期權背景資料載入 | 從觀察清單發起快取更新與資料重繪。 |
| `_refresh_underlying_spots` | 更新標的現貨價格 | 批次更新觀察標的的現價。 |
| `_refresh_underlying_closes` | 更新標的日收盤 | 抓近兩個月收盤供已實現波動。 |
| `AddTickerModal` / `RemoveTickerModal` | 新增／移除觀察標的視窗 | 管理不屬於持倉的手動期權觀察標的。 |
| `action_add_ticker` / `action_remove_ticker` | 新增／移除觀察標的 | 開啟相對應的標的管理流程。 |
| `generate_options_recommendations` | （函式庫，畫面不呼叫）完整期權建議 | 含方向與策略映射。測試鎖定 TUI 不得 import。 |
| `compute_directional_verdicts` | （函式庫，畫面不呼叫）雙訊號方向 | Dollar Delta OI + BS 殘差。驗證為 UNDERPOWERED。 |
| `assess_option_forecast` | （函式庫，畫面不呼叫）proper-score 閘門 | n、Brier、命中、穩定度未過則觀望。 |
| `CalibrationScreen` / `CalibrationModal` | （已移除）校準畫面 | 2026-08-06 搬到 QuantTrade。AssetTrack 只讀 Champion 或 legacy 狀態。 |
| `run_backtest` | （進階／函式庫）執行訊號回測 | 期權方向回測仍在程式庫；觀察清單畫面不再顯示方向回測。 |
| `OptionsHelpScreen` | 期權指標說明畫面 | 說明 IV、已實現波動、貴賤溢價、Greeks 與「不是股價預測」限制。 |
| `action_help` | 開啟期權說明 | 從期權觀察頁進入使用說明。 |

## 8. 類股板塊分析

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `SectorAnalysisScreen` | 類股板塊分析畫面 | 管理板塊群組；顯示成員、廣度與**未來 10 個交易日**複合預測。 |
| `SectorGroupModal` | 板塊群組編輯視窗 | 新增或編輯板塊名稱與成員股票代號。 |
| `_fetch_and_cache_sector_groups` | 抓取並快取板塊資料 | 批次取得成員市場資料與每日快照。 |
| `_recompute_flows` | 重新計算板塊票與預測 | `detect_broad_flow` + 預測模型的 confirmation + `generate_sector_recommendations`。 |
| `_render_groups` | 繪製板塊列表 | 顯示已建立板塊與其共識／預測概況。 |
| `_render_conclusions` | 繪製板塊結論 | 可點選公式細節的 `Recommendation` 列表。 |
| `_render_members` | 繪製板塊成員明細 | 選定板塊內各標的的價格與漲跌。 |
| `detect_broad_flow` | 廣度 3-of-5（Vote A） | 當日需廣度與市值加權報酬同向；5 日中至少 `min_days` 日同向。 |
| `assess_sector_composite` | 合成三票 | 2 張多方且零空方 → 看多候選；breadth 空且 SMA5<SMA20 → 風險警示。 |
| `generate_sector_recommendations` | 產生類股預測建議 | 預測窗 10 個 NYSE session。空方文案不是放空指令。`backtest` 參數僅相容保留。 |
| `generate_sector_conclusions` | 類股預測一句話 | 上一列的主頁投影。 |
| `build_prediction_model` | 建立確認／條件機率模型 | 背景快取。畫面只用 `sector_confirmation`（Vote B/C）。 |
| `generate_prediction_recommendations` | （函式庫，畫面不呼叫）1–3 日條件機率 | 僅測試與研究；TUI 不顯示 🔮 列。 |
| `backtest_sector_flow` | 舊廣度 walk-forward | 仍可計算；不再寫進 2-of-3 預測卡當命中率。 |
| `action_refresh_now` | 立即更新板塊資料 | 強制重新抓取並計算板塊資訊。 |
| `action_add_group` / `action_edit_group` / `action_delete_group` | 新增／編輯／刪除板塊 | 管理使用者自訂板塊群組。 |

## 9. 績效、曝險與共用領域物件

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `TrackingState` | 追蹤開關狀態 | 是否啟用、啟用時間、是否有 Tracking Gap、基準清單。 |
| `CashFlow` | 外部資金流 | 入金／出金＋當時 QQQ／VT 收盤。 |
| `ValuationSnapshot` | 估值快照 | 完整資產美元值與基準價。 |
| `PerformanceReport` | 績效報告 | 現金流調整報酬與各基準 Performance Gap。 |
| `YFinanceBenchmarkPrices` | 基準報價提供者 | 14 日回看收盤，供影子單位數。 |
| `calculate_portfolio_exposure` | 計算投資組合曝險 | 股票 1x、槓桿 ETF 倍數、期權 Δ 等值；缺報價不輸出假比例。 |
| `PortfolioExposure` | 曝險結果 | 總／淨曝險、槓桿、標準／槓桿 ETF／期權分桶。 |
| `calculate_cash_ratio` | 現金比例 | <5% 進攻、>20% 防守、其間中性。 |
| `Recommendation` | 結構化建議 | `rec_id`／`category`／`direction`／`verdict`／`basis`／`detail_sections`。 |
| `dashboard_line` / `detail_headline` / `render_detail_recs` | 三層投影 | 主頁一句話、分析頁兩層＋連結、公式頁完整第三層。 |
| `is_taiwan_position` | 台股判定 | 投資建議排除台股的唯一來源。 |
| `position_stance_by_symbol` | 持倉淨多空 | 與訊號方向交叉提示，非加減碼指令。 |

## 10. 儲存、回測與驗證（非畫面）

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `load_manual_positions` / `save_manual_positions` | 讀寫持倉 | 加密 JSON。 |
| `load_*_daily_snapshots` / `append_*_daily_snapshot` | 讀寫逐日真實快照 | ETF／期權／板塊 JSONL；不回填。 |
| `prune_*_history` | 修剪歷史 | 預設 730 天；期權貴賤走勢另用 90 天。 |
| `load_etf_watchlist` / `save_etf_watchlist` | ETF 觀察清單檔 | 明文 JSON。未設定則主頁 ETF 卡不編造全宇宙建議。 |
| `load_options_watchlist` / `save_options_watchlist` | 期權手動標的檔 | 明文 JSON。 |
| `load_sector_groups` / `save_sector_groups` | 板塊定義檔 | 每使用者一份。 |
| `backtest_etf_consensus` | ETF 共識回測 | 與畫面同一 `compute_symbol_trends`；Wilson／二項／ESS。 |
| `backtest_verdicts` | 期權方向回測 | `calibration.py`；TUI 不展示方向結果。 |
| `validate` | 家族盲方向驗證 | `direction_forecast_validation.validate` → PASS／FAIL／UNDERPOWERED。 |
| `ensure_state` / `default_params` | 唯讀校準狀態 | `{user}_calibration.json`。`apply_pending` 拋 `CalibrationReadOnlyError`。 |
| `calibration_status_label` | 校準狀態標籤 | 進階 ETF 研究表表頭可能顯示；沒有調參 UI。 |

## Dashboard 導航對照

| Dashboard 動作 | 使用者看見的中文功能 | 目的地 |
|---|---|---|
| `action_add_position` | 新增部位（`1`） | `AddPositionModal`（Holdings 表格另支援 `e`/`x`/`space`）。 |
| `action_refresh_now` | 立即重整（`2`／`r`） | Dashboard 報價與資料刷新。 |
| `action_logout` | 安全登出（`3`／`q`） | 登出確認視窗。 |
| `action_upcoming_events` | 近期重大事件（`4`） | `UpcomingEventsScreen`。 |
| `action_save_snapshot` | 儲存快照（`5`） | 背景保存投資組合快照。 |
| `action_active_etfs` | 主動式 ETF 排行（`6`） | `ActiveETFsScreen`（`j` 建議、`w` 觀察清單、`a` 研究全表、`s` SEC 身分）。 |
| `action_options_watchlist` | 期權觀察清單（`7`） | `OptionsWatchlistScreen`。 |
| `action_sector_analysis` | 類股板塊分析（`8`） | `SectorAnalysisScreen`。 |
| `action_performance_tracking` | 使用者績效比較（`9`） | `PerformanceTrackingScreen`。 |
| `action_disable_tracking` | 取消績效追蹤（績效頁 `d`） | 經確認後停止目前追蹤區間、保留資料並解除持股管理限制。 |
| `action_deposit` / `action_withdrawal` | 宣告入金／出金（`i`／`o`） | `CashFlowModal`。 |
| `action_calibration` | （已移除）投資建議校準 | 不再綁 Dashboard `k`；參數改讀 Champion 契約。 |
| Holdings 公式連結 | 查看公式細節 | `RecommendationDetailScreen`（事件／ETF 建議／類股預測）。 |

## 已刪除或不再由本套件實作（避免舊名回流）

| 舊名稱 | 現況 |
|---|---|
| `cross_model.py` / 跨模型摘要卡 | 已刪除。主頁只剩類股、期權觀察、ETF 觀察清單三卡。 |
| `Experiment*` / `FeedbackCycle` / 鍵 `0` | 已搬到 QuantTrade。 |
| `CalibrationScreen` / `CalibrationModal` / 鍵 `k` | 已移除。 |
| `AdjustPositionsModal` / `ChoosePositionModal` | 已由 Holdings 表格直接操作取代。 |
| 期權「看多／看空」正式建議 | 畫面改為已觀察樣態；方向引擎留在函式庫。 |
| 類股「5 日中 3 日廣度同向」作為正式預測 | 歷史驗證 FAIL。現行改 2-of-3／風險警示，窗為 10 個交易日。 |
| `INVESTMENT_LOGIC.md` | 工作樹已刪。現行建議邏輯以 `blockDiagram.md` 第 11 章為準。 |
