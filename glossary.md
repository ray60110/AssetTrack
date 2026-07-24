# AssetTrack 功能名稱對照表

本文件對照 `assettrack/tui.py` 中主要 feature、英文程式名稱、中文名稱與實際功能。
最後依據：2026-07-24（已同步 bug#00098 版面精簡、bug#00091 移除台股 ETF、bug#00096 跨模型總結與常駐狀態列、bug#00107–00113 投資邏輯修正）。`compose`、`on_mount`、`on_key` 與 `on_*` 為 Textual 框架生命週期／事件處理名稱，維持原名；本表不逐一重複列出。

## 名稱慣例

| 程式命名模式 | 中文意義 | 實際用途 |
|---|---|---|
| `*Screen` | 畫面 | 一個完整可切換的 TUI 畫面。 |
| `*Modal` | 彈出視窗 | 暫時要求使用者輸入、選擇或確認的視窗。 |
| `action_*` | 使用者操作 | 由快捷鍵、按鈕或選單觸發的公開動作。 |
| `_render_*` / `_build_*` | 呈現／建立畫面 | 將現有資料格式化、繪製為表格或卡片；不應做長時間網路請求。 |
| `_fetch_*` / `run_*` | 抓取／執行工作 | 讀取外部資料或執行完整背景工作。 |
| `_handle_*` | 結果處理 | 接收使用者選擇或背景工作的結果，決定下一步流程。 |
| `_apply_*` | 套用變更 | 將已驗證的修改寫入持倉或設定資料。 |
| `_` 前綴 | 內部方法 | 僅供同一 module 或 class 內部使用，不是對外 API。 |

## 1. 應用程式啟動與流程控制

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `main` | 命令列主入口 | 解析 `--user/-u`，並啟動 TUI。 |
| `run_tui_dashboard` | 啟動 TUI 儀表板 | 建立並執行 `AssetTrackApp`。 |
| `AssetTrackApp` | AssetTrack 主應用程式 | 管理登入、初始導覽、Dashboard 進入／離開與跨畫面背景維護。 |
| `_handle_login_complete` | 處理登入完成 | 登入後依持倉是否存在，導向 Dashboard 或新手導覽。 |
| `_handle_onboarding_choice` | 處理新手導覽選擇 | 建立範例持倉、手動新增第一筆持倉，或直接進入空白看板。 |
| `_start_dashboard` | 啟動主儀表板 | 讀取匯率、保存目前使用者與持倉狀態，推入 Dashboard。 |
| `_background_data_refresh` | 全域背景資料更新 | 定期補抓當日 ETF、期權與板塊快取，使不開啟個別頁面也可累積資料。 |
| `_handle_dashboard_exit` | 處理儀表板離開 | 依登出或結束程式的結果，回登入畫面或關閉 App。 |

## 2. 登入與帳號

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `LoginScreen` | 登入畫面 | 顯示帳號登入入口，支援 Touch ID、密碼登入與註冊。 |
| `run_touchid_auth` | Touch ID 背景驗證 | 在背景執行 macOS Touch ID 驗證，避免阻塞介面。 |
| `_login_success` | 登入成功處理 | 載入該使用者持倉並完成登入流程。 |
| `PasswordModal` | 密碼驗證視窗 | 輸入並驗證既有帳號密碼。 |
| `RegisterModal` | 註冊帳號視窗 | 建立帳號及儲存密碼資料。 |
| `OnboardingModal` | 新手導覽視窗 | 讓新使用者選擇範例資料、手動新增或空白開始。 |
| `LogoutConfirmModal` | 登出確認視窗 | 避免誤觸造成登入狀態結束。 |

