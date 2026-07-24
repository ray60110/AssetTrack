# AssetTrack — 跨券商投資組合追蹤與投資建議終端機 (全螢幕 TUI)

`AssetTrack` 是一款輕量、離線優先的終端機投資組合追蹤器，主要針對美國股票與選擇權（Firstrade、Interactive Brokers 等多個券商管道）的投資人設計，可建立個人資料庫，密碼、指紋認證機制。提供除了即時報價、多幣別計價與 Bloomberg 風格的終端機視覺化之外，以**四大分析功能各自產生投資建議、每項建議以歷史真實快照做 walk-forward 回測驗證、回測本身再做統計顯著性檢定、四個面向再統整成主頁一張跨模型總結建議**為核心，四大功能包括：
    1. 重大事件歷程 `UpcomingEventsScreen`
    2. 主動式ETF排行、其操作分析 `ActiveETFsScreen`
    3. 期權觀察與分析 `OptionsWatchlistScreen`
    4. 類股板塊分析 `SectorAnalysisScreen`

並具備**每雙週／每週自動校準門檻**的能力。
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
        Dash --> CALI[CalibrationScreen 投資建議校準 k]
        Dash -->|表格內格編輯| FieldEdit[FieldEditModal]
        Dash -->|部位操作選單| ActModal[PositionActionsModal]
    end

    subgraph 分析與回測引擎 (離線、零網路)
        Dash --> XM[cross_model 跨模型總結建議]
        XM --> A1[analysis 主動式ETF跨基金共識 + 回測]
        XM --> A2[options_analysis 期權方向結論 + calibration 回測]
        XM --> A3[sector_analysis 類股廣度共識 + 回測]
        A1 & A2 & A3 --> BT[backtest_stats 統計驗證: Wilson/二項/ESS]
        Dash --> SCH[calibration_schedule 每雙週校準提案 需確認]
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
* 內建 macOS **Touch ID** 生物辨識驗證（透過 `touchid_helper`），並支援密碼登入與新帳戶註冊。
* 多使用者設定檔（Profile），每位使用者擁有獨立的 `positions.json`、SQLite 資料庫、快照與快取，以及系統鑰匙圈（Keychain）金鑰。
* 首次登入以互動式引導小精靈（`OnboardingModal`）帶你建立第一筆持倉。

### 2. 全螢幕 Dashboard 主看板
啟動即進入全螢幕終端機看板，由上而下堆疊下列面板（皆離線讀取本機資料即時運算）：
* **券商資產分布**與 **Total Value / Unrealized P&L / Portfolio Beta** 三格指標。
* **Holdings 表格**：支援表格內直接操作 —— `Enter` 就地編輯選取欄位、`e` 編輯整筆、`x` 刪除（游標列或多選列）、`space` 多選標記；右側常駐**近期重大事件**摘要。
* **🧭 跨模型總結建議**：把三項有回測背書的方向訊號以可信度加權，統整為整體傾向與把握度（高／中／低）。
* **三張結論卡**（由上而下）：類股板塊共識 → 期權觀察結論 → 主動式 ETF 趨勢共識，每檔標的附自己的回測命中率。
* **常駐狀態列**：登入後即啟動背景抓取，狀態列每秒顯示目前背景在抓什麼、閒置時顯示「資料已是最新」。之後每 30 分鐘依快取新鮮度冪等補抓，主頁每 60 秒重繪。

### 3. 四大投資建議功能 ＋ 回測 ＋ 統計驗證 ＋ 跨模型總結 ＋ 自動校準
所有分析 **100% 離線、零網路**，只讀取系統在背景刷新時逐日真實累積的本機快照（`etf_cache/options_cache/sector_cache` 的 `history/*.jsonl`），不回填、不臆測；資料不足時誠實顯示「資料收集中／累積中」而非假結論。本機分析資料保留 365 天。

