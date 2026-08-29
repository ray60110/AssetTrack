# AssetTrack — 跨券商投資組合追蹤與投資建議終端機 (全螢幕 TUI)

`AssetTrack` 是一款輕量、離線優先的終端機投資組合追蹤器，主要針對美國股票與選擇權（Firstrade、Interactive Brokers 等多個券商管道）的投資人設計，可建立個人資料庫，密碼、指紋認證機制。提供除了即時報價、多幣別計價與 Bloomberg 風格的終端機視覺化之外，以**四大分析功能各自產生資訊或投資建議、三項方向建議以歷史真實快照做 walk-forward 回測驗證，回測本身再做統計顯著性檢定**為核心，四大功能包括：
    1. 重大事件歷程 `UpcomingEventsScreen`
    2. 主動式ETF排行、其操作分析 `ActiveETFsScreen`
    3. 期權觀察與分析 `OptionsWatchlistScreen`
    4. 類股板塊分析 `SectorAnalysisScreen`

門檻參數優先讀取 QuantTrade 匯出的 `{user}_champion_params.json`，沒有契約檔時回退 legacy 校準狀態。策略實驗室（Forecast Ledger、Promotion、鍵 `0`／`k`）已於 2026-08-06 搬出本套件。
投資建議層一律以**美股為主**；台股／TWD 僅保留於**持倉追蹤、報價與匯率換算**，不進入任何投資建議。完整判斷邏輯見根目錄 [`INVESTMENT_LOGIC.md`](./INVESTMENT_LOGIC.md)。

---

## 運行架構流程圖 (Runtime Architecture Flow)

```mermaid
graph TD
    User([使用者 User]) -->|Touch ID / 密碼驗證| Auth[身分驗證與安全]
    Auth -->|通過| TUI[Textual TUI 主程式 assettrack]

    subgraph 全螢幕看板與畫面 (Textual TUI Screens)
        TUI --> Login[LoginScreen / Register / Onboarding]
        TUI --> Dash[DashboardScreen 主看板]
        Dash --> Cal[UpcomingEventsScreen 近期重大事件 4]
        Dash --> ETF[ActiveETFsScreen 主動式ETF 6 → 進階分析]
        Dash --> OPT[OptionsWatchlistScreen 期權觀察清單 7]
        Dash --> SEC[SectorAnalysisScreen 類股板塊分析 8]
        Dash --> PERF[PerformanceTrackingScreen 績效比較 9]
        Dash -->|表格內格編輯| FieldEdit[FieldEditModal]
        Dash -->|部位操作選單| ActModal[PositionActionsModal]
    end

    subgraph 分析與回測引擎 (離線、零網路)
        Dash --> A1[analysis 主動式ETF跨基金共識 + 回測]
        Dash --> A2[options_analysis 已觀察樣態 + 預期波動／Greeks]
        Dash --> A3[sector_analysis 類股廣度共識 + 回測]
        A1 & A2 & A3 --> BT[backtest_stats 統計驗證: Wilson/二項/ESS]
    end

    subgraph 資料存取與報價 (Data & Quotes)
        TUI -->|讀取/寫入持倉| JSONStore[(user_positions.json)]
        TUI -->|市值快照| DB[(user_assettrack.db · SQLite)]
        TUI -->|逐日真實快照累積| CACHE[(etf_cache / options_cache / sector_cache · history/*.jsonl)]
        TUI -->|校準狀態| CJSON[(user_calibration.json)]
        TUI -->|併發抓取即時報價/財報/總經| YF[yfinance / FRED]
        YF -->|USDTWD=X 匯率| Convert[基準貨幣與匯率快取]
    end
```

---

## 主要功能特色