## 3. 主儀表板與投資組合總覽

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `DashboardScreen` | 投資組合主儀表板 | 顯示持倉、總資產、損益、券商分布、近期事件及策略分析卡片。 |
| `_get_cached_usdtwd_rate` | 取得快取美元／台幣匯率 | 以一小時快取減少重複查詢匯率。 |
| `_calc_weights` | 計算持倉權重 | 依美元等值計算每個持倉在投資組合的比例。 |
| `_build_metrics_panel` | 建立投資組合總覽卡 | 呈現資產總值、未實現損益與 Portfolio Beta 三格（bug#00098 已移除持倉數／券商數）。 |
| `_build_holdings_table` | 建立持倉表格 | 依券商整理標的、數量、成本、報價、市值、損益等資料；支援表格內直接操作（`space` 多選、`e` 編輯、`x` 刪除）。 |
| `_build_broker_panel` | 建立券商分布卡 | 呈現各券商／帳戶的資產分布。 |
| `_build_recent_events_panel` | 建立近期事件摘要卡 | Holdings 右側常駐，顯示未來事件精簡清單（另見第 5 節）。 |
| `_build_cross_model_panel` | 建立跨模型總結建議卡 | 主看板上方一張，把 ETF／期權／類股三方向以回測可信度加權、附把握度（見 `cross_model.py`）。 |
| `_marked` | 表格多選標記集合 | Holdings 表格 `space` 多選、`e` 編輯、`x` 刪除（批次）之狀態，經 `on_key` 觸發。 |
| `_render_all` | 重繪整個儀表板 | 依目前記憶體資料更新 Dashboard 所有表格與卡片。 |
| `_tick_header` | 更新頁首時鐘與常駐狀態列 | 每秒更新時間、刷新狀態並重繪底部 `#status-bar`（顯示背景抓取進度／閒置）。 |
| `_do_refresh_worker` | 背景更新投資組合 | 讀取持倉、匯率、無風險利率與即時報價，完成後重繪看板。 |
| `action_refresh_now` | 立即更新 | 使用者主動觸發報價與看板更新。 |
| `action_save_snapshot` / `run_save_snapshot` | 儲存投資組合快照 | 在背景將目前投資組合保存為每日歷史快照。 |
| `action_calibration` | 開啟投資建議校準（`k`） | 開啟 `CalibrationModal`，檢視／確認雙週校準提案。 |
| `action_logout` | 安全登出 | 開啟確認視窗並結束目前登入。 |

## 4. 持倉管理

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `AddPositionModal` | 新增／編輯持倉視窗 | 輸入或修改股票、ETF、期權等持倉欄位；支援「儲存並繼續」批次連續輸入、Symbol 自動推斷市場／幣別。 |
| `PositionActionsModal` | 持倉操作選單 | 提供備註、類別、幣別、券商帳戶與刪除等操作。 |
| `FieldEditModal` | 單一欄位編輯視窗 | 編輯一個文字或選項欄位。 |
| `DeleteConfirmModal` | 刪除持倉確認視窗 | 要求使用者確認刪除動作。 |
| `action_add_position` | 新增部位（`1`） | 開啟 `AddPositionModal` 批次新增（bug#00098 後為部位入口，取代舊 `action_adjust_positions`）。 |
| Holdings 表格直接操作 | 就地編輯／刪除／多選 | `Enter` 就地編輯欄位、`e` 編輯整筆、`x` 刪除、`space` 多選標記（`_marked`），由 `on_key` 處理（已取代 `AdjustPositionsModal`／`ChoosePositionModal`）。 |
| `_handle_field_edit` | 處理持倉欄位修改 | 驗證並更新代號、類型、數量、成本或市場。 |
| `_apply_metadata_edit` | 套用持倉附加資料 | 寫入備註、類別、計價幣別與成本幣別。 |
| `_apply_broker_account_edit` | 套用券商／帳戶修改 | 修改券商與帳戶，必要時合併重複持倉。 |
| `_handle_delete_confirm` | 處理持倉刪除確認 | 確認後移除持倉、保存資料並觸發刷新。 |

