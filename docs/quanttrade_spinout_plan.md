# QuantTrade Spin-out 遷移計畫

**日期**：2026-08-06　**狀態**：待核准，尚未動任何檔案
**目標**：把 AssetTrack 的「策略實驗室」整套抽成獨立 TUI 應用 QuantTrade，AssetTrack 保留原本的資產追蹤功能。

依 2026-08-06 的六項決策：抽共用 core 套件／QuantTrade 獨立 db 並搬移現有資料／capture 整套搬走／AssetTrack 移除鍵 `0` 與鍵 `k`／QuantTrade 自寫 ingestion／資料目錄各自一份。

---

## 1. 相依邊界（AST 全掃描結果，非估計）

把 `tui.py` 對實驗室的引用剔除後重算傳遞閉包，邊界**完全乾淨**：core 不反向依賴 QuantTrade 專屬模組，AssetTrack 專屬模組也不依賴它們。

### A. `assettrack-core` — 兩邊都要，16 模組 / 12,495 行

| 模組 | 行數 | 模組 | 行數 |
|---|---:|---|---:|
| analysis | 1,959 | options_analysis | 1,786 |
| quotes | 1,900 | storage | 1,517 |
| sector_analysis | 923 | sector_predictive | 828 |
| institutional | 675 | shared | 652 |
| backtest_stats | 463 | calibration | 427 |
| cross_model | 404 | models | 326 |
| etf_trades | 249 | greeks | 135 |
| sec_identity | 134 | market_sessions | 117 |

### B. QuantTrade 專屬 — 整包搬走，9 模組 / 10,644 行

`feedback` 2,149｜`experiment` 2,141｜`evaluation` 1,491｜`experiment_runtime` 1,206｜
`feedback_cycle` 1,128｜`experiment_policies` 1,104｜`replay` 886｜
`statistical_validation` 264｜`experiment_test_mode` 275

### C. AssetTrack 留下 — 4 模組 / 11,008 行

`tui` 9,735｜`performance` 637｜`calibration_schedule` 431｜`ark_holdings` 205

---

## 2. 資料切分

`<user>_assettrack.db`（2.1 MB）的 21 張表，交集為零：

**搬去 QuantTrade（18 張）** — `forecast_records` 972 筆、`outcomes` 659 筆、`policy_versions` 13、
`evaluation_runs` 10、`failure_diagnostics` 30、`policy_assignments` 9、`policy_events` 9、
`feedback_checkpoints` 1，其餘 10 張為空（promotion／rollback／candidate／corrections／locks）＋`schema_migrations` v1–13。

**AssetTrack 留下（3 張）** — `positions_history` 22、`snapshots` 2、`transactions` 1。

**快取目錄**：`etf_cache` 33M、`institution_cache` 48M、`options_cache` 21M、`benchmark_cache` 9.4M、
`sector_cache` 344K → 複製一份到 `QuantTrade/data/`（本機 `cp`，不經雲端）。

---

## 3. 目標結構

```
<workspace-parent>/
├── assettrack-core/                 ← 新增
│   ├── pyproject.toml               (name: assettrack-core)
│   └── assettrack_core/             16 模組，內部相對 import 不動
│       └── paths.py                 ← 新增：可設定的資料目錄解析
├── AssetTrack/                      ← 保留，瘦身
│   └── assettrack/                  tui / performance / calibration_schedule / ark_holdings
└── QuantTrade/                      ← 新增
    ├── pyproject.toml               (依賴 assettrack-core)
    ├── data/                        快取複本 + <user>_quanttrade.db
    └── quanttrade/
        ├── 9 個實驗室模組
        ├── paths.py                 quanttrade.db 路徑與 migration
        ├── ingest.py                ← 新增：headless 抓取
        ├── cli.py                   ← 新增：ingest / cycle / report
        └── tui/                     ← 新增：TUI 介面
```

`get_data_dir()` 目前是 `Path.cwd()/"data"`，綁工作目錄——從別的目錄啟動 AssetTrack 就看不到資料，本來就是隱形 bug。core 改為：`$ASSETTRACK_DATA_DIR` → `$QUANTTRADE_DATA_DIR`（由呼叫端 app 設定）→ 退回 `cwd/data`。AssetTrack 行為不變。

---

## 4. 階段

