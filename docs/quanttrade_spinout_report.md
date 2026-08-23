# QuantTrade Spin-out 階段 0–4 驗收報告

**日期**：2026-08-06　**範圍**：階段 0–4（拆分、資料遷移、漂移守門）。階段 5–7（ingestion、TUI）未做。

---

## 驗收條件逐項

| # | 條件 | 結果 |
|---|---|---|
| 1 | AssetTrack 啟動正常，`0`／`k` 鍵不存在 | ✅ Dashboard bindings 剩 `1-9,i,o,q,r,ctrl+c`；選擇權頁剩 `a,c,d,h,q,escape` |
| 2 | AssetTrack 非實驗測試全綠 | ✅ **139 passed + 7 subtests, 1 failed** |
| 3 | QuantTrade 測試全綠 | ✅ **199 passed, 0 failed** |
| 4 | 遷移筆數一致、外鍵無孤兒 | ✅ 18 張表全數相符；`foreign_key_check` 無輸出；`integrity_check` = ok |
| 5 | feedback cycle 逐欄位一致 | ✅ **兩份報告逐位元組完全相同** |
| 6 | 兩邊 db 不再交集 | ✅ AssetTrack 剩 3 張表（1,596 KB → 32 KB）；QuantTrade 18 張表 |

**條件 2 的那個 failed 是 baseline 就存在的**：`test_advanced_analysis_sources` 的 textual async 測試（`coroutine raised StopIteration`），源於驗證容器的 textual 版本與你 venv 的 python3.14 不同，非程式問題。分割前後同一個測試、同樣失敗——**零回歸**。

### 條件 5 的做法

不是「跑一次看起來對」，而是 **分割前的程式碼＋原始 db** 與 **分割後的 QuantTrade＋遷移後 db** 各跑一次 `run_local_feedback_cycle("<user>", 2026-08-05)`，把六個 family 的 outcome、detail、health state／reason／warning_mode／direction_may_continue、matured_count、new_matured、diagnostics、candidate id 全部序列化後比對：

```
sector_flow          no_evidence      health=-            matured=   0 new=  0 diag=[]
sector_predictive    assessed         health=warming_up   matured= 659 new=  0 diag=['underpowered']
etf_consensus        no_evidence      health=-            matured=   0 new=  0 diag=[]
etf_selection_tilt   no_evidence      health=-            matured=   0 new=  0 diag=[]
options              no_evidence      health=-            matured=   0 new=  0 diag=[]
cross_model          no_evidence      health=-            matured=   0 new=  0 diag=[]
```

> ⚠ 六族裡有五族是 `no_evidence`。這是**分割前就是這樣**（前後報告完全相同可證），與 memory 的 `backtest_loop_audit_2026_08_03`「生產只跑 sector 族」一致。不是這次搬移造成的，但它意味著閉環目前實際只有一族在累積證據。

---

## 實際完成的內容

### 檔案分佈

| | 模組 | 測試 | scripts |
|---|---|---|---|
| **QuantTrade**（新） | 25 複製 + 4 自有（`paths`、`capture`、`champion_params`、`__init__`） | 9 搬移 + 1 新增（漂移守門） | 5 |
| **AssetTrack**（保留） | 21（原 30） | 10（原 20） | 1（原 6） |

`tui.py` 9,736 → 8,742 行（少 994 行）。移出的 9 個實驗室模組、10 個測試、5 個 scripts 放在 `AssetTrack/_to_delete/`——`device_bash` 不允許刪檔，**確認無誤後請你手動刪除該資料夾**。

### 計畫外的三項調整

**(a) `tui.py` 裡的實驗室函式比計畫多。** 原計畫只列了三個背景 cycle 函式，實際還有 `_refresh_experiment_benchmark_truth`、`experiment_truth_symbols`、`_refresh_experiment_symbol_truth`——結算用的 ground-truth 價格寫入。全部收進 `quanttrade/capture.py`（406 行，逐字搬移）。

**(b) `CrossModelEvidence` 與 `session_aligned_snapshots` 移進 `cross_model.py`。** 主頁的跨模型卡片透過 `build_cross_model_evidence` 用到這兩個名稱，但它們原本住在 `experiment_policies` / `experiment_runtime`——那兩個模組要移除，主頁就會壞。它們描述的是「一份跨模型證據長什麼樣子」而非實驗方法，所以放進兩邊都持有的 `cross_model.py`，兩側同步施作，逐位元組相同不受影響。

**(c) db 路徑切換點放在 `quanttrade/paths.py`**，不改 `storage.py`。實驗室模組的 7 處 `from .storage import get_db_path` 改接 `paths`。這條界線讓 16 個分析模組能永遠保持逐字相同——而那正是 Policy Version 身分的基礎。

