# Candidate MutationRule 規則表 — 草案（待審查）

狀態：**草案，尚未寫進程式**。日期：2026-08-03。對應 orchestrator 設計文件的待決 3。

`generate_candidates(parent, diagnostic, rules, budget)` 要求呼叫端提供 `rules`，
而生產程式目前一條都沒有。這份文件把 15 個 whitelist 參數各補上
`step / minimum / maximum / direction / failure_classes` 五個值。

---

## 0. 動工前先看：options 的 `bias_min_pct` 分歧

期權方向門檻的實際算式是（`options_analysis.py:1618`）：

```
thr = max(bias_min_abs, bias_min_pct / 100 × spot)
```

- **期權畫面**透過 `_options_verdict_params(user)` 傳入使用者校準後的 `bias_min_pct`。
- **`OptionsForecastPolicyAdapter` 完全沒有這個參數**，只傳 `bias_min_abs`，
  所以 ledger 永遠用函式預設 `0.03`。

這跟我剛修掉的跨模型 `etf_min_etfs_evaluated` 是同一類缺陷：只要你把 `bias_min_pct`
從 0.03 調開，畫面與 ledger 就會用不同門檻，對高價標的可能得到不同方向。

拿你本機真實現價算，`bias_min_pct=0.03` 時哪一項會壓過另一項：

| 標的 | 現價 | `0.03% × spot` | 生效門檻 | 誰決定 |
|---|---|---|---|---|
| NVDA | 200.75 | 0.060 | 0.150 | `bias_min_abs` |
| AAPL | 308.91 | 0.093 | 0.150 | `bias_min_abs` |
| MU | 823.03 | **0.247** | **0.247** | `bias_min_pct` |

**這直接影響規則表**：如果只把 `bias_min_abs` 列為可調參數，那麼對 MU 這種高價標的，
把它從 0.15 一路調到 0.24 都不會有任何效果——規則看起來在動，實際上完全沒作用。

**建議**：把 `bias_min_pct` 加進 options adapter 的 parameters 與 whitelist，
並讓**它**成為主要旋鈕（也順便修好畫面/ledger 分歧）；`bias_min_abs` 保留為低價標的的地板，
列為次要規則。下表按此假設撰寫，標記為「提議新增」。

---

## 1. 三個設計原則

**① v1 只收緊，不放寬。** 每條規則都是單向的（`MutationRule` 本來就只有一個
`direction`）。放寬門檻是把雜訊當訊號最快的路徑，而且 §10.2 列出的所有回饋都是
「收緊 / abstain / 降權」，沒有任何一條是放寬。要放寬，走人工開發新版本。

**② 棘輪由 Promotion Gate 擋，不是由規則擋。** 只收緊會讓參數逐輪往 max 漂。
四道煞車：(a) 只有在**有** `negative_edge` / `no_incremental_edge` 診斷時才產生候選，
健康的 policy 不會有候選；(b) 每個候選都要在 replay 的配對比較中，信賴區間下界越過
D-06 的最低實用改善幅度；(c) Gate 有 `minimum_coverage_ratio`，把覆蓋率打崩的收緊過不了；
(d) 每個參數 20 個 session 的 cooldown。

**③ 規則的排列順序就是優先序。** `generate_candidates` 依序掃 rules，湊滿 3 個就停。
所以每個 family 的第一條應該是「最直接決定要不要出方向」的那個旋鈕。

---

## 2. 規則表

`FC` 欄：`NIE` = `no_incremental_edge`，`NEG` = `negative_edge`。

### etf_consensus