* **近期重大事件與經濟日曆（快捷鍵 `4`）**：歷史事件只保留**當月與上個月**，並顯示未來 90 天的個股／SOX 財報與 FED/NFP/CPI 日程；更早的已完成事件會從表格與事件歷史資料排除，避免持續累積。事件以固定「日期／狀態／內容」欄位的縱向卡片排列；已發生卡片以淺灰底和 `✓` 表示完成，待發生卡片以 `○` 與事件類型色框呈現。已發生財報會更新 Revenue／CAPEX／EBIT／FCF 當期值、去年同期與 YoY；已發生總經事件會列出本期、前期與變動。預設以 `Asia/Taipei` 顯示日期時間，畫面內按 `T` 可輸入任意 IANA 時區並保存為使用者偏好。
* **主動式 ETF 跨基金共識（快捷鍵 `6`）**：於 14 天視窗比較逐日真實快照，輸出「大類資產輪動」「個股同時買入/賣出」「單一大額規模性變動」三層建議。方向需**真實股數變化與權重變化兩訊號同向**才算；共識平手一律歸中性（不偏多）。
* **期權方向結論（快捷鍵 `7`）**：每檔標的的綜合方向結論由兩訊號合成 —— Dollar Delta OI Skew（Delta 權重名義曝光）＋扣除 delta/gamma/theta/DTE 後的 **OI 加權重定價殘差**；skew 另以同側殘差交叉確認買/賣方向，矛盾或未確認時標「觀望」不硬給方向。
* **類股板塊共識（快捷鍵 `8`）**：以廣度擴散指數＋市值加權報酬雙訊號同向、再加持續性過濾，抓「整個族群普遍同向」而非單一名股。
* **回測層**：三項有方向的功能共用同一套 **walk-forward** 骨架（只用 ≤T 的真實快照重推當日結論，再看 T 之後的真實走勢是否同向），前瞻期 1/5/10/14/30/60 天並列，命中率一律附「基準上漲率」與「超額 edge」。
* **統計驗證層**：命中率再經 Wilson 信賴區間、對基準的單尾二項檢定（Bonferroni 多重比較調整）、前後子區間穩定性；並以**有效獨立樣本數 (ESS)** 消除重疊視窗自相關造成的顯著性高估。
* **跨模型總結建議（主頁）**：三方向以各自 14 天前瞻期的回測可信度加權合成，事件作謹慎度修正；證據不足時誠實回報「資料累積中」。
* **每雙週／每週自動校準（快捷鍵 `k`）**：定期依回測結果提出門檻調整建議，一律**需使用者確認才套用**（可【套用】【略過】【立即重算】【切換週期】）。

### 4. 重大財報與總經日曆
* 整合持有部位個股財報、SOX 半導體十大成分股財報與三大重大總經數據（FED/NFP/CPI）公佈日程。
* 支援盤前/盤後判斷與美東時間自動換算至本地 GMT+8（依日光節約自動調整）；日曆畫面左側月曆網格反色標示事件、右側列出詳情。

### 5. 基準計價貨幣切換與即時報價
* 支援以 `USD` 或 `TWD` 作為基準計價貨幣，自動透過 `yfinance` 取得 `USDTWD=X` 匯率，將所有持倉金額、成本基礎與未實現損益即時換算。

### 6. 純手動持倉管理
* 專注於純手動部位管理（不做第三方 API 串接與 CSV 導入）。新增表單支援「儲存並繼續」批次連續輸入、輸入 Symbol 自動推斷市場/幣別（如 `2330` → TW/TWD），非必要欄位收於可展開的進階欄位；選擇權含明細。
* 非阻塞式背景 worker 執行緒異步下載行情與匯率，避免畫面卡頓。

---

## 指令使用說明

```bash
# 啟動互動式全螢幕 TUI（唯一執行模式，需通過 Touch ID 或密碼登入）
assettrack -u username
# 或省略 -u/--user，預設使用 "default" 帳戶
assettrack
```

### 主看板快速鍵

