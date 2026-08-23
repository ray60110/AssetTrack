# Abstain 率分析：門檻過高，還是資料不足？

2026-08-04。資料來源：`data/<user>_assettrack.db`（真實 ledger）、
`data/sector_cache/<user>_predictive.json`（真實模型）、
`data/sector_cache/<user>_summaries.json`（今日 evidence）。全部實測，非推估。

**結論先講：門檻不是主因，abstain 率也不是主因。真正的瓶頸是 Policy Version 一天重鑄四次。**

---

## 0. 真實 ledger 現況

| family | 總預測 | 方向性 | abstain | abstain 率 | 涵蓋 sessions |
|---|---|---|---|---|---|
| sector_predictive | 570 | 34 | 536 | **94.0%** | 2（07-31、08-03）|
| sector_flow | 24 | 14 | 10 | **41.7%** | 2 |
| etf_consensus / etf_selection_tilt / options / cross_model | **0** | 0 | 0 | — | 0 |

其餘四族**一筆都沒有**——bug#00127 接了 runtime，但實際上從未產出過紀錄。這是另一件事，
但必須先講清楚：所謂「幾乎全部都是 abstain」，其中四個 family 是「連 abstain 都沒有」。

現任 Champion `pv_d918d0c9` 的 evaluation metrics（實際存在 DB 裡）：

```
forecast_count            228
directional_count          22      → direction_coverage 9.6%
matured_count              36      （其餘 192 筆未到期）
distinct_signal_sessions    1      ←── 關鍵
evaluable_direction_count   1      ←── 關鍵
effective_sample_size    null
```

14 筆診斷全部是 `underpowered`，`required_effective_sample_size: 30`，
`effective_sample_size: 0 或 1`。`candidate_proposals` = 0。迴路正確地什麼都沒做。

---

## 1. 門檻到底擋掉了什麼（實測 gate attrition）

`compute_prediction_signals` 有**六道**閘門，但 ledger 只記錄一個
`abstain_reason: "prediction_thresholds_not_met"`——**無法從紀錄分辨是哪一道擋的**。
所以以下是離線重跑真實模型算出來的。

### 1a. 模型層：175 patterns × 3 horizons = 525 個樣本格

| 閘門 | 擋掉 | 可調？ |
|---|---|---|
| n < `min_samples` (30) | 171 | 是，白名單 |
| edge < `min_edge` (0.03) | 179 | 是，白名單 |
| 穩定性（early/late 需同側） | 54 | **否。無參數、無紀錄** |
| Bonferroni 顯著性 | **0** | 否，無參數 |
| confidence <= `min_confidence` (60) | 4 | 是，白名單 |
| **通過** | **117 (22.3%)** | |

模型本身**不缺訊號**：22% 的樣本格有能力發訊號。

### 1b. 放寬單一門檻的效果（模型層）

| 改動 | 通過數 |
|---|---|
| 現行 30 / 0.03 / 60 | 117 |
| `min_samples` 30→10 | 151 |
| `min_edge` 0.03→0.01 | 155 |
| `min_confidence` 60→0 | 121 |
| 三個全放到最鬆 | 270 |

即使三個旋鈕全部放到最鬆，也只到 270/525——**剩下的 255 全部卡在沒有參數的穩定性檢查**。
換句話說，白名單裡那三個旋鈕只能解釋一半的 abstain。

### 1c. 今日 evidence（38 檔 × 3 horizons = 114）

| 閘門 | sector=none | sector=up | sector=down |
|---|---|---|---|
| 缺 ma30/ma60 | 3 | 3 | 3 |
| n < 30 | **0** | 3 | 27 |
| edge < 0.03 | 87 | 60 | 52 |
| 穩定性 | 8 | 14 | 16 |
| confidence <= 60 | **0** | 0 | 0 |
| **發出訊號** | **16** | 34 | 16 |
| abstain 率 | 85.6% | 69.4% | 85.6% |

放寬單一門檻對今日 evidence：

| 改動 | 方向性 / 114 | abstain |
|---|---|---|
| 現行 | 16 | 85.6% |
| `min_samples` → 10 | **16（完全沒變）** | 85.6% |
| `min_confidence` → 0 | **16（完全沒變）** | 85.6% |
| `min_edge` → 0.01 | 42 | 62.2% |
| 三個全鬆 | 51 | 54.1% |

**只有 `min_edge` 真的在咬。** `min_samples` 與 `min_confidence` 在目前的 evidence 下
完全不作用（`min_samples` 只在 sector 有明確方向時才會咬，因為那些 pattern 格較稀有）。
`min_confidence=60` 幾乎是裝飾——顯著性檢定已經先把不合格的濾掉了，Bonferroni 擋 0 個。

### 1d. sector_flow：真正咬住的是 `min_days`，不是 breadth

實際 abstain 紀錄裡有這樣一筆：

```
科技七巨頭  latest_breadth 0.714  （門檻 breadth_threshold=0.5，遠遠超過）
           up_days 1, down_days 1  →  direction "none"
```

`min_days=3` 要求連續天數，廣度再強、只有一天也不算數。sector_flow 的 abstain
是**時間序列上的持續性要求**造成的，不是強度門檻造成的。

---

## 2. abstain 率跟資料充分性有沒有關係？——幾乎沒有

這是本次分析最重要的一點。`evaluation.py:277`：