### Champion 參數契約（§5.1）已上線

QuantTrade 每次 cycle 結束匯出 `data/{user}_champion_params.json`，AssetTrack 的 `_active_params()` 優先讀它、讀不到才回退 legacy。實測輸出：

```json
"params": {"etf": {"consensus_threshold": 0.5, "min_etfs_evaluated": 4},
           "sector": {"breadth_threshold": 0.5, "min_days": 3},
           "options": {"bias_min_pct": 0.03}},
"policy_version_ids": {"etf_consensus": "pv_37fb607c…", "sector_flow": "pv_038dfde4…"},
"unmapped": {"options.bias_min_pct": "no Policy Version equivalent"}
```

`options.bias_min_pct`（現價百分比）與 adapter 的 `bias_min_abs`（每股美元）單位不同、換算需要當時股價，事後補算等於偽造點時資訊——所以誠實列進 `unmapped`，不硬湊。

### 漂移守門

`tools/check_drift.py` + `tests/test_shared_module_drift.py`：16 個共有模組的 sha256 必須與分割基準相同，且（AssetTrack 在本機時）兩邊必須一致。目前 **16/16 全部一致**。

---

## 備份與回滾

`AssetTrack/_spinout_backup/`：

- `assettrack_src_20260806.tgz` — 分割前完整原始碼
- `<user>_assettrack.db.<date>.bak` — 分割前完整資料庫（含全部 ledger）
- `at_stage2.tgz` / `qt_stage2.tgz` / `qt_stage4.tgz` — 各階段快照

回滾方式：還原 `.bak` 覆蓋 `data/<user>_assettrack.db`，解開 `assettrack_src_*.tgz` 覆蓋原始碼。QuantTrade 目錄可整個移除，不影響 AssetTrack。

⚠ **AssetTrack 的工作目錄在分割前就有 14 個檔案未 commit**（含 `tui.py`、`analysis.py`、`storage.py` 等）。我沒有動 git——沒有 commit、沒有開 branch、沒有 stash。所以 `git diff` 現在會同時包含你原本的未提交變更與這次的分割改動。要分開的話，`_spinout_backup/assettrack_src_20260806.tgz` 是分割前（但含你未提交變更）的狀態。

---

## 下一步

**階段 5 — ingestion**：`_fetch_and_cache_etf_symbols`（298 行）、`_fetch_and_cache_options_underlyings`、`_fetch_and_cache_sector_groups` 仍在 AssetTrack 的 `tui.py` 裡且與畫面纏繞。在 QuantTrade 自寫 headless 版本之前，它仍依賴 AssetTrack 寫進**自己那份**快取的快照——而兩邊資料目錄已分家，所以 QuantTrade 目前的快取是 2026-08-06 的靜態複本，不會更新。

**階段 6 — TUI**：`test_experiment_tui.py` 沒有搬（它測的是 AssetTrack 的 `ExperimentTestModeScreen`），要在 QuantTrade 的畫面完成後重寫。

**建議優先於階段 6**：先修 replay 條件化偏誤。目前 Challenger 只在 Champion 願意出手的樣本上被評分，這讓整條升級路徑的結論不可信——在那之前把 Gate 接到畫面上，只是把一個有偏誤的判斷做得更好看。

---

## 補記：雲端掛載對 SQLite 的限制（2026-08-06）

這次遷移在雲端環境操作，而 Claude 端掛載你磁碟的方式**不支援 SQLite 的檔案鎖定**：
在掛載路徑上直接 `sqlite3.connect()` 會拋 `disk I/O error`，而且失敗的連線會留下一個
hot journal，下一次開啟時把資料庫回滾成空的（實際發生過一次，已從備份還原並用 md5 驗證）。

因此所有資料庫操作都是「複製到本機暫存區 → 操作 → 複製回掛載點」，驗證改用 md5 比對而非
再次開啟資料庫。**這是雲端掛載的限制，你在自己的 Mac 上直接執行 QuantTrade 不會遇到。**

一個殘留物需要你處理：`QuantTrade/data/<user>_quanttrade.db-journal`。掛載點不允許
`rm` 也不允許 `mv`，我已把它**截斷成 0 位元組**（SQLite 只認有效標頭的 journal，零長度
會被忽略，不會觸發回滾），但最好還是手動刪掉它：

    rm <quanttrade-data-dir>/<user>_quanttrade.db-journal

資料庫本身的 md5 為 `c39a6f8b6e691f8d6d762574b8251b83`（1,609,728 bytes），
與遷移驗證通過時的那一份相同。