### 1. 安全認證與多使用者隔離
* 密碼以 PBKDF2 雜湊存入 macOS Keychain，至少 8 個字元；舊的明文密碼在下次成功登入時自動升級。
* Touch ID 只解鎖「已經用密碼在這台裝置登入過」的帳號，提示會帶帳號名稱。
* 登入後持倉 JSON、績效帳本、報價 overlay、事件歷史與 SQLite 快照會加密寫入；登出即關閉保險庫。
* 每位使用者有獨立資料檔與 Keychain 項目。SEC 識別仍存在獨立 Keychain service，畫面只顯示遮罩。
* 首次登入以互動式引導小精靈（`OnboardingModal`）帶你建立第一筆持倉。

### 2. 全螢幕 Dashboard 主看板
啟動即進入全螢幕終端機看板（皆離線讀取本機資料即時運算）：
* **總資產塊**：NAV、股票／ETF／期權／現金市值與％、筆數、USD／TWD 比、今日、未實現、Beta vs SPY。
* **曝險塊**：總／淨倍數與金額、股票／普通 ETF、倍數 ETF、期權 Δ、券商％與金額。
* **持倉表**：`Enter` 就地編輯、`e` 編輯整筆、`x` 刪除、`space` 多選；右側窄欄列未來事件。
* **觀察條**（左到右對 6／7／8）：類股 · 10 日、期權樣態、ETF 觀察。公式與免責按對應鍵。
* **狀態列**：背景抓取時寫「抓取：…」，閒置時寫「閒置」。之後每 30 分鐘依快取新鮮度冪等補抓，主頁每 60 秒重繪。

### 2.1 使用者績效追蹤與大盤比較

* 新帳號註冊時可選擇是否啟用績效追蹤；既有帳號也能從績效頁中途啟用。績效頁按 `d` 可確認取消追蹤並解除持股／現金管理限制，既有紀錄仍會保留；重新啟用後會如實標示「追蹤斷層」，不回填不存在的歷史。
* 啟用後會以**完整資產總值**（所有股票、ETF、選擇權與現金）建立 Tracking Baseline，預設同時建立 QQQ 與 VT 的 Shadow Benchmark Portfolio。
* Dashboard 按 `9` 開啟比較頁，以表格直接呈現使用者完整資產、現金流調整後報酬、QQQ／VT 同步資金流後的等值、美元差額及領先／落後百分比。
* 首筆 baseline 會在取得完整報價後立即保存；之後預設於每週日保存一次估值，benchmark 採前一個可取得的美股交易日收盤價。
* Dashboard 按 `i`／`o` 宣告入金／出金。每筆 JSON 帳本保留來源或用途、管道、券商帳戶、原幣金額、匯率、USD 等值、備註與當日 benchmark 價格。
* 追蹤期間，證券買進必須由同券商／帳戶／幣別的現金支付；現金不足會拒絕。刪除證券部位會視為賣出並把現值轉回現金；現金不得直接新增、改寫或刪除，必須使用出入金流程。
* 每位使用者的績效資料獨立保存於 `data/<user>_total_asset_tracking.json`。完整資料契約與公式見 [`docs/performance_tracking.md`](./docs/performance_tracking.md)。

### 3. 四大分析功能 ＋ 回測 ＋ 統計驗證
所有分析 **100% 離線、零網路**，只讀取系統在背景刷新時逐日真實累積的本機快照（`etf_cache/options_cache/sector_cache` 的 `history/*.jsonl`），不回填、不臆測；資料不足時誠實顯示「資料收集中／累積中」而非假結論。本機研究快照與 outcome truth 保留 2 年。

