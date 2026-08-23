# FeedbackCycleOrchestrator 設計（P0-C）— 待審查

狀態：**設計草案，尚未實作**。日期：2026-08-03（§2.5 為同日覆核補記）。

實作進度：**O-1 完成**（bug#00130）。`FeedbackCycleOrchestrator.advance()` 已存在並由
背景 cycle 呼叫；checkpoint 表（migration v12）、health 評估與持久化、`diagnose_history`
皆已接線，斷了一直沒發現的 health 回饋（`run_policy_cycle` 的 `health` 參數永遠是 None）
已修復。

仍為生產零呼叫：`generate_candidates`、`candidate_mutation_rules`（bug#00128 的規則表）、
`capture_champion_and_shadow`、`evaluate_shadow`、`review_promotion`、
`build_point_in_time_dataset`、`plan_purged_walk_forward`、`run_replay_comparison`
——即 O-2 至 O-5。

決策補登：**D-11 特徵窗口（`window_days` / `lookback`）不納入自動調整**（使用者
2026-08-03 確認；計劃書 §10.3 的白名單表需同步修正）。**D-12 legacy 校準改唯讀**
（bug#00129）。

目的：把「診斷 → 候選 → replay → shadow → 提案」串成一次冪等、可中斷續跑的 cycle。

前提：**TUI 沒有整合大型語言模型**。所有調整必須是規則式、確定性、離線、可重現的：
同樣的 ledger 狀態必須得到同樣的候選、同樣的 replay 結果、同樣的提案。任何「看情況判斷」
的環節都必須寫成參數與門檻，否則它就不可稽核，也不可能被回測驗證。

---

## 1. 可調整變量完整清冊

以下是目前程式中**實際存在**的每一個變量，依「誰有權改它」分層。這張表就是自動化的邊界：
不在 A 層的東西，orchestrator 一律不得碰。

### A 層：可自動產生候選（whitelist，單步、需人工核准才套用）

| Family | 參數 | 現值 | 意義 |
|---|---|---|---|
| etf_consensus | `consensus_threshold` | 0.5 | 多少比例的 ETF 同向才算共識 |
| etf_consensus | `flat_threshold_pp` | 0.5 | 權重變動多少 pp 以下視為持平 |
| etf_consensus | `minimum_etfs` | 4 | 最少幾檔 ETF 有評估才敢出方向 |
| etf_selection_tilt | `tilt_min_net` | 0.1 | 淨傾斜多少才算有 stance |
| etf_selection_tilt | `stance_breadth_min` | 0.2 | 廣度下限 |
| options | `skew_call_hi` | 70.0 | 新增 OI 集中買權的上門檻 |
| options | `skew_call_lo` | 30.0 | 集中賣權的下門檻 |
| options | `bias_min_abs` | 0.15 | 重定價殘差偏向的絕對門檻（每股美元） |
| sector_flow | `breadth_threshold` | 0.5 | 類股廣度門檻 |
| sector_flow | `capw_threshold` | 0.1 | 市值加權報酬門檻 |
| sector_flow | `min_days` | 3 | 持續幾日才算成立 |
| sector_predictive | `min_samples` | 30 | 條件機率最少樣本 |
| sector_predictive | `min_edge` | 0.03 | 相對基準的最小 edge |
| sector_predictive | `min_confidence` | 60.0 | 信心門檻 |
| cross_model | `neutral_band` | 0.15 | 合成分數的中性帶 |

### B 層：Outcome Spec — **永遠不可自動調**

`neutral_move_threshold` (0.0025)、`round_trip_cost_bps` (10.0)、`benchmark_target_kind` /
`benchmark_id`、`price_field`。

理由：這四項定義「什麼叫預測對了」。讓系統自己調它們，等於讓被考的人改考卷。任何改動都
會產生新 Policy Version，且舊紀錄無法用新規則重評，所以只能由你在資料累積前決定（D-09）。

### C 層：結構性參數 — **只能人工開發成新版本**

| Family | 參數 | 現值 | 為何不能自動 |
|---|---|---|---|
| 四個 forward family | `horizons` | `[1, 5, 10]` | D-10 固定階梯。讓系統挑 horizon 就是 §8.1 禁止的 horizon shopping |
| sector_flow | `horizon_sessions` | 5 | 同上 |
| sector_predictive | `model_hash` / `model_first_date` / `model_last_date` | 每日重建 | 模型指紋，不是門檻 |
| options | `history_mode` | `forward_only` | D-08，要改需先有可信的 point-in-time surface |
| cross_model | `outcome_target` | `QQQ` | D-01 |
| cross_model | `etf_min_etfs_evaluated` | 4 | **衍生值**，必須等於 ETF family 的 `minimum_etfs`，不可獨立變動 |

