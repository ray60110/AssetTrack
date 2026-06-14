# AssetTrack — 跨券商投資組合追蹤器 (純 CLI 模式)

`AssetTrack` 是一款輕量且高效的命令列投資組合追蹤器，專為同時持有美股、台股、選擇權（含 Firstrade、Interactive Brokers 等多個券商管道）的投資人設計。本專案已全面遷移至 **純 CLI 運行架構**，讓您無需開啟網頁瀏覽器，即可在終端機中獲得即時報價、多幣別切換與 Bloomberg 風格的圖表視覺化體驗。

---

## 運行架構流程圖 (Runtime Architecture Flow)

下圖展示了 `assettrack` 運行時的資料流與指令調用架構：

```mermaid
graph TD
    User([使用者 User]) -->|Touch ID 驗證| Auth[身分驗證與安全]
    Auth -->|通過| CLI[Typer CLI 入口: assettrack]
    
    subgraph CLI 指令集 (CLI Commands)
        CLI -->|預設| CmdDashboard[互動式儀表板 Dashboard]
        CLI -->|value| CmdValue[資產市值查詢]
        CLI -->|history| CmdHistory[歷史折線圖]
        CLI -->|log-trade| CmdLogTrade[交易登錄與損益]
        CLI -->|add/edit/remove| CmdManage[持倉管理]
        CLI -->|import-csv| CmdImport[CSV部位匯入]
        CLI -->|set-credential| CmdCred[券商API金鑰管理]
    end

    subgraph 資料處理與報價強化 (Data Processing)
        CmdDashboard & CmdValue & CmdHistory -->|讀取專屬持倉| JSONStore[(user_positions.json)]
        CmdDashboard & CmdValue & CmdHistory -->|抓取即時報價與匯率| YahooFinance[yfinance API]
        YahooFinance -->|USDTWD=X| Convert[基準貨幣動態換算]
    end

    subgraph 本地資料庫 (SQLite Database)
        CmdDashboard & CmdRefresh -->|建立並儲存快照| DBSnap[Snapshots Table]
        CmdHistory -->|讀取歷史快照紀錄| DBSnap
        CmdLogTrade -->|寫入交易紀錄| DBTrans[Transactions Table]
    end

    subgraph 終端機視覺化呈現 (Rich Terminal Output)
        Convert -->|資產比例進度條| VisualAlloc[資產權重佔比 Progress Bar]
        Convert -->|未實現損益對比條| VisualPnL[部位盈虧排行榜 盈綠/虧紅]
        DBSnap -->|歷史淨值數據| VisualChart[ASCII 歷史趨勢折線圖]
    end
```

---

## 主要功能特色

1. **安全認證與多使用者隔離**
   * 內建 macOS **Touch ID** 生物辨識驗證（透過 `touchid_helper`）。
   * 支援多使用者設定檔（Profile），每個使用者擁有獨立的 `positions.json`、SQLite 資料庫及系統鑰匙圈金鑰（Keychain）。

2. **基準計價貨幣切換 (`-c` / `--currency`)**
   * 支援 `USD` 與 `TWD` 作為基準貨幣。
   * 自動透過 `yfinance` 獲取即時的 `USDTWD=X` 匯率，並將所有持倉金額、成本基礎與未實現損益進行即時換算。

3. **精美終端機視覺化圖表**
   * **互動式儀表板**：輸入 `assettrack` 直接進入互動式 Dashboard 迴圈。
   * **資產權重佔比條 (Holdings Weight %)**：以進度條字元 (`████░░ 40%`) 呈現資產配置比例。
   * **部位盈虧排行榜 (P&L Performance Bar)**：橫向呈現各持倉部位損益，**獲利著綠色 (向右)**、**虧損著紅色 (向左)**。
   * **歷史淨值折線圖 (ASCII Trend Line)**：在終端機內以 `●` 繪製折線圖，自動計算價格區間（Y 軸）與日期（X 軸）。

4. **台美股與選擇權標準化支援**
   * 台股代碼（如 `2330`）會自動標準化為 `2330.TW`。
   * 自動解析 OCC 標準選擇權代碼格式，支援模糊空格匹配。

5. **多管道持倉與交易管理**
   * **手動新增/編輯/移除**：提供互動式引導小精靈，逐步引導建立或修改各部位持倉。
   * **交易紀錄登錄 (Log Trade)**：買賣操作自動同步加減持倉、更新平均成本，並將紀錄寫入 SQLite 資料庫的 Transactions 表。
   * **CSV 匯入**：支援 Firstrade（Tax Center 導出）與 Interactive Brokers (IBKR) 導出的 CSV 部位直接匯入並合併。

---

## 指令使用說明

專案可透過 Python 虛擬環境中的 `assettrack` 執行：

```bash
# 0. 啟動互動式儀表板 (需通過 Touch ID 驗證，預設會進入此迴圈)
assettrack -u username

# 1. 首次啟動初始化引導小精靈 (引導導入 CSV/API 或手動新增)
assettrack init

# 2. 登錄新交易 (買入/賣出，自動加減持倉、更新平均成本並存入資料庫)
assettrack log-trade --broker manual --symbol AAPL

# 3. 評估與分析投資績效指標與策略表現 (支援 -c TWD 與 -c USD 切換)
assettrack perf -c TWD

# 4. 查看當前持倉即時市值與損益排行榜 (盈綠虧紅)
assettrack value -c TWD --refresh

# 5. 互動式新增/編輯/移除部位
assettrack add --broker manual
assettrack edit
assettrack remove

# 6. 匯入券商導出的 CSV 部位檔 (支援自動合併)
assettrack import-csv /path/to/firstrade_tax_center.csv --broker firstrade --merge

# 7. 保存當前持倉報價快照至資料庫中，建立歷史折線圖節點
assettrack refresh --save

# 8. 查看過去 30 天的資產淨值歷史紀錄與 ASCII 趨勢折線圖
assettrack history -c TWD --days 30

# 9. 憑證與金鑰管理 (儲存於 macOS Keychain)
assettrack set-credential --broker firstrade
assettrack clear-credential --broker firstrade
```

---

## 檔案結構說明

* `assettrack/cli.py`：CLI 入口與終端機 UI 圖表繪製核心（含 ASCII 折線圖、損益排行、互動式儀表板與各類 Typer 指令）。
* `assettrack/quotes.py`：報價取得、台股標準化與 `yfinance` 匯率換算邏輯。
* `assettrack/storage.py`：資料存取層。處理 `user_positions.json` (持倉明細) 以及 SQLite `user_assettrack.db` (歷史快照 snapshots 與 交易紀錄 transactions)。
* `assettrack/models.py`：定義系統統一的 `Position`、`PortfolioSnapshot` 等 Pydantic 模型。
* `assettrack/touchid_helper/`：Swift 編譯的獨立執行檔，提供 macOS Touch ID 生物辨識驗證功能。
* `assettrack/brokers/`：包含 Firstrade API、IBKR API 以及 CSV 解析器的對接模組。
* `data/*_positions.json`：依使用者隔離存放的持倉明細 JSON 檔案。
* `data/*_assettrack.db`：依使用者隔離的 SQLite 本機資料庫。