* **近期重大事件與經濟日曆（快捷鍵 `4`）**：歷史事件只保留**當月與上個月**，並顯示未來 90 天的個股／SOX 財報與 FED/NFP/CPI 日程；更早的已完成事件會從表格與事件歷史資料排除，避免持續累積。事件以固定「日期／狀態／內容」欄位的縱向卡片排列；已發生卡片以淺灰底和 `✓` 表示完成，待發生卡片以 `○` 與事件類型色框呈現。已發生財報會更新 Revenue／CAPEX／EBIT／FCF 當期值、去年同期與 YoY；已發生總經事件會列出本期、前期與變動。預設以 `Asia/Taipei` 顯示日期時間，畫面內按 `T` 可輸入任意 IANA 時區並保存為使用者偏好。
* **類股板塊共識（快捷鍵 `6`）**：以廣度擴散指數＋市值加權報酬雙訊號同向、再加持續性過濾，抓「整個族群普遍同向」而非單一名股。
* **期權觀察（快捷鍵 `7`）**：畫面報已觀察到的現價／IV 樣態、預期波動 ±1σ、ATM 權利金相對近20日已實現波動的貴賤（Call/Put／跨式溢價為市價減 RV 定價），以及你持有選擇權的淨 Greeks。在「波動貴賤」欄用 ↑↓ 選擇標的、Enter 看近 90 天每日 ATM IV − RV（每天 RV＝當日往前 20 個交易日；更早快照刪除）；財報剩餘不到 10 天時註記剩餘天數。**不輸出股價漲跌預測。** 跨式溢價是指示性模型差，不是已驗證的可交易超額報酬。
* **主動式 ETF 跨基金共識（快捷鍵 `8`）**：ETF universe 每日由市場 screener 動態建立，只納入 **AUM > US$5B** 且基金說明明確標示主動管理者，再依真實持股資產配置分類、分類內按 AUM 排序。於 14 天視窗比較逐日真實快照，部位級列出股票買入／賣出 ETF 數、總金額與整體多空；方向需**真實股數變化與權重變化兩訊號同向**才算。畫面另追蹤 Bridgewater、Citadel、Millennium、Elliott 的 SEC 13F 季度持股與相鄰申報期差分；13F 並非即時交易資料，Put/Call 亦不揭露履約價與到期日，因此不會被偽裝成精確期權時間區間訊號。
* **回測層**：TUI 上仍有方向輸出的 ETF 與類股，共用 point-in-time **walk-forward** 骨架（只用 ≤T 的真實快照重推當日結論，再看 T 之後的真實走勢）。期權方向回測仍留在程式庫，畫面不再顯示；若日後導入新方向政策，須先過 `direction_forecast_validation.validate`。期權結算使用 NYSE 假日日曆，只接受剛好 T+h 的真實 outcome（缺日不以更晚價格代替），並 purge 同標的重疊 outcome 區間；raw n 只供稽核，不能冒充獨立樣本。
* **期權 proper-score 驗證層**：每次機率只使用該預測當時已成熟的 outcome，以 expanding empirical-Bayes 對當時基準率做收縮；同時呈現 Brier score／Brier skill、log loss、命中 edge、Wilson CI、Bonferroni 與前後穩定性。`(1-p value)` 不再顯示成漲跌「信心水準」。
* **門檻來源**：畫面結論優先使用 QuantTrade 匯出的 Champion 參數；契約不存在時回退 `calibration_schedule` 的 legacy 狀態。AssetTrack 不再提供鍵 `k` 調參畫面。

### 4. 重大財報與總經日曆
* 整合持有部位個股財報、SOX 半導體十大成分股財報與三大重大總經數據（FED/NFP/CPI）公佈日程。
* 支援盤前/盤後判斷與美東時間自動換算至本地 GMT+8（依日光節約自動調整）；日曆畫面左側月曆網格反色標示事件、右側列出詳情。

### 5. 基準計價貨幣切換與即時報價
* 支援以 `USD` 或 `TWD` 作為基準計價貨幣，自動透過 `yfinance` 取得 `USDTWD=X` 匯率，將所有持倉金額、成本基礎與未實現損益即時換算。