### D 層：特徵窗口 — **不納入自動調整（D-11，已確認）**

`etf_consensus.window_days` (14)、`etf_selection_tilt.window_days` (14)、
`options.window_days` (14)、`sector_flow.lookback` (5)。

計劃書 §10.3 的白名單表原本寫了 ETF 可自動調「window」，程式的
`_CANDIDATE_PARAMETER_WHITELISTS` 沒有納入。**以程式為準**：window 同時改變「多少資料
進入判斷」與「多久算一個訊號」，單步調動的效果不單調，而且它與 replay 的 purge 長度
耦合，一起變動會讓配對比較失去意義。要改仍可，但走人工開發新 Policy Version。

### E 層：Protocol 參數 — 屬於「評估方法」而非「投資判斷」，改動要新 protocol version

| Protocol | 參數 | 現值 |
|---|---|---|
| `HealthProtocol` | `minimum_effective_blocks` / `warning_miss_streak` / `recovery_evaluations` | 30 / 3 / 2 |
| `ClusteredValidationProtocol` | `confidence_level` / `bootstrap_iterations` / `randomization_iterations` / `random_seed` / `minimum_effective_blocks` | 0.95 / 2000 / 2000 / 1729 / 30 |
| `PurgedWalkForwardProtocol` | `minimum_training_sessions` / `validation_sessions` / `step_sessions` / `purge_sessions` / `embargo_sessions` / `minimum_folds` | 60 / 20 / 20 / **0** / **0** / 2 |
| `PromotionGateThresholds` | D-06 的 0.0200 / 0.0020，加 shadow 樣本數、覆蓋率、風險非劣性 | — |
| `CandidateMutationBudget` | `max_candidates_per_cycle` | 3 |

> ⚠ **`purge_sessions` 與 `embargo_sessions` 的預設值是 0。** 對 10-session 前瞻期而言，
> purge 必須至少等於該 slice 的 horizon，否則 training 的標籤會跨進 validation，
> 整個 replay 就有前視偏誤。orchestrator **必須**在建 protocol 時把 purge 設為
> `max(該 dataset 的 horizon)`，不能沿用預設。這是實作時最容易漏掉的一點。

### F 層：Runtime 參數 — 不屬於 Policy 身分，可直接改

`SETTLEMENT_GRACE_SESSIONS` (5)、`ANALYSIS_CACHE_RETENTION_DAYS` /
`BENCHMARK_TRUTH_RETENTION_DAYS` (730)、cycle lease (15 分鐘)、
`MIN_NEW_MATURED_TO_EVALUATE`（新增，見 §3）。

---

## 2. 診斷 → 動作對照表（以及四個真正的缺口）

`FailureClass` 目前有 9 種。下表是每一種**現在能做什麼**：

| FailureClass | 應有的回饋 | 現在能不能做 | 缺什麼 |
|---|---|---|---|
| `data_quality` | VOID、告警、暫停該 adapter，**不准調參** | ✅ `generate_candidates` 已短路 | — |
| `underpowered` | 維持 warming-up，不下結論 | ✅ 已短路 | — |
| `no_incremental_edge` | 收緊門檻或降權 | ✅ A 層參數往「緊」的方向單步 | — |
| `negative_edge` | degraded Warning Mode + 收緊 | ✅ 同上 | — |
| `direction_asymmetry` | **只讓壞方向 abstain** | ❌ | adapter 沒有任何 per-direction 參數 |
| `horizon_mismatch` | **只限制失效的 horizon 出方向** | ❌ | 沒有「哪些 horizon 可出方向」的參數 |
| `miscalibration` | 機率收縮 / Platt / isotonic | ❌ | 沒有機率校正參數（只有 sector_predictive 產生機率） |
| `regime_drift` | 特定 regime abstain / 降權 | ❌ | 沒有 regime 參數，evidence 也沒切 regime slice |
| `strategy_mapping` | 停止策略建議 | N/A | 目前不輸出可成交策略 P&L |

**這是最重要的發現：九種失效裡，只有兩種是系統現在真的能回應的。** 其餘四種診斷得出來、
但沒有任何旋鈕可以轉——診斷會寫進 ledger 然後停在那裡。

要補齊，需要新增四組 A 層參數（每一組都是新的 Policy Version，需你核准後才進 whitelist）：