| 階段 | 內容 | 產出 |
|---|---|---|
| **0 安全網** | 兩 repo 各開 branch；跑現有 20 個測試存 baseline；備份 db 與 data/ | baseline 報告 |
| **1 建 core** | 建目錄與 pyproject；搬 16 模組（`git filter-repo` 保留 blame）；新增 `paths.py`；`storage` 改用它 | core 可 import、獨立測試通過 |
| **2 AssetTrack 改接** | 4 個留下模組改 `from assettrack_core.X import`；移除 `ExperimentTestModeScreen`(5117–5594)、`CalibrationModal`(3195–3627)、`CalibrationScreen`(9104–9286)、背景 cycle 三函式(3562–3628) 與其在 `_background_data_refresh`(9543) 的呼叫；移除 bindings `0`／`k`（3708、3711、8623）與 `action_calibration`／`action_experiment_test_mode`；刪 9 個實驗測試 | AssetTrack 啟動、四大畫面正常 |
| **3 建 QuantTrade** | 目錄與 pyproject；搬 9 模組並改 import 前綴；`paths.py` 指向 `{user}_quanttrade.db`；搬 9 個測試與 5 個 scripts | 測試全綠（用新 db） |
| **4 資料遷移** | 遷移腳本：18 張表整批複製，逐表比對筆數、外鍵完整性、`schema_migrations` 對齊；AssetTrack 端 `DROP` 舊表（先備份） | 遷移報告 + 可回滾備份 |
| **5 ingestion** | `ingest.py` 呼叫 core 的 `quotes`／`institutional` 抓取＋`storage.append_*_daily_snapshot`（格式仍由 core 定義，只有排程是新寫的）；`cli.py` 提供 `quanttrade ingest` / `cycle` | 可獨立跑完一次完整迴路 |
| **6 TUI** | 移植 Test Mode 為主畫面；新增 Ledger 瀏覽、失效診斷、候選提案、Replay 結果、Promotion Gate 五個畫面 | `quanttrade` 可啟動 |
| **7 驗收** | 兩邊測試全綠；**冪等驗證**：QuantTrade 對遷移後資料重跑 feedback cycle，結果與遷移前逐欄位一致 | 驗收報告 |

階段 0–4 是機械性搬移，風險可控；5–7 是新建功能。建議 0–4 一次做完並驗收，再進 5–7。

---

## 5. 三個必須正視的後果

### 5.1 「結論＝被回測＝同一函式」的不變式會斷（**最重要**）

AssetTrack 的畫面經 `_active_params()` 讀 legacy `{user}_calibration.json` 取門檻；QuantTrade 的 ledger 用 Policy Version 的 `parameters`。拆開後這兩組值各自演化，**AssetTrack 顯示的結論會不再是 QuantTrade 正在評估的那個結論**——這正是 bug#00089 當初要鎖住的東西。

**建議**：QuantTrade 每次 cycle 結束把各 family 的 champion parameters 匯出成 `data/champion_params.json`，AssetTrack 的 `_active_params()` 改讀它、legacy JSON 降為歷史顯示（符合設計文件 §4 與 D-12）。這是一個小契約，但必須在階段 2 就做，否則拆完當天兩邊就開始漂移。

### 5.2 資料目錄分家 ⇒ 抓取次數加倍

兩個 app 各自抓 ETF 持股與選擇權鏈。除了流量，還有一個實質風險：**同一天兩邊抓到的快照可能不同**（盤中抓取時間差）。QuantTrade 的 ledger 以自己的快取為準即可自洽，但 AssetTrack 畫面顯示的數字會跟 ledger 記錄的略有出入。若日後覺得困擾，回頭共用資料目錄即可解決。

### 5.3 搬移不會修掉既有的 replay 條件化偏誤

`replay_dataset_from_ledger` 只取 Champion 已發且已結算的 forecast，Challenger 因此只在「Champion 願意出手」的樣本上被評分——收緊門檻的候選會系統性地看起來與 Champion 無異。這是搬過去之後仍在的問題，建議列為 QuantTrade 的第一個 bug。

---

## 6. 驗收條件

1. AssetTrack 啟動，Dashboard／ActiveETFs／AdvancedAnalysis／SectorAnalysis／OptionsWatchlist 五畫面正常，`0`／`k` 鍵不再存在。
2. AssetTrack 的 11 個非實驗測試全綠。
3. QuantTrade 的 9 個實驗測試全綠。
4. 遷移後 `forecast_records` 972 / `outcomes` 659 / `policy_versions` 13 筆數一致，外鍵無孤兒。
5. 同一份資料重跑 `run_local_feedback_cycle`，六個 family 的 outcome、health state、diagnostics 與遷移前逐欄位一致。
6. AssetTrack 的 db 不再含任何 ledger 表；QuantTrade 的 db 不含 positions／transactions。

---

## 7. 待確認

1. **core 套件命名**：`assettrack-core`（import 為 `assettrack_core`）可以嗎？還是想取個中性的名字（例如 `marketcore`）——反正兩邊都會依賴它。
2. **git 歷史**：要不要用 `git filter-repo` 保留 23,139 行的 blame？會慢一點但值得。若不需要，直接複製檔案。
3. **§5.1 的 champion params 契約**現在做，還是先讓兩邊漂移、日後再處理？
4. 先做**階段 0–4** 驗收後再繼續，還是一路做到 TUI 完成？