### 6. 純手動持倉管理
* 專注於純手動部位管理（不做第三方 API 串接與 CSV 導入）。`action_add_position` 的新增表單支援股票、ETF、選擇權與現金；現金可依券商／帳戶新增 USD 或 TWD，同幣別重複新增會疊加。
* 持倉、總資產、未實現、Beta 與券商比例皆納入現金。主頁總資產列出股票／ETF／期權／現金市值與％，不標進攻／防守。曝險同時列出總／淨倍數與金額；一般股票／ETF 以 1x 計算，倍數與反向 ETF 依基金名稱自動辨識（新增／編輯時可手動覆寫），期權以 Delta × 合約數 × 合約乘數 × 標的現價換算等值曝險。任一必要報價不足時不輸出假比例。
* 新增表單支援「儲存並繼續」批次連續輸入、輸入 Symbol 自動推斷市場/幣別（如 `2330` → TW/TWD），非必要欄位收於可展開的進階欄位；選擇權含明細。
* 非阻塞式背景 worker 執行緒異步下載行情與匯率，避免畫面卡頓。

---

## 指令使用說明

```bash
# 啟動互動式全螢幕 TUI（唯一執行模式，需通過 Touch ID 或密碼登入）
assettrack -u username
# 或省略 -u/--user，預設使用 "default" 帳戶
assettrack
```

若要抓取四家機構的 SEC 13F，首次登入時會依 SEC Fair Access 規範引導建立
「SEC 識別名稱＋聯絡信箱」。使用者必須先看到用途與傳送對象並主動同意；也可以取消，
繼續使用 13F 以外的功能。資料會與目前登入的 AssetTrack 帳號綁定，存入作業系統
Keychain 的獨立 service，不寫入 `.env`、投資快取、SQLite 或紀錄檔。每個 SEC HTTP
請求只會取用目前登入帳號的身分。

在「主動式 ETF 排行」按 `s` 可查看遮罩後的聯絡資訊、修改或經二次確認後刪除。刪除後該帳號停止
自動更新 13F；再次需要時會重新引導並取得同意。輸入會拒絕控制字元與不合法信箱，
避免 HTTP header injection。暫時性網路失敗會有限次退避重試，若仍失敗則保留前次成功
申報並標示為待重試。

Yahoo ETF 的 AUM、價格與持股也會檢查完整性。單一代碼缺資料時最多重試三次；若仍不完整，
不會用空回應覆蓋先前的有效持股，快取會標成 `retryable`，並由 30 分鐘背景更新週期再次反查。

### 主看板快速鍵

| 按鍵 | 功能 |
| --- | --- |
| `1` | 新增（批次連續輸入，含選擇權明細） |
| `2` / `r` | 重整報價 |
| `3` / `q` | 登出 |
| `4` | 事件日曆（財報 / FED / NFP / CPI） |
| `5` | 快照 |
| `6` | 類股 |
| `7` | 期權（預期波動、ATM 貴賤、Enter 看 IV−RV、持倉淨 Greeks） |
| `8` | ETF 觀察 |
| `9` | 對標：完整資產與 QQQ／VT |
| `i` / `o` | 宣告入金／出金，並同步 benchmark 資金流 |
| `e` / `x` / `space` | Holdings 表格內：編輯整筆／刪除（游標列或多選列）／多選標記 |
| `↑↓←→` / `Enter` / `Esc` | 表格與表單欄位巡覽、選取、返回 |

### 各分析畫面內快速鍵

| 畫面 | 按鍵 |
| --- | --- |
| 近期重大事件（`4`） | `T` 調整並保存事件顯示時區　`Esc` 返回 |
| 類股板塊分析（`6`） | `a` 新增板塊　`e` 編輯板塊　`d` 刪除板塊　`r` 重新整理　`Esc` 返回 |
| 期權觀察清單（`7`） | `↑↓` 波動貴賤欄選標的　`Enter` ATM IV−RV 走勢　`a` 新增標的　`d` 刪除標的　`h` 說明　`c` 重抓今日快照　`Esc` 返回 |
| 主動式 ETF（`8`） | `j` 建議　`w` 觀察清單　`a` 研究全表　`h` 說明　`c` 清除快取　`s` SEC 身分　`Esc` 返回 |
| 績效追蹤（`9`） | `t` 中途／重新啟用　`d` 確認取消追蹤　`i` 入金　`o` 出金　`r` 更新比較　`Esc` 返回 |