| 提議新參數 | 適用 | 型別 | 回應哪個失效 |
|---|---|---|---|
| `directional_horizons` | 四個 forward family | horizons 的子集合 | `horizon_mismatch`。**不在子集內的 horizon 仍照常記錄為 abstain**，coverage 才不會被灌水，只是不出方向 |
| `direction_gate` | 全部 | `{"up": bool, "down": bool}` | `direction_asymmetry`。壞方向直接 abstain，好方向不受影響 |
| `probability_shrinkage` | sector_predictive | 0.0–1.0 | `miscalibration`。把 P(up) 往當時凍結的 base rate 收縮，不動方向 |
| `regime_gate` | 全部 | 停用的 regime 標籤集合 | `regime_drift`。**前置條件**：`EvaluationSlice` 目前只切 `horizon × direction`，要先加 regime 維度 |

前兩者是布林/集合而非數值，`generate_candidates` 現在只支援「數值單步」（`MutationRule`
有 `step` / `minimum` / `maximum`）。需要擴充成支援「離散開關」型的 mutation，且同樣受
每輪 3 個的預算限制。

---

## 2.5 二次清點的補充發現（2026-08-03 覆核）

初版 §1／§2 漏了三項，補記如下。

**(a) `diagnose_history` 也是生產零呼叫，而它是 `REGIME_DRIFT` 的唯一來源。**
`diagnose()` 只看單一 EvidencePack，判不出「上一次還是正 edge、這一次翻負」。
`diagnose_history()` 吃連續兩份同版本的 EvidencePack 才能判。orchestrator 必須兩個都呼叫。

**(b) 兩個 ETF family 對同一份 report 用不同門檻。**
`ETFConsensusPolicyAdapter` 與 `ETFSelectionTiltPolicyAdapter` 都呼叫
`analysis.compute_symbol_trends`，但 tilt **不傳** `consensus_threshold` / `flat_threshold_pp`，
永遠吃函式預設 0.5 / 0.5。所以只要 consensus 被調到（例如）0.65，同一檔股票在
consensus 眼中是「無共識」、在 tilt 眼中仍是「up」，兩個 family 對「up」的定義就分岔了。
tilt 的 Policy Version 也沒有記錄這兩個值，等於它的判斷依賴一個它自己不宣告的門檻。
→ 建議把 `consensus_threshold` / `flat_threshold_pp` 也納入 tilt 的 parameters
（值由 ETF family 共用，如同 cross_model 的 `etf_min_etfs_evaluated`），
否則自動調參會讓兩個 family 的分歧隨每次 promotion 擴大。

**(c) 四個 silent default——不是版本身分漏洞，但不可調。**

| 函式 | 參數 | 預設 | 誰沒傳 |
|---|---|---|---|
| `compute_symbol_trends` | `endpoint_k` | 3 | ETF consensus、ETF tilt |
| `compute_symbol_trends` | `reported_share_signal` | False | 同上 |
| `compute_symbol_trends` | `rel_share_threshold` | 0.05 | 同上 |
| `compute_directional_verdicts` | `bias_min_n` | 2 | options |

已驗證這**不是**版本身分漏洞：`_adapter_code_hash` 用 `inspect.getsource`，
函式簽章連同預設值都在雜湊範圍內，改預設值一定產生新的 code_hash。
但它們**不在 `parameters` 裡，因此不可調**，回饋迴路永遠碰不到。
`bias_min_n=2`（殘差偏向至少要幾張合約參與）尤其值得日後納入——
樣本太少的偏向本來就該不算數，那正是 `NEGATIVE_EDGE` 想收緊的東西。

---

## 3. Orchestrator 架構

### 3.1 對外介面

```python
class FeedbackCycleOrchestrator:
    def advance(self, user_id: str, as_of: date) -> FeedbackCycleReport: ...
```

只有一個方法。內部隱藏所有排程、狀態機與統計。TUI 只需在背景 cycle 尾端呼叫一次。

### 3.2 一次 advance() 的步驟

