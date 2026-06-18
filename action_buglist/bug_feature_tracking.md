---
tags: [AssetTrack, CLI]
GitHub version: v0.0.1
Local version: v0.0.2
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

5. [open] [bug#00005] [newfeature] **互動式登入與自動循環刷新之即時報價 CLI 介面 (市場開市狀態顯示)**
   * **問題描述**：CLI 啟動時應要求使用者輸入帳號登入，認證後調閱持倉並進入每分鐘自動更新報價的循環，不可直接 return，並動態判斷顯示各部位之「開市/未開市」狀態。
   * **root cause**：舊有設計為一次性執行輸出靜態歡迎面板，無法持續即時觀看及自動判斷各地交易所之開關市狀態。
   * **solution**：在 main 入口處加入互動式登入引導，認證後進入無限 while 循環配合 `console.clear()` 每分鐘重新下載最新行情，並以 `zoneinfo` 在台北與紐約時區判斷美股與台股交易所的開關市狀態。
   * **fixed by**：v0.0.1-dev

6. [open] [bug#00014] [newfeature] **進階選擇權追蹤與 Black-Scholes 希臘字母監控 (Advanced Option Metrics & Greeks)**
   * **問題描述**：對於選擇權持倉，除了基礎參數外，缺乏希臘字母（Delta, Gamma, Theta, Vega）的估算，難以監控時間值衰減 (Theta Decay) 或價內外狀態 (ITM/OTM)。
   * **root cause**：
   * **solution**：
   * **fixed by**：

7. [open] [bug#00015] [newfeature] **多資產整合擴充（現金與加密貨幣錢包支援） (Multi-Asset Class Support: Cash & Crypto)**
   * **問題描述**：系統定位為統合性資產整合追蹤，但目前僅限於證券與期權。應擴充支援非證券的固定資產/現金科目（如銀行存款、數位穩定幣）手動登錄，以及加密貨幣公鏈餘額與交易所 API 自動同步。
   * **root cause**：
   * **solution**：
   * **fixed by**：

8. [open] [bug#00017] [newfeature] **互動式全螢幕終端面板與本機/Webhook 警報系統 (Rich TUI Dashboard & Price Alerting)**
   * **問題描述**：目前的 CLI dashboard 為定時 `console.clear()` 刷新，易產生閃爍且無選單分頁。需要為 CLI 引入選單、清單與欄位修改時的鍵盤/游標（上下左右方向鍵）選擇體驗。目前考量以下三種技術解決方案（尚未決定）：
     * **方案一：使用 `questionary` 庫**（推薦，輕量且侵入性最小）：基於 `prompt_toolkit` 封裝，可直接將現有的 `Prompt.ask` 替換為支援方向鍵與 Enter 選擇的互動式清單，極易與現有的 `Rich` 終端輸出整合。
     * **方案二：使用 `prompt_toolkit`**（控管度最高）：可實現高度自訂的鍵盤事件監聽、自動補全與熱鍵綁定，但程式碼複雜度較高。
     * **方案三：使用 `Textual` 庫重構為全螢幕 TUI**（視覺效果與互動最豐富）：Rich 官方推出的全螢幕終端機 UI 框架，支援滑鼠、鍵盤焦點、多視窗分頁等，但需要將整體 CLI 重構為事件驅動架構。
   * **root cause**：
   * **solution**：
   * **fixed by**：

---

## ✅ 已關閉與驗證項目 (Closed Items)

1. [closed] [bug#00006] [newfeature] **新使用者無持倉之引導精靈與功能選單**
   * **問題描述**：新使用者或持倉空白帳戶登入時，系統缺乏明確的下一步引導（如初始化、手動新增、CSV 導入或範例部位）。應實作互動選單引導。
   * **root cause**：1. 新使用者登入時因無持倉，介面無清晰指引。2. 新增選項選單中使用 `ctx.invoke` 呼叫其他指令時，未提供 `ctx` 實體導致拋出 TypeError。
   * **solution**：1. 實作引導選單與功能選項。2. 將 `ctx.invoke` 替換為直接調用 Python 函數（如 `init_setup(ctx)` 及 `add(ctx, broker="manual")`）避開 parameter check 錯誤。
   * **fixed by**：v0.0.1-dev

2. [closed] [bug#00007] [newfeature] **Keychain 安全憑證儲存與 Touch ID 生物辨識雙因子登入機制**
   * **問題描述**：系統應安全儲存使用者密碼，並於 macOS 環境中支援 Touch ID 生物辨識驗證，指紋驗證失敗或不支援時，無縫降級回鑰匙圈密碼驗證。
   * **root cause**：原系統無使用者身份認證，亦無憑證保存與生物特徵辨識機制。
   * **solution**：以 `keyring` 將使用者密碼儲存於系統 Keychain 中；並使用 Swift 編譯獨立的 macOS Touch ID 驗證輔助程式，於登入時自動執行指紋檢測，失敗或取消時降級回 3 次密碼輸入限制。
   * **fixed by**：v0.0.1-dev

3. [closed] [bug#00008] [newfeature] **即時監控看板非阻塞式互動選單與子指令整合**
   * **問題描述**：即時更新看板會阻塞使用者輸入，使用者無法直接在畫面中執行 any 操作。應實作非阻塞式輸入，允許使用者在看板中直接新增倉位、縮減倉位/登錄交易，且不中斷自動定時刷新報價的行為。
   * **root cause**：1. 看板以 `time.sleep` 阻塞等待，無法接收鍵盤輸入。2. 在循環中直接呼叫 Typer 子指令時，預設 `OptionInfo` 會引起屬性錯誤；且 `ctx.obj` 未動態綁定為登入的使用者，導致寫入預設資料庫/配置檔案。
   * **solution**：1. 使用 Unix/macOS `select.select` 實作非阻塞式 Stdin 輸入輪詢（超時 60 秒），若無輸入則自動更新報價。2. 整合子指令面板，以 `try...except (typer.Exit, Exception)` 隔離子指令退出，並動態綁定 `ctx.obj = user` 與明確處理預設參數以避開 `OptionInfo` 錯誤。
   * **fixed by**：v0.0.1-dev

4. [closed] [bug#00009] [function] **CLI 入口模組原始碼損毀及修復**
   * **問題描述**：`cli.py` 原始碼於系統執行過程中損毀並遭清空（0 位元組），導致 `ImportError: cannot import name 'app' from 'assettrack.cli'` 無法執行 CLI。
   * **root cause**：先前系統檔案寫入異常或中斷，造成核心 `cli.py` 檔案內容遺失。
   * **solution**：從歷史對話 logs 中提取並完整重建 `cli.py` 內容，包含 Keychain/Touch ID 登入流程、非阻塞式輪詢選單與各 CLI 指令，並進行編譯與測試驗證通過。
   * **fixed by**：v0.0.1-dev

5. [closed] [bug#00010] [function] **選擇權持倉參數、自訂券商帳戶與持倉修改功能之優化**
   * **問題描述**：手動新增選擇權持倉功能未完善，需正確輸入到期日與行權價等明細以防 Yahoo Finance 查詢 404 及錯誤警告；此外缺乏修改現有持倉、選取/輸入特定券商子帳戶 (如 FT, IBKR) 等功能。
   * **root cause**：1. 選擇權部分欄位可能在合併、導入或交易登錄時遺失或未正確寫入；當 yfinance 查詢無報價的選擇權合約時，會在終端機輸出 HTTP 404 等垃圾資訊。2. CLI 僅允許新增與登錄交易，無修改介面。3. CLI 新增與修改時未提供設置/寫入 `account` 欄位的引導，且唯一性判定未考量 account 造成同代碼不同券商之覆蓋。
   * **solution**：1. 於 `Position` 加入 Pydantic validator，若符合 OCC 標準格式則自動解析並補全期權明細；同時在 `quotes.py` 實作 stderr/stdout 重導向與 override yfinance logger，阻斷查詢報價 404 等雜訊。2. 於 CLI menu 提供 Option `2`-修改持倉功能，允許修改數量、成本、貨幣、帳戶及期權資訊，且編輯後支援重疊持倉自動合併。3. 於 CLI 及 Streamlit 手動增刪查改中導入券商 `account` (如 FT, IBKR) 選取/填寫，並改以 `(broker, account, symbol)` 為唯一鍵進行重複判定與合併。
   * **fixed by**：v0.0.1-dev

6. [closed] [bug#00011] [newfeature] **工具定位重構：移除下單/交易邏輯，補全持倉參數欄位**
   * **問題描述**：1. 功能選單中「登錄交易(減持/平倉)」是下單軟體邏輯，與資產管理工具定位不符。2. 新增/修改持倉缺少 market、exchange、cost_currency、multiplier、sector、notes 等完整欄位，stock 與 option 相關欄位無法完整維護。
   * **root cause**：1. 早期設計未明確區分「資產管理工具」與「交易執行軟體」，導致 log_trade 功能混入選單。2. Position model 欄位設計偏向最小可行，未考量多市場（US/TW/HK）、多幣別成本、合約乘數、分類標籤等實際需求。
   * **solution**：1. 移除 CLI 選單 option 3 的 `log_trade` 呼叫，改為 `remove_position`（直接刪除持倉），並新增獨立的 `assettrack remove` 指令。2. `Position` model 新增 6 個 Optional 欄位：`market`、`exchange`、`cost_currency`、`multiplier`、`sector`、`notes`。3. `_interactive_add_one` 完整重寫，涵蓋所有欄位引導（含台股市場自動後綴、非美股選擇權手動代碼輸入）。4. `edit` 重構為全欄位逐一確認模式（Enter 保留原值）。5. ~~`dashboard.py` 相關修改~~（已隨 Streamlit UI 完全移除）。
   * **fixed by**：v0.0.1-dev

7. [closed] [bug#00012] [function] **移除 Streamlit Web UI（dashboard.py），確保工具定位為純 CLI**
   * **問題描述**：AssetTrack 定位為純 CLI 工具，但 `dashboard.py` 為 Streamlit Web UI（含 HTML5/CSS Glassmorphism、Plotly 圖表、瀏覽器 file uploader），與 CLI-only 架構矛盾；`pyproject.toml` 的 `[ui]` optional deps（streamlit, plotly）亦需移除。
   * **root cause**：早期設計同時維護 Streamlit Web UI 與 CLI，後明確決策以純 CLI 為主，但 dashboard.py 及其依賴未同步清除。
   * **solution**：1. 刪除 `dashboard.py`。2. 從 `pyproject.toml` 移除 `[project.optional-dependencies] ui` 群組（streamlit>=1.35, plotly>=5.20）。3. 確認 `cli.py` 完全以 `typer` + `rich` 實作，無任何 HTML5 引用。
   * **fixed by**：v0.0.1-dev

8. [closed] [bug#00013] [newfeature] **投資組合 Beta 權重與市場敏感度分析 (Portfolio Beta-Weighting & Analytics)**
   * **問題描述**：目前系統缺乏量化投資組合風險的指標。應實作計算個股/選擇權相對於基準大盤（如 SPY 或 0050）的 Beta 值，並顯示於 CLI dashboard 表頭中。同時可支援歷史最大回撤 (Max Drawdown) 與風險價值 (VaR) 計算。
   * **root cause**：`quotes.py` 無 beta 抓取函數；`cli.py` dashboard 表頭指標列無 Portfolio Beta 欄位。
   * **solution**：1. 在 `quotes.py` 新增 `fetch_beta()`，以 `yfinance ticker.info["beta"]` 取得個股 beta（選擇權自動以 underlying 查詢）。2. 在 `cli.py` `render_dashboard_once` 中以每個持倉的 USD 市值為權重，計算 Weighted Portfolio Beta。3. 在指標欄新增第五格「⚡ Portfolio Beta」Panel，顏色依風險等級變色（≤0.8 綠色、≤1.2 黃色、>1.2 紅色）。
   * **fixed by**：v0.0.1-dev

9. [closed] [bug#00016] [newfeature] **大盤指數基準對比與時間/資金加權報酬率計算 (Portfolio Benchmarking & TWR/IRR Performance)**
   * **問題描述**：歷史淨值折線圖僅顯示自身絕對淨值，缺乏與大盤指數（如 SPY, QQQ, ^GSPC）的相對績效對比；且原設計依賴 SQLite 快照才能運作，使用者需先手動存檔才有圖可看。
   * **root cause**：1. `history` 指令原以快照（Snapshot）資料為數據源，要求使用者必須累積快照才能使用。2. 缺乏嚴苛的前置條件驗證（options 無歷史市價、空持倉、無網路等邊界情境均未處理）。3. 圖表為單純折線圖，無法直觀呈現部位結構與券商比例。
   * **solution**：v2 完整重設計。1. 改為「當下持倉部位 × 歷史股價」回推（Position-based Backtest），不再依賴快照，任何時候都能使用。2. 新增 `fetch_historical_prices_weekly()` 批次下載週頻價格（yf.download 批次拉取）。3. 加入嚴格前置條件：排除 Options（yfinance 無法取得歷史定價）、排除非 USD 持倉、驗證網路下載成功、至少 2 個有效週節點。4. 新增互動選單：期間固定為 60d/180d/YTD，基準選 SPY/QQQ/^GSPC/停用。5. 新增 `draw_history_chart()`：直方圖（`█▓▒░` 按券商分層）+ 折線（`○─` benchmark），X 軸每週切分、Y 軸 USD 市值。6. 新增 `get_upcoming_macro_events()`：hardcoded 2025-2026 FED/NFP/CPI 日程，顯示未來 90 天內事件清單。7. 績效摘要顯示組合回報 / Benchmark 回報 / Alpha / 期間高低點。
   * **fixed by**：v0.0.1-dev

10. [closed] [bug#00018] [UI] **歡迎畫面 Logo 拼寫錯誤與實體圖片 ASCII 轉換**
    * **問題描述**：CLI 啟動時時の ASCII Welcome Page 寫錯字為 `AssetTrak`（漏掉 `c`），且缺乏品牌感。應使用 Pillow 直接將官方圖片 Logo 轉換為精緻的 ASCII 鷹頭標誌，並修復文字拼寫為 `AssetTrack`。
    * **root cause**：1. 舊有 ASCII Art 手工拼寫錯誤。2. 未能整合圖片設計。
    * **solution**：1. 實作獨立的 Pillow 預處理與自適應像素轉換，將 `assettrack_logo.png` 的鷹頭 Logo 部位以 row gap 完美裁切，在 threshold=235 條件下渲染成無雜點的 ASCII 圖示。2. 修復下方 `AssetTrack` 拼字並以 Slant ASCII 樣式展示。
    * **fixed by**：v0.0.1-dev

11. [closed] [bug#00019] [function] **歷史週頻市值 NaN 傳播與單一基準指數解析失效**
    * **問題描述**：1. 當持倉包含近期上市或歷史不全的標的（如 SPCX）時，yfinance 回傳的 NaN 會傳播並破壞整個週期的總值計算，導致回溯大半週數被過濾只剩最近幾天。2. 基準指數（如 QQQ）等單一標的下載時，yfinance 返回 columns 為 MultiIndex 的 DataFrame，造成 `row["Close"]` 被誤解析為 pandas Series，觸發 TypeError 導致 QQQ 歷史資料為空，進而使大盤對比強制降級為「停用」。
    * **root cause**：1. 未對 `float("nan")` 進行過濾與防護。2. 單一 ticker 下載時 columns 同樣是 MultiIndex 形式，原程式碼未做對應判斷。
    * **solution**：1. 在 `fetch_historical_prices_weekly` 下載與 `history()` 計算中，引入 `math.isnan()` 對所有價格進行嚴格的實數與空值過濾，使無歷史價格的標的在該週市值中不作加總傳播。2. 統一單 ticker 與多 ticker 的 DataFrame 解析邏輯，對 `pd.MultiIndex` 的層級（Level 0 & Level 1）進行自適應的 close price 行提取，保證 QQQ/SPY 均能成功解析。
    * **fixed by**：v0.0.1-dev

12. [closed] [bug#00020] [newfeature] **純手動持倉管理與 API/CSV 匯入功能移除**
    * **問題描述**：目前系統中的 IBKR API 連線設定與 Firstrade CSV 匯入在使用時存在不便，需簡化工具定位，移除此二類 API 及 CSV 的相關連線設定與匯入指令，改為純手動部位管理並配合 Keychain/Touch ID 登入認證。
    * **root cause**：依賴外部連線與檔案結構容易因變動或環境問題造成異常，不利於快速輕量化資產記錄與隱私安全性。
    * **solution**：1. 自 CLI 移除 `import-csv`、`set-credential` 及 `clear-credential` 指令，並移除程式中對 `.brokers` CSV 解析器的 import。2. 更新儀表板 header 資訊，隱去 Keychain 串接狀態欄。3. 更新選單，將原本的「連線設定」選項移除，並重編號餘下功能為 1~7。4. 引導精靈在初始化時不再詢問 API/CSV，改為直接引導至手動新增持倉。
    * **fixed by**：v0.0.1-dev

13. [closed] [bug#00021] [newfeature] **功能選單重整與新增取消返回功能 (Action Menu Reorganization & Cancel Options)**
    * **問題描述**：原本的 7 個主選單選項需簡化，並將「新增持倉」與「移除持倉」整合至新選單「1-部位調整」的子選單中。同時，為了防止使用者選錯選項，每個互動式選項均需要有取消或返回主選單的機制。
    * **root cause**：選單項目過多使得介面擁擠，且缺乏在每個輸入詢問時退回主選單的取消機制。
    * **solution**：1. 將主選單簡化為 5 個選項，其中選項 1 改為「部位調整」；2. 點選「部位調整」後顯示「新增部位、修改部位、移除部位、返回主選單」的子選單；3. 在 `_prompt_broker_account` 與 `history` 選擇中新增 `q` 退出機制，利用 `typer.Exit` 拋出以退回主選單；4. 在安全登出與儲存快照前加入確認提示。
    * **fixed by**：v0.0.1-dev

14. [closed] [bug#00022] [newfeature] **Holdings 以券商分區塊顯示並新增今日漲跌欄位 (Broker-Grouped Holdings & Daily P&L Columns)**
    * **問題描述**：原本 Holdings 表格為全體打平排列，不易區分不同券商；且缺少每日持倉淨值變化（今日漲跌金額與百分比）的快速參考欄位。
    * **root cause**：1. `_build_positions_table` 為單一扁平表格，未按 broker 分組。2. `Position` model 缺少 `prev_close` 欄位，`quotes.py` 未拉取前日收盤價。
    * **solution**：1. `models.py` 新增 `prev_close: Optional[float]` 欄位，並新增 `daily_change`（部位今日淨值變動）與 `daily_change_pct`（個股漲跌幅）兩個 property。2. `quotes.py` 在 `enrich_positions_with_quotes` 中改直接操作 yfinance Ticker，從 `fast_info.previous_close` 取得前日收盤，快速失敗時退回 5d history 取倒數第二收盤作為 fallback。3. `cli.py` 新增 `_build_broker_holdings()` 函數：按 broker 分組、每組依 USD 等值市值由大至小排序、印出 Rule 分隔 Header（顯示券商名稱與小計）、表格新增「今日%」與「今日漲跌」欄位（正負以綠紅色標示），並取代原 dashboard 中對 `_build_positions_table` 的呼叫。
    * **fixed by**：v0.0.1-dev