---

## 檔案結構說明

**TUI 與共用**
* `assettrack/tui.py`：全螢幕 Textual TUI 看板、所有畫面/ Modal 控制，以及套件唯一命令列進入點 `main()`。
* `assettrack/shared.py`：TUI 共用純邏輯（總經事件日程、部位立場 `position_stance_by_symbol`、`is_taiwan_position` 投資建議台股判定唯一來源、ASCII 圖繪製）。
* `assettrack/quotes.py`：報價、財報與總經數據並行抓取、台股標籤格式化、匯率換算、時區與 Beta。
* `assettrack/models.py`：`Position`、`PortfolioSnapshot` 等 Pydantic 模型。
* `assettrack/performance.py`：績效追蹤深層 module——toggle、追蹤斷層、JSON 資金帳本、週日估值、QQQ／VT 影子基準與追蹤期間持倉管控。

**分析與回測引擎（離線）**
* `assettrack/analysis.py`：主動式 ETF 多數性/規模性共識、大類資產輪動 + `backtest_etf_consensus` 回測。
* `assettrack/institutional.py`：AUM > US$5B 主動式 ETF 動態 universe、持股內容分類，以及四家機構 SEC 13F 季報抓取與標準化。
* `assettrack/options_analysis.py`：期權觀察（預期波動、淨 Greeks）＋程式庫內方向特徵（TUI 不呼叫）。
* `assettrack/options_valuation.py`：ATM 權利金相對已實現波動的貴賤與指示性模型差。TUI 期權頁使用此模組。
* `assettrack/options_forecasting.py`：期權方向預測的評分 interface（purged walk-forward、proper score）。TUI 目前不呼叫此模組。
* `assettrack/calibration.py`：使用 NYSE session、同一個 point-in-time 方向函式與不重疊 outcomes 的 walk-forward 回測。
* `assettrack/sector_analysis.py`：類股廣度共識 + `backtest_sector_flow` 回測。
* `assettrack/sector_predictive.py`：類股持續性 × 個股 MA／影線／連漲跌的 1–3 session
  條件機率模型；畫面與前瞻實驗共用 `compute_prediction_signals`。
* `assettrack/backtest_stats.py`：回測統計驗證（Wilson CI、對基準二項檢定＋多重比較調整、子區間穩定性、ESS）。
* `assettrack/direction_forecast_validation.py`：任何新方向政策上線前的 family-blind 驗證閘門。
* `assettrack/calibration_schedule.py`：legacy 校準狀態讀取；參數變更改由 QuantTrade Champion 契約匯入。
* `assettrack/exposure.py`：總曝險／淨曝險／槓桿比例。
* `assettrack/greeks.py`：Black-Scholes 希臘字母、理論價與隱含波動率反解。
* `assettrack/etf_trades.py`：由前後不同的真實持股狀態推導 ETF 買賣歷史。
* `assettrack/sec_identity.py`：SEC 識別名稱與聯絡信箱（Keychain，畫面只顯示遮罩）。
* `assettrack/ark_holdings.py`：ARK 持股抓取（進階 ETF 研究表）。
* `assettrack/market_sessions.py`：NYSE 交易日曆。

**資料與其他**
* `assettrack/storage.py`：資料存取層 —— `*_positions.json`（持倉）、SQLite `*_assettrack.db`（市值快照）、`etf_cache/options_cache/sector_cache` 逐日 `history/*.jsonl` 真實快照與 adjusted-close truth（2 年保留）、`*_calibration.json`（校準狀態）。
* `assettrack/touchid_helper`：Swift 編譯的 macOS Touch ID 驗證執行檔。
* `verification/`：行為驗證套件（認證、持倉、分析、TUI）。執行：`.venv/bin/pytest`
* `INVESTMENT_LOGIC.md`：投資判斷與回測邏輯技術文件。
* `data/`：依使用者隔離的持倉 JSON、SQLite 資料庫、各快取與校準狀態。