```
acquire lease (scope="feedback-cycle", per user)          ← 與各 family 的 capture lease 分開
  read checkpoints
  for family in six_families:
      ① gate      新成熟 outcome 數 - last_outcome_evaluated < MIN_NEW_MATURED → skip
      ② evidence  取該 family 最新的 EvidencePack + FailureDiagnostic（capture cycle 已產生）
                    · **另需呼叫 `diagnose_history(evidence_history)`**——它吃的是
                      EvaluationLedger 裡同一 policy version 的**連續兩份** EvidencePack，
                      而 `REGIME_DRIFT` 只由它產生。capture cycle 只呼叫 `diagnose()`，
                      所以少了這一步就永遠偵測不到「edge 由正翻負」
      ③ health    health_signal_from_evidence → assess_health → 持久化
      ④ candidate generate_candidates(champion, diagnostic, RULES[family], budget)
                    · cooldown：同一參數 N 個 session 內不得再動
                    · dedupe：candidate_id 已存在 → 略過
      ⑤ replay    dataset ← ledger 的 forecast+outcome 歷史
                    plan_purged_walk_forward(purge = max(horizon), embargo = 1)
                    run_replay_comparison(champion, candidate)
                    · 未過 negative control 或折數不足 → 記錄結果，該候選終止
      ⑥ shadow    通過 replay 的候選註冊為 Challenger，並標記為「下一次 capture 要同步發預測」
      ⑦ gate2     shadow 成熟樣本足夠 → evaluate_shadow
      ⑧ propose   review_promotion(replay, shadow, thresholds) 全過 → 建立 pending Proposal
      write checkpoint(family)
release lease
```

**③ health 的回饋路徑**：`assess_health` 的結果必須存下來，讓**下一次** capture 時
`run_policy_cycle(health=...)` 讀到。目前那個參數永遠是 `None`，這條線是斷的。
不能在同一次 cycle 內即時套用，因為當次的預測已經發出去了。

**⑥ shadow 的架構約束（重要）**：`capture_champion_and_shadow` 必須在**同一個 session、
同一份 evidence** 下同時發出 Champion 與 Challenger 的預測，否則兩者不可配對比較。
這代表 shadow **不能**是 orchestrator 事後補做的一步——各 family 的
`run_*_experiment_cycle` 必須接受一個 `challengers` 參數，由 orchestrator 在前一輪決定、
存進 checkpoint，下一輪 capture 時一併發出。這會改動 §上一節四個 cycle 函式的簽章。

### 3.3 觸發條件（§11.4：不用日曆）

| Checkpoint | 意義 | 觸發門檻 |
|---|---|---|
| `last_outcome_evaluated` | 上次評估時的成熟樣本水位 | 新增成熟 outcome ≥ `MIN_NEW_MATURED`（建議 10） |
| `last_drift_check` | 上次 drift 檢查 | 每次 advance |
| `last_challenger_trained` | 上次產生候選 | 距今 ≥ cooldown 且有可行動的診斷 |
| `last_shadow_started` | 上次啟動 shadow | 有通過 replay 的候選 |
| `last_model_promoted` | 上次升級 | 由人工核准寫入 |
| `parameter_cooldown[param]` | 每個參數上次被動的 session | 同一參數 ≥ `PARAM_COOLDOWN_SESSIONS`（建議 20）內不得再動 |

用「新樣本數」而非日曆的理由：日曆觸發等於拿同一批資料反覆重抽，直到某次僥倖過關——
這正是 §8.7 要用 sequential testing / cooldown 防的事。

### 3.4 冪等與中斷續跑

- 每個產物都有內容決定的穩定 ID（`candidate_id`、`evaluation_id`、`proposal_id`），
  同樣狀態重跑得到同樣 ID → 寫入是 upsert-or-skip，不會產生重複。
- 每一步各自 commit 自己的不可變紀錄；中途失敗不回滾已完成的步驟，下次從 checkpoint 續跑。
- 整個 advance 包在一個 per-user lease 裡（沿用 `ExperimentCycleLock`），
  scope 與 capture cycle 分開，避免長時間的 replay 卡住每日抓取。
- Replay 是 cycle 裡最貴的一步（2000 次 bootstrap × 2000 次 sign-flip）。
  應設 `MAX_REPLAYS_PER_CYCLE`（建議 1），排隊處理，避免單次背景刷新拖太久。

### 3.5 不變式（要寫成測試鎖住）

1. `data_quality` / `underpowered` 永遠不產生候選。
2. 一個候選只改一個參數、一步、在 `[minimum, maximum]` 內。
3. 每個 family 每輪最多 3 個候選。
4. 候選必須能被 `instantiate_policy_version` 精確重建，否則拒絕。
5. Challenger 永遠不會因為 Champion degraded 而自動接手。
6. Promotion 一律需要人工核准；orchestrator 只能建立 pending Proposal。
7. Replay 的 `purge_sessions` ≥ 該 dataset 的最大 horizon。
8. 同一參數在 cooldown 內不得來回。
9. 純雜訊資料跑完整個 cycle 不得產生任何 Proposal（false-positive 測試）。
10. 中斷後重跑不新增重複的 candidate / evaluation / proposal。