## 5. 重大事件與經濟日曆

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `UpcomingEventsScreen` | 近期重大事件畫面 | 顯示持倉、SOX 成分股財報及重要總經事件的月曆。 |
| `_fetch_upcoming_events_worker` | 背景抓取近期事件摘要 | 為 Dashboard 抓取未來財報與總經事件，用於首頁摘要卡。 |
| `_build_recent_events_panel` | 建立近期事件摘要卡 | 顯示未來 30 天的精簡事件清單。 |
| `run_calendar_fetch` | 抓取完整事件日曆 | 讀取過去 30 天至未來 90 天的財報與總經事件。 |
| `_format_cpi_conclusion` | 格式化 CPI 結論 | 將已公布 CPI 資料及下次 Fed 會議機率轉成文字結論。 |
| `_format_fed_conclusion` | 格式化 Fed 決策結論 | 將已發生的 Fed 利率決策轉成文字結論。 |
| `_format_earnings_conclusion` | 格式化財報結論 | 將已公布財報的營收、毛利、淨利年增資訊轉成摘要。 |
| `_render_monthly_calendar` | 繪製月曆事件表 | 將事件依月份排成 Rich 月曆表格。 |
| `action_upcoming_events` | 開啟近期重大事件 | 從 Dashboard 進入完整事件日曆。 |

## 6. 主動式 ETF 分析

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `ActiveETFsScreen` | 主動式 ETF 排行畫面 | 顯示美國主動式 ETF 的 AUM、績效、持股與歷史（bug#00091 已移除台股 ETF 排行；台股僅保留持倉追蹤）。 |
| `_fetch_and_cache_etf_symbols` | 抓取並快取 ETF 資料 | 批次取得 ETF 績效、資產規模、持股及持股價格並保存。 |
| `run_background_fetch` | ETF 背景資料載入 | 由 ETF 畫面觸發資料取得與快取更新。 |
| `_render_ranking_tables` | 繪製 ETF 排行表 | 更新美股主動式 ETF 排行（bug#00091 台股分頁已移除）。 |
| `_render_holdings` | 繪製 ETF 持股明細 | 顯示所選 ETF 的成分、權重、估算股數與資產配置。 |
| `_render_history` | 繪製 ETF 歷史資料 | 顯示已累積的 ETF 持股變化紀錄。 |
| `AdvancedAnalysisScreen` | ETF 進階趨勢分析畫面 | 從多檔 ETF 持股快照找出趨勢與共同結論。 |
| `_run_analysis` | 執行 ETF 趨勢分析 | 計算標的持股趨勢、排名與結論。 |
| `_build_etf_conclusions_panel` | 建立 ETF 趨勢結論卡 | 在 Dashboard 顯示精簡 ETF 分析結論。 |
| `action_advanced_analysis` | 開啟 ETF 進階分析 | 從 ETF 排行頁進入趨勢分析畫面。 |