| 按鍵 | 功能 |
| --- | --- |
| `1` | 新增部位（支援批次連續輸入多筆，含選擇權明細） |
| `2` / `r` | 立即重整報價 |
| `3` / `q` | 安全登出 |
| `4` | 近期重大事件與經濟日曆（財報 / FED / NFP / CPI ＋指標動態解析） |
| `5` | 儲存市值快照 |
| `6` | 主動式 ETF 排行與跨基金趨勢共識 |
| `7` | 期權觀察清單與方向結論 |
| `8` | 類股板塊分析 |
| `k` | 投資建議校準（檢視待確認調整、套用/略過/切換週期） |
| `e` / `x` / `space` | Holdings 表格內：編輯整筆／刪除（游標列或多選列）／多選標記 |
| `↑↓←→` / `Enter` / `Esc` | 表格與表單欄位巡覽、選取、返回 |

### 各分析畫面內快速鍵

| 畫面 | 按鍵 |
| --- | --- |
| 近期重大事件（`4`） | `T` 調整並保存事件顯示時區　`Esc` 返回 |
| 主動式 ETF（`6`） | `a` 進階分析　`c` 清除快取重載　`Esc` 返回 |
| 期權觀察清單（`7`） | `a` 新增標的　`d` 刪除標的　`k` 校準狀態　`h` 說明　`c` 清除快取重載　`Esc` 返回 |
| 類股板塊分析（`8`） | `a` 新增板塊　`e` 編輯板塊　`d` 刪除板塊　`r` 重新整理　`Esc` 返回 |

---

## 檔案結構說明

**TUI 與共用**
* `assettrack/tui.py`：全螢幕 Textual TUI 看板、所有畫面/ Modal 控制，以及套件唯一命令列進入點 `main()`。
* `assettrack/shared.py`：TUI 共用純邏輯（總經事件日程、部位立場 `position_stance_by_symbol`、`is_taiwan_position` 投資建議台股判定唯一來源、ASCII 圖繪製）。
* `assettrack/quotes.py`：報價、財報與總經數據並行抓取、台股標籤格式化、匯率換算、時區與 Beta。
* `assettrack/models.py`：`Position`、`PortfolioSnapshot` 等 Pydantic 模型。

**分析與回測引擎（離線）**
* `assettrack/analysis.py`：主動式 ETF 多數性/規模性共識、大類資產輪動 + `backtest_etf_consensus` 回測。
* `assettrack/options_analysis.py`：期權方向結論（Dollar Delta OI skew + OI 加權重定價殘差）、淨 Greeks、分析結論卡。
* `assettrack/calibration.py`：期權方向結論的 walk-forward 回測與校準狀態標籤。
* `assettrack/sector_analysis.py`：類股廣度共識 + `backtest_sector_flow` 回測。
* `assettrack/backtest_stats.py`：回測統計驗證（Wilson CI、對基準二項檢定＋多重比較調整、子區間穩定性、ESS）。
* `assettrack/cross_model.py`：跨模型總結（三方向以回測可信度加權、事件作謹慎度修正）。
* `assettrack/calibration_schedule.py`：每雙週/週自動校準引擎（提案顯著性把關、需確認才套用）。
* `assettrack/greeks.py`：Black-Scholes 希臘字母、理論價與隱含波動率反解。
* `assettrack/etf_trades.py`：由每日持股快照推導 ETF 買賣歷史。

**資料與其他**
* `assettrack/storage.py`：資料存取層 —— `*_positions.json`（持倉）、SQLite `*_assettrack.db`（市值快照）、`etf_cache/options_cache/sector_cache` 逐日 `history/*.jsonl` 真實快照（365 天保留）、`*_calibration.json`（校準狀態）。
* `assettrack/touchid_helper`：Swift 編譯的 macOS Touch ID 驗證執行檔。
* `INVESTMENT_LOGIC.md`：投資判斷與回測邏輯技術文件。
* `data/`：依使用者隔離的持倉 JSON、SQLite 資料庫、各快取與校準狀態。