---

## 4. 雙軌調參衝突（必須先解決）

現在有**兩套**東西會動同一批旋鈕：

| | Legacy `calibration_schedule`（按鍵 `k`） | 新 `generate_candidates` |
|---|---|---|
| 參數來源 | `data/{user}_calibration.json` 的 `active_params` | Policy Version 的 `parameters` |
| 涵蓋 | `etf.consensus_threshold`、`etf.min_etfs_evaluated`、`sector.breadth_threshold`、`sector.min_days`、`options.bias_min_pct` | A 層 15 個參數 |
| 證據 | legacy `backtest_*` 報告 + `model_health` | Experiment ledger 的 EvidencePack |
| 驗證 | 顯著性把關，**沒有 replay / shadow** | replay + negative control + shadow + Gate |
| 套用 | 使用者在 modal 按確認 | 使用者核准 Promotion Proposal |

三個名稱直接重疊：`consensus_threshold`、`breadth_threshold`、`min_days`。
（`options.bias_min_pct` 與 adapter 的 `bias_min_abs` 是**不同**參數，不衝突。）

重疊的後果：兩個控制器用不同證據、不同節奏推同一個旋鈕，cooldown 互相看不見，可能造成
來回震盪，而且 ledger 會不斷因為 legacy 那邊改參數而觸發 `ChampionTransition`、切換版本，
讓每個版本的樣本都湊不滿。

**建議**：legacy 校準改為唯讀顯示（保留歷史與畫面，停止寫入 `active_params`），
調參權責單一化到 Experiment 這條線。這符合計劃書 §14.4 的既定方向。
但這會改變你現在熟悉的 `k` 鍵行為，所以列為待決 2。

---

## 5. 分階段實作建議

| 階段 | 內容 | 前置 |
|---|---|---|
| O-1 | checkpoint schema + `advance()` 骨架 + health 回饋接線（③）＋不變式測試 | 無 |
| O-2 | `MutationRule` 規則表（見待決 3）+ 候選產生 + cooldown | O-1 |
| O-3 | Replay 資料集 builder（從 ledger 建）+ purge=max(horizon) + 候選比較 | O-2 |
| O-4 | Shadow：四個 cycle 函式加 `challengers` 參數 + 配對捕捉 + `evaluate_shadow` | O-3 |
| O-5 | Promotion Gate + pending Proposal + Test Mode 顯示 | O-4 |
| O-6 | 補齊四個缺口參數（§2 下半），讓其餘失效類型也有旋鈕可轉 | O-5 |

O-1 到 O-5 是把現有零件串起來；O-6 才是新增能力。

---

## 6. 待你決策

1. **特徵窗口要不要納入自動調整？** `window_days` / `lookback`（D 層）。計劃書 §10.3 說要，
   程式的 whitelist 沒有。我的建議是**不納入**：window 同時改變「多少資料進判斷」與
   「多久算一個訊號」，單步調動的效果不單調，而且它與 replay 的 purge 長度耦合，
   一起變動會讓比較失去意義。但這是你的決定。

2. **legacy 校準（`k` 鍵）是否改為唯讀？** 見 §4。不改的話兩套控制器會搶同一個旋鈕。

3. **`MutationRule` 規則表的實際數值。** 這張表目前**完全不存在**——`generate_candidates`
   要求呼叫端傳 `rules`，而生產程式沒有任何地方定義過。每個 A 層參數都需要
   `step / minimum / maximum / direction / 對應的 failure_classes` 五個值。
   我可以先依 legacy `PARAM_SPEC` 的既有級距擬一份草案給你改，那份級距是你之前用過的
   （例如 `consensus_threshold` step 0.05、範圍 0.5–0.8）。

4. **`MIN_NEW_MATURED` / `PARAM_COOLDOWN_SESSIONS` / `MAX_REPLAYS_PER_CYCLE`**
   建議 10 / 20 / 1。這三個屬 F 層，之後可改，不影響 Policy 身分。

5. **§2 的四個缺口參數要不要做（O-6）？** 不做的話，`direction_asymmetry`、
   `horizon_mismatch`、`miscalibration`、`regime_drift` 四種診斷永遠只能看、不能修。

6. **ETF tilt 要不要與 consensus 共用 `consensus_threshold` / `flat_threshold_pp`？**
   見 §2.5(b)。不共用的話，自動調參會讓兩個 family 對「up」的定義隨每次 promotion 越差越遠。

7. **`bias_min_n` 要不要納入 options 的 parameters 與 whitelist？** 見 §2.5(c)。
