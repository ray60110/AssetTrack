# AssetTrack — 跨券商投資組合追蹤器 (全螢幕 TUI 與 CLI 雙模式)

`AssetTrack` 是一款輕量且高效的命令列與終端機投資組合追蹤器，專為同時持有美股、台股、選擇權（含 Firstrade、Interactive Brokers 等多個券商管道）的投資人設計。本專案提供**全螢幕互動式 TUI 看板**與簡潔的 **CLI 子指令**，讓您無需開啟網頁瀏覽器，即可在終端機中獲得即時報價、多幣別計價、歷史績效回推與 Bloomberg 風格的圖表視覺化體驗。

---

## 運行架構流程圖 (Runtime Architecture Flow)

下圖展示了 `assettrack` 運行時的資料流、Textual TUI 畫面與子指令調用架構：

```mermaid
graph TD
    User([使用者 User]) -->|Touch ID / 密碼驗證| Auth[身分驗證與安全]
    Auth -->|通過| CLI[Typer CLI 入口: assettrack]
    
    CLI -->|預設 (無參數)| TUI[Textual TUI 主程式]
    
    subgraph TUI 全螢幕看板 (Textual TUI Screens)
        TUI --> Login[LoginScreen]
        Login --> Onboard[OnboardingModal]
        TUI --> Dash[DashboardScreen]
        TUI --> PerfHist[PerformanceHistoryScreen]
        TUI --> CalendarScreen[UpcomingEventsScreen]
        Dash -->|表格內格編輯| FieldEdit[FieldEditModal]
        Dash -->|部位操作選單| ActModal[PositionActionsModal]
    end

    subgraph CLI 子指令集 (CLI Commands)
        CLI -->|init| CmdInit[init: 初始化引導]
        CLI -->|add/edit/remove| CmdManage[add/edit/remove: 持倉管理]
        CLI -->|log-trade| CmdLog[log-trade: 交易登錄]
        CLI -->|value| CmdValue[value: 市值查詢]
        CLI -->|perf| CmdPerf[perf: 績效分析]
        CLI -->|history| CmdHistory[history: 歷史折線圖]
        CLI -->|calendar| CmdCal[calendar: 重大事件日曆]
        CLI -->|refresh| CmdRefresh[refresh: 儲存市值快照]
    end

    subgraph 資料存取與報價 (Data & Quotes)
        TUI & CLI -->|讀取/寫入持倉| JSONStore[(user_positions.json)]
        TUI & CLI -->|異步抓取即時報價| YahooFinance[yfinance API]
        YahooFinance -->|USDTWD=X 匯率| Convert[基準貨幣與匯率快取]
    end

    subgraph 本地資料庫 (SQLite Database)
        TUI & CmdRefresh -->|建立與儲存市值快照| DBSnap[Snapshots Table]
        PerfHist & CmdHistory -->|讀取歷史快照| DBSnap
        CmdLog -->|寫入交易紀錄| DBTrans[Transactions Table]
    end

    subgraph 終端機視覺化 (Terminal Visualization)
        Dash -->|指標與券商佔比| MetricsWidget[metrics / broker-dist panel]
        Dash -->|個股今日漲跌與盈虧| HoldingTable[holdings-table]
        PerfHist & CmdHistory -->|歷史市值趨勢| VisualChart[ASCII 歷史趨勢折線圖]
        CalendarScreen & CmdCal -->|日曆網格與重大日程| VisualCal[GMT+8 月曆與重大事件]
    end
```

---

## 主要功能特色

1. **安全認證與多使用者隔離**
   * 內建 macOS **Touch ID** 生物辨識驗證（透過 `touchid_helper`），登入不慎洩漏密碼時自動保護。
   * 支援多使用者設定檔（Profile），每個使用者擁有獨立的 `positions.json`、SQLite 資料庫及系統鑰匙圈金鑰（Keychain）。

2. **全螢幕 Textual TUI 看板**
   * 啟動 `assettrack` 即可直奔精美全螢幕終端機介面，支援鍵盤焦點切換（左右鍵於側邊欄與表格間移動）、上下滾動與滑鼠點擊。
   * **表格即時編輯**：在 Holdings 表格內，選取 Symbol、Type、Qty、Avg Cost、Market 等格子按下 `Enter` 即可呼叫 `FieldEditModal` 即時修改持倉資料；點擊其餘格子則彈出持倉操作選單。
   * **非阻塞式背景刷新**：主畫面自動建立背景 worker 執行緒，每 60 秒異步下載最新行情與匯率，避免畫面卡頓。