| # | 參數 | step | min | max | 方向 | FC | 理由 |
|---|---|---|---|---|---|---|---|
| 1 | `consensus_threshold` | 0.05 | 0.50 | 0.80 | ↑ | NIE, NEG | 沿用 legacy `PARAM_SPEC` 級距。0.80 = 要 80% ETF 同向，再高幾乎不可能成立 |
| 2 | `minimum_etfs` | 1 | 3 | 8 | ↑ | NIE, NEG | 沿用 legacy。⚠ 只有 32 檔主動 ETF，單一持股很少被 8 檔以上同時持有，max 再高會把覆蓋率打到 0 |
| 3 | `flat_threshold_pp` | 0.25 | 0.50 | 2.00 | ↑ | NIE | ⚠ bug#00123 已證實揭露權重長期完全不變，調高會讓幾乎所有持股變 flat。step 取小、max 保守，且**只回應 NIE**（這是輸入端雜訊過濾，不是方向門檻） |

### etf_selection_tilt

| # | 參數 | step | min | max | 方向 | FC | 理由 |
|---|---|---|---|---|---|---|---|
| 1 | `tilt_min_net` | 0.05 | 0.10 | 0.50 | ↑ | NIE, NEG | `net_score ∈ [-1, 1]`，0.5 已是很強的單邊傾斜 |
| 2 | `stance_breadth_min` | 0.05 | 0.20 | 0.60 | ↑ | NIE, NEG | 跨基金廣度，同上尺度 |

### options

| # | 參數 | step | min | max | 方向 | FC | 理由 |
|---|---|---|---|---|---|---|---|
| 1 | `bias_min_pct` **（提議新增）** | 0.02 | 0.03 | 0.15 | ↑ | NIE, NEG | 完全沿用 legacy `PARAM_SPEC`。這是唯一會隨現價伸縮的門檻，高價標的由它決定 |
| 2 | `skew_call_hi` | 5.0 | 55.0 | 90.0 | ↑ | NIE, NEG | `call_pct >= hi` → 看多。收緊 = 要求更極端的買權集中 |
| 3 | `skew_call_lo` | 5.0 | 10.0 | 45.0 | **↓** | NIE, NEG | ⚠ **這條是反向的**。`call_pct <= lo` → 看空，所以收緊是**降低**它 |
| 4 | `bias_min_abs` | 0.10 | 0.15 | 1.00 | ↑ | NEG | 只在低價標的生效（見 §0），列為次要。預算 3 個，通常輪不到它 |

> `skew_call_hi` / `skew_call_lo` 是對稱的一對，但每個候選只准改一個參數，
> 所以它們會變成兩個各自進 replay 的候選，由樣本決定哪一側才是問題。這是刻意的。

### sector_flow

| # | 參數 | step | min | max | 方向 | FC | 理由 |
|---|---|---|---|---|---|---|---|
| 1 | `breadth_threshold` | 0.05 | 0.50 | 0.80 | ↑ | NIE, NEG | 沿用 legacy |
| 2 | `capw_threshold` | 0.05 | 0.10 | 0.50 | ↑ | NIE, NEG | 市值加權日報酬（%），0.5% 已是明確的單日方向 |
| 3 | `min_days` | 1 | 3 | **5** | ↑ | NIE, NEG | ⚠ **max 必須 ≤ `lookback`（現為 5）**。`ready = evaluated >= min_days` 而 `evaluated ≤ lookback`，超過就永遠 `ready=False`、整個 family 靜默失效。legacy max 同為 5 |

### sector_predictive

| # | 參數 | step | min | max | 方向 | FC | 理由 |
|---|---|---|---|---|---|---|---|
| 1 | `min_confidence` | 5.0 | 60.0 | 85.0 | ↑ | NIE, NEG | ⚠ min 是 60.0 而非更低：bug#00125 已把 `meets_threshold` 改成嚴格 `> 60.0`，且 n<5 時信心上限恰為 60.0。降到 60 以下等於重新打開那個洞 |
| 2 | `min_edge` | 0.02 | 0.03 | 0.15 | ↑ | NIE, NEG | 相對基準的最小 edge |
| 3 | `min_samples` | 10 | 30 | 100 | ↑ | NEG | 只回應 NEG——這是證據充分性而不是方向門檻。NIE（有命中但沒超越基準）不該用「要更多樣本」來回應 |