```python
effective_sample_size = max(1, min(len(evaluable), distinct_sessions // inferential_horizon))
```

**ESS 以 session 計，不以預測筆數計。** 同一天多發 30 檔股票，`distinct_sessions`
仍然是 1，ESS 仍然是 1。ledger 實測坐實：`directional_count 22`，
`distinct_signal_sessions 1`，`evaluable_direction_count 1`。

22 筆方向性預測 → ESS 最多 1。

所以「把門檻調低讓它多回答一點」買到的是**橫斷面廣度**，不是**統計檢定力**。
要脫離 `underpowered`（ESS >= 30）需要的是：

| horizon | 需要的**同版本** sessions | 約當日曆時間 |
|---|---|---|
| 1 | 30 | 約 6 週 |
| 2 | 60 | 約 3 個月 |
| 3 | 90 | 約 4.5 個月 |

而這只是脫離 warming-up。依 bug#00133 實測，要判出真實負 edge 還需要約 120 sessions。

橫斷面廣度**唯一**的貢獻是降低單一 session 內估計值的變異——邊際效益，
不改變 ESS，也不改變任何一個 gate。

---

## 3. 真正的瓶頸：Policy Version 一天重鑄四次

12 小時內 mint 了 4 個 sector_predictive 版本、3 個 sector_flow 版本：

| 版本 | 建立時間 | `neutral_move_threshold` / cost | model_hash | 預測數 | 方向性 | sessions |
|---|---|---|---|---|---|---|
| `pv_f11ca0b6` | 01:14 | **0.0 / 0.0** | 9821f5d3 | 114 | 4 | 1 |
| `pv_ead7ca4a` | 02:43 | **0.0 / 0.0** | 9821f5d3 | 114 | 4 | 1 |
| `pv_fafcf5e5` | 10:29 | 0.0025 / 10.0 | 9821f5d3 | 114 | 4 | 1 |
| `pv_d918d0c9` | 13:46 | 0.0025 / 10.0 | **fc3af8c9** | 228 | 22 | 2 |

兩個原因，都是實質問題：

**(a) 前兩版帶著 bug#00126 修正前的 `0.0 / 0.0` outcome spec。**
228 筆預測（sector_predictive 114×2 + sector_flow 6×2）綁在從未當過 Champion 的版本上，
**永久孤兒**，不可能跟任何東西合併評估。

**(b) `model_hash` 是被版本化的參數。**
13:46 的 transition 理由是「active calibration parameters changed by user confirmation」，
同時 hash 從 9821f5d3 變成 fc3af8c9（`num_tests` 660→1050，代表標的 universe 變了），
而 `model_first_date`／`model_last_date` **都沒變**。

這代表：**只要預測模型重建且內容有任何變化，就會 mint 新 Champion，樣本數歸零。**
模型是 per-session 重建的（`built_for_session: 2026-08-03`），訓練窗一旦往前推，
patterns 必然變、hash 必然變。

**結論：在目前設計下，sector_predictive 的 ESS 結構性地被壓在 1 附近，
不管跑多久都到不了 30。** 這不是資料不足，是識別碼設計讓資料無法累積。

---

## 4. 所以要動什麼

按影響大小排序。前兩項不解決，調門檻沒有意義。

1. **把 `model_hash` 從版本身分裡拿掉（或改成不影響身分的紀錄欄位）。**
   模型內容當然要可稽核，但它應該像 `risk_free_rate` 那樣寫進 `observed_regime`
   與 evidence hash，而不是進 `parameters`。否則 Champion 活不過一次模型重建。
   注意：這會改變 Policy Version 的語意（同一個 version id 對應不同模型內容），
   需要明確決定「什麼算同一個策略」——屬 C 層結構決策，人工判斷。
2. **清掉／隔離 `0.0 / 0.0` outcome spec 的孤兒版本**，並加一條測試確保
   任何 adapter 都不可能用零死區、零成本建立 Policy Version。
3. **把 abstain 原因記下來。** 現在六道閘門共用一個
   `prediction_thresholds_not_met`，導致「門檻是否過高」這個問題**在 ledger 裡無法回答**——
   本次分析必須離線重跑模型才拿得到上面那些數字。改成記錄實際擋住的那一道
   （以及當時的 n / edge / confidence 值），成本極低，之後所有調參討論都靠它。
4. **`min_edge` 是唯一在咬的旋鈕**，但 v1 規則表只准 `INCREASE`（0.03 → 0.15），
   而 0.03 已經是下界。也就是說**系統只能讓 abstain 更多，永遠不能更少**。
   若要讓迴路有能力回應「回答太少」，需要一個
   `LOW_COVERAGE`／`INSUFFICIENT_SIGNAL` 診斷 + 允許放寬的規則——但這會直接衝撞
   「tighten-only」原則，屬需要人決定的方向性變更。
5. **穩定性檢查是最大的無參數過濾器**（門檻全鬆時擋 255/525，佔 49%）。
   它目前既不可調、也不被記錄、也不在任何文件裡。至少要讓它可見。

---

## 附：本次分析用的腳本

- `/tmp/gate_attrition.py`：模型層 525 格逐閘門統計 + 門檻掃描
- `/tmp/symbol_attrition.py`：今日 evidence 38 檔 × 3 horizons 逐閘門統計

兩支都只讀資料，不寫任何東西。建議收進 `scripts/` 並在 T2 的 oracle 測試裡重用。