3. **重大財報與總經日曆 (Upcoming Events)**
   * 整合**持有部位個股財報**、**SOX 半導體十大成分股財報**與**三大重大總經數據 (FED/NFP/CPI) 公布日程**。
   * 支援**盤前/盤後時間判斷**與美東時間自動換算至**本地 GMT+8 時區**（依日光節約時間自動變更）。
   * 於主看板顯示 30 天內事件摘要；按下快速鍵 `5` 即可切換至日曆畫面，左側顯示 Sunday-based 月曆網格（以綠/黃/青反色標示事件），右側列出詳情。

4. **基準計價貨幣切換與即時報價**
   * 支援以 `USD` 或 `TWD` 作為基準計價貨幣。
   * 自動透過 `yfinance` 獲取即時的 `USDTWD=X` 匯率，並將所有持倉金額、成本基礎與未實現損益進行即時換算。

5. **精美終端機視覺化圖表**
   * **資產權重佔比條 (Holdings Weight %)**：以進度條字元 (`████░░ 40%`) 呈現資產配置比例。
   * **部位盈虧與分佈**：橫向呈現各持倉部位損益，獲利著綠色 (向右)、虧損著紅色 (向左)；並顯示各券商帳戶市值比例。
   * **歷史績效折線圖 (ASCII Trend Line)**：在 TUI 或 CLI 內以 `●` 與 `○` 繪製組合淨值與對比基準大盤（如 SPY, QQQ, ^GSPC）之走勢對照圖。

6. **純手動持倉管理 (Secure Manual Management)**
   * 為了最佳的資產記錄隱私與防護，系統專注於**純手動部位管理**（移除了不穩定且有隱私風險的第三方 API 串接與 CSV 導入指令）。
   * 提供互動式引導小精靈（Onboarding Screen / CLI），一步步引導您增、刪、查、改持倉資料。

---

## 指令使用說明

專案可透過 Python 虛擬環境中的 `assettrack` 執行：

```bash
# 0. 啟動互動式全螢幕 TUI 儀表板 (預設執行模式，需通過 Touch ID 或密碼登入)
assettrack -u username

# 1. 首次啟動帳戶初始化引導 (引導建立範例持倉或手動新增第一筆持倉)
assettrack init

# 2. 手動新增/修改/移除持倉部位 (提供互動式小精靈)
assettrack add
assettrack edit
assettrack remove

# 3. 登錄新交易 (買入/賣出，自動加減持倉、更新平均成本並寫入交易紀錄庫)
assettrack log-trade --symbol AAPL --qty 10 --price 185.0

# 4. 查看當前持倉即時市值與損益排行榜 (CLI 模式，盈綠虧紅)
assettrack value -c TWD --refresh

# 5. 評估與分析投資績效指標與策略表現 (計算 Weighted Portfolio Beta 等指標)
assettrack perf -c TWD

# 6. 保存當前持倉報價快照至資料庫中，建立歷史折線圖節點
assettrack refresh

# 7. 查看過去天的資產淨值歷史紀錄與與基準大盤對比的 ASCII 趨勢折線圖
assettrack history -c USD -d 60

# 8. 顯示投資持倉與總經事件日曆 (未來 90 天，依 GMT+8 分月表格呈現)
assettrack calendar -d 90
```

---

## 檔案結構說明

* `assettrack/cli.py`：CLI 入口、子指令註冊，以及終端機 CLI 圖表繪製核心（含 ASCII 折線圖、損益排行、總經事件回推與 Typer 指令）。
* `assettrack/tui.py`：全螢幕 Textual TUI 看板與所有 Modal 畫面控制。
* `assettrack/quotes.py`：報價取得、台股標籤格式化、yfinance 匯率換算、時區判定、Beta 獲取與財報並行抓取邏輯。
* `assettrack/storage.py`：資料存取層。處理 `user_positions.json` (持倉明細) 以及 SQLite `user_assettrack.db` (歷史快照 snapshots 與 交易紀錄 transactions)。
* `assettrack/models.py`：定義系統統一的 `Position`、`PortfolioSnapshot` 等 Pydantic 模型。
* `assettrack/touchid_helper`：Swift 編譯的獨立執行檔，提供 macOS Touch ID 生物辨識驗證功能。
* `data/*_positions.json`：依使用者隔離存放的持倉明細 JSON 檔案。
* `data/*_assettrack.db`：依使用者隔離的 SQLite 本機資料庫。