### cross_model

| # | 參數 | step | min | max | 方向 | FC | 理由 |
|---|---|---|---|---|---|---|---|
| 1 | `neutral_band` | 0.05 | 0.15 | 0.50 | ↑ | NIE, NEG | 合成分數的中性帶。adapter 已驗證必須 `0 ≤ band < 1` |

---

## 3. 到頂之後怎麼辦

參數已在 `maximum`、診斷卻仍然是 `negative_edge` 時，`generate_candidates` 會因為
`next_value == current` 而跳過，回傳空集合——**這會看起來像「系統沒事做」，
但實際上是「系統已無計可施」**，兩者意義完全不同。

建議 orchestrator 明確區分並記錄三種結果：

| 情況 | 記錄 |
|---|---|
| 沒有診斷 | `no_action_needed` |
| 有診斷，但該 FailureClass 沒有任何規則對應（見設計文件 §2 的四個缺口） | `no_available_action`＋缺哪一類旋鈕 |
| 有規則，但參數已到 max | `exhausted`＋**建議使用者考慮暫停該 family**（人工決定，系統不自動停） |

第三種是重要訊號：一個 family 把所有門檻都收到頂還是負 edge，代表問題不在門檻，
在方法本身。這時候該做的是人工重新設計，而不是繼續轉旋鈕。

---

## 4. 程式形式（核准後放進 `feedback.py`）

```python
CANDIDATE_RULES_VERSION = "candidate-rules-v1"

_CANDIDATE_MUTATION_RULES: Mapping[PolicyFamily, tuple[MutationRule, ...]] = {
    PolicyFamily.ETF_CONSENSUS: (
        MutationRule("consensus_threshold", 0.05, 0.50, 0.80,
                     MutationDirection.INCREASE, (_NIE, _NEG)),
        MutationRule("minimum_etfs", 1, 3, 8,
                     MutationDirection.INCREASE, (_NIE, _NEG)),
        MutationRule("flat_threshold_pp", 0.25, 0.50, 2.00,
                     MutationDirection.INCREASE, (_NIE,)),
    ),
    ...
}


def candidate_mutation_rules(family: PolicyFamily) -> tuple[MutationRule, ...]:
    """Ordered v1 rules; position is priority because the budget truncates."""
    return _CANDIDATE_MUTATION_RULES.get(family, ())
```

需要一起加的測試：

1. 每條規則的 `parameter` 都在該 family 的 whitelist 內（兩張表不得漂移）。
2. 每條規則的 `minimum` 等於該 family adapter 的現行預設值（起點不可低於出廠值）。
3. `sector_flow.min_days` 的 `maximum` ≤ adapter 的 `lookback`。
4. 每條規則從 `minimum` 連續套用到 `maximum` 的每一步，都能建出合法的 adapter
   （不會踩到 adapter 自己的 `raise ValueError`）。
5. 每個 family 的規則數 ≥ 1，且 `no_incremental_edge` 與 `negative_edge` 至少各有一條可用。
6. 沒有任何規則掛在 `data_quality` 或 `underpowered` 上。

---

## 5. 待你確認

1. **`bias_min_pct` 要不要加進 options adapter？**（§0）不加的話，options 的規則對高價
   標的實質無效，而且畫面/ledger 分歧會留著。
2. **上表 15（+1）條的 step / min / max 數值**。凡是 legacy `PARAM_SPEC` 有的
   （`consensus_threshold`、`min_etfs_evaluated`、`breadth_threshold`、`min_days`、
   `bias_min_pct`）我都原樣沿用你之前用過的級距；其餘是我依各參數的實際值域擬的，
   請直接改。
3. **v1 只收緊、不放寬**是否同意（§1 原則 ①）。
4. **`exhausted` 時只提示、不自動暫停**是否同意（§3）。