## 7. 期權觀察、風險與校準

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `OptionsWatchlistScreen` | 期權觀察清單畫面 | 分析持倉／自訂標的的期權鏈、IV、預期波動、訊號與 Greeks。 |
| `_underlyings_from_positions` | 從持倉取得期權標的 | 取出股票、ETF 或期權持倉的標的代號。 |
| `_watchlist_underlyings` | 建立期權觀察標的清單 | 合併持倉標的與使用者手動加入的標的，並區分來源。 |
| `_fetch_and_cache_options_underlyings` | 抓取並快取期權資料 | 批次抓取期權鏈、財報日期，保存每日真實快照。 |
| `run_background_fetch` | 期權背景資料載入 | 從觀察清單發起快取更新與資料重繪。 |
| `_refresh_underlying_spots` | 更新標的現貨價格 | 批次更新觀察標的的現價。 |
| `_run_analysis` | 執行期權分析 | 計算異常震盪、IV 背離、OI 偏向、預期波動與部位 Greeks。 |
| `_render_greeks` | 繪製期權風險指標 | 顯示 Delta、Theta、Vega 等持倉風險。 |
| `_build_options_flow_panel` | 建立期權分析結論卡 | 在 Dashboard 顯示精簡的期權策略／風險結論。 |
| `AddTickerModal` / `RemoveTickerModal` | 新增／移除觀察標的視窗 | 管理不屬於持倉的手動期權觀察標的。 |
| `action_add_ticker` / `action_remove_ticker` | 新增／移除觀察標的 | 開啟相對應的標的管理流程。 |
| `CalibrationScreen` | 訊號回測校準畫面 | 用實際累積快照，評估方向性訊號的命中率、樣本數與統計顯著性。 |
| `CalibrationModal` | 投資建議校準確認視窗 | Dashboard `k` 開啟；顯示雙週校準提案並提供【套用】【略過】【立即重算】【切換週期】（校準涵蓋 ETF／類股門檻，`calibration_schedule.py`，需確認才套用）。 |
| `run_backtest` | 執行訊號回測 | 在背景執行 walk-forward 回測。 |
| `OptionsHelpScreen` | 期權指標說明畫面 | 說明 IV、預期波動、Greeks、OI 與訊號的意義和限制。 |
| `action_calibration` / `action_help` | 開啟校準／說明 | 從期權觀察頁進入校準或使用說明。 |

## 8. 類股板塊分析

| 英文名稱 | 中文對應 | 實際功能 |
|---|---|---|
| `SectorAnalysisScreen` | 類股板塊分析畫面 | 管理板塊群組，顯示成員表現、漲跌廣度與市場共識。 |
| `SectorGroupModal` | 板塊群組編輯視窗 | 新增或編輯板塊名稱與成員股票代號。 |
| `_fetch_and_cache_sector_groups` | 抓取並快取板塊資料 | 批次取得所有板塊成員的市場資料與每日快照。 |
| `_recompute_flows` | 重新計算板塊共識 | 根據歷史快照重新判斷板塊是否有普遍買進／賣出訊號。 |
| `_render_groups` | 繪製板塊列表 | 顯示已建立板塊與其共識概況。 |
| `_render_conclusions` | 繪製板塊結論 | 呈現可解讀的板塊共識摘要。 |
| `_render_members` | 繪製板塊成員明細 | 顯示選定板塊內各標的的價格與漲跌資訊。 |
| `_build_sector_consensus_panel` | 建立板塊共識卡 | 在 Dashboard 顯示精簡板塊分析結論。 |
| `action_refresh_now` | 立即更新板塊資料 | 強制重新抓取並計算板塊資訊。 |
| `action_add_group` / `action_edit_group` / `action_delete_group` | 新增／編輯／刪除板塊 | 管理使用者自訂板塊群組。 |

## Dashboard 導航對照

| Dashboard 動作 | 使用者看見的中文功能 | 目的地 |
|---|---|---|
| `action_add_position` | 新增部位（`1`） | `AddPositionModal`（批次新增；Holdings 表格另支援 `e`/`x`/`space` 直接操作）。 |
| `action_refresh_now` | 立即重整（`2`／`r`） | Dashboard 報價與資料刷新。 |
| `action_logout` | 安全登出（`3`／`q`） | 登出確認視窗。 |
| `action_upcoming_events` | 近期重大事件（`4`） | `UpcomingEventsScreen`。 |
| `action_save_snapshot` | 儲存快照（`5`） | 背景保存投資組合快照。 |
| `action_active_etfs` | 主動式 ETF 排行（`6`） | `ActiveETFsScreen`。 |
| `action_options_watchlist` | 期權觀察清單（`7`） | `OptionsWatchlistScreen`。 |
| `action_sector_analysis` | 類股板塊分析（`8`） | `SectorAnalysisScreen`。 |
| `action_calibration` | 投資建議校準（`k`） | `CalibrationModal`。 |
