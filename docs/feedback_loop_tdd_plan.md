# 回饋迴路 TDD 計畫

狀態：草案，2026-08-03。起因是檢視 `scripts/verify_feedback_loop.py` 的驗證方式時，
發現 bug#00133 的四處改動**沒有任何一條測試會因為改回舊行為而失敗**。

---

## 一、驗證資料現在是怎麼運作的

`scripts/verify_feedback_loop.py` 建一個合成的 cross-model family，
只有「policy 的判斷式」是假的，其餘全部走生產程式碼：真的 `PolicyVersion`、
真的白名單旋鈕 `neutral_band`、真的 `engine.capture/advance`、真的 `evaluate/diagnose`、
真的 `FeedbackCycleOrchestrator`、真的 replay／shadow／Gate。

### 世界的真值是刻意種的

`SeededFamily._return_for(score)`，`random.Random(20260803)` 固定種子：

| mode | 報酬規則 | 這個世界的「正確答案」 |
|---|---|---|
| `edge` | `noise ± 0.4%`，方向永遠跟 score 同號 | Champion 是好的，迴路**不該動它** |
| `noise` | 純 `gauss(0, 1%)`，score 完全無用 | 沒有東西可修 |
| `weak-signal`（預設） | `\|score\| ≥ 0.70` → **+0.8% 同向**；`\|score\| < 0.70` → **−1.2% 反向** | 收緊 band 是**真的修好**，不是為收緊而收緊 |

`weak-signal` 是整個設計的關鍵：Champion 的 `neutral_band=0.15` 會對 0.15–0.70 這段
**被刻意下毒的弱訊號**下注，整體必為負 edge；而把 band 往 0.70 推是有真實報酬依據的修正。
因此「迴路應該產出什麼」是**事前可知的**，不是事後看數字合理化。

其餘設計要點：

- **一條連續複利價格路徑**。horizon=1 時到期日就是下一個 signal 日，若每輪寫新的進場價，
  會覆蓋前一筆的出場價，所有 outcome 結算成 0 報酬（曾經踩過）。
- 基準 SPY 走 `gauss(0, 0.3%)` 獨立隨機，讓 benchmark-adjusted 超額有意義。
- 真值走 `MappingSessionTruthSource`，settle 由 `engine.advance()` 正常觸發。
- 400 sessions，多出一個 session 讓最後一筆有地方結算。

### 目前三種模式的實跑結果

| mode | 命中率 | diagnoses | health | outcome | 耗時 |
|---|---|---|---|---|---|
| `edge` | 57.4% | none | healthy | `no_action_needed` | 7.3s |
| `noise` | 41.7% | `no_incremental_edge` ×2 | **healthy** | **`candidates_proposed`** | 14.8s |
| `weak-signal` | 29.4% | `negative_edge` ×2 | degraded | `candidates_proposed` → Gate 拒絕 | 14.4s |

---

## 二、問題：它不是測試，是列印機

`main()` **沒有任何 assert，永遠 `return 0`**。三種模式的預期結果只寫在 docstring 散文裡。
後果有三：

### (1) bug#00133 的四處改動全部沒有回歸保護

實測突變（改壞 → 跑全套 231）：

| 改動處 | 突變 | 結果 |
|---|---|---|
| `evaluation._edge_validation` | 拿掉 economic fallback | **231 passed**（存活） |
| `replay._decision_utility` | ABSTAIN 改回 `None` | **231 passed**（存活） |
| `feedback.evaluate_shadow` | ABSTAIN 改回 `continue` | **231 passed**（存活） |
| `ReplayComparisonReport.*_coverage` | — | 測試中**零次引用** |

四個突變體全部存活。也就是說：唯一「驗證」過 bug#00133 的東西，是我手動看了一次列印輸出。
下一個人（或下一個我）把它改回去，不會有任何紅燈。

### (2) 腳本自己的預期與實際行為已經對不上

docstring 寫 `noise: nothing works, the loop should do nothing`，
但實跑 `noise` 得到 `candidates_proposed`——迴路對純雜訊產生了候選。
同時 `health=healthy` 卻又提了候選，這兩者是否該共存也沒有定義。
**這個分歧存在多久了沒人知道，因為沒有斷言。**

### (3) 單一種子、單一 family

`Random(20260803)` 一組。400 sessions 的那些數字（+0.000668 等）是不是幸運抽樣，未知。
而且整個 harness 只跑 `CROSS_MODEL` 加一個假 policy——bug#00133 主張「五個 family 壞掉」，
卻沒有任何測試讓**真實 adapter** 走過 fallback 路徑。

---

## 三、TDD 計畫

原則：**每一條測試先寫成紅的，紅法必須是「把修正改回舊行為就會失敗」。**
不接受「寫完程式再補一條會過的測試」。

### T0 — 補上 bug#00133 的回歸測試（先做，每條對應一個存活突變體）

| # | 測試 | 檔案 | 紅法 |
|---|---|---|---|
| T0-1 | `test_diagnosis_falls_back_to_economic_edge_when_baseline_absent` | `test_experiment_evaluation.py` | slice 的 `directional=None`／`economic=負`、ESS 足夠 → 必須產出 `NEGATIVE_EDGE`。fallback 拿掉即紅 |
| T0-2 | `test_directional_validation_takes_precedence_over_economic` | 同上 | 兩者皆有且**方向相反** → 必須採 directional。改成 `economic or directional` 即紅 |
| T0-3 | `test_all_five_baseline_less_families_can_reach_negative_edge` | 同上 | 對六個 `PolicyFamily` 參數化。**這條才是 bug#00132 的源頭防線** |
| T0-4 | `test_replay_scores_abstention_as_zero_excess` | `test_experiment_replay.py` | Champion 下注且虧、Challenger 觀望 → 該 pair **必須計入**且改善 > 0。改回 `None` 即紅 |
| T0-5 | `test_replay_still_drops_abstention_under_brier_skill` | 同上 | 機率指標下觀望仍不可計分 |
| T0-6 | `test_shadow_scores_abstention_as_zero_excess` | `test_experiment_feedback.py` | `evaluate_shadow` 同 T0-4。**兩處必須一起紅**，否則兩階段又會分岔 |
| T0-7 | `test_replay_report_exposes_both_coverage_ratios` | `test_experiment_replay.py` | 幾乎全觀望的 Challenger → 改善高**且**涵蓋率低，兩個欄位同時可讀。這就是把陷阱釘在原地 |
| T0-8 | `test_scoring_protocol_versions_are_pinned` | 新檔 | 三個 protocol 字串當契約寫死。任何計分行為變更都必須有意識地 bump |

### T1 — 不變量（property-style，跨規則／跨 family）

- `test_every_v1_rule_only_tightens` — 16 條規則各套一次，斷言 `coverage(challenger) ≤ coverage(champion)`。
  這是「涵蓋率守門為何必要」的結構性理由，不該只靠註解。
- `test_every_actionable_diagnosis_has_at_least_one_rule` — 診斷 enum × MutationRule 表交叉比對。
  漏掉就是永遠診斷得出來卻沒旋鈕可轉（`no_available_action` 應該是罕見狀態，不是預設）。
- `test_every_family_has_a_reachable_promotion_metric` — **預期紅**：cross-model 沒有 benchmark，
  D-05 主指標算不出。讓這個已知缺口變成一條 xfail 而不是備忘錄裡的一行字。

### T2 — 把 harness 變成有斷言的測試

1. `SeededFamily` 搬到 `tests/support/seeded_family.py`；
   `scripts/verify_feedback_loop.py` 改成 import 它、只負責列印。
   **一個世界、兩個消費者**，不會再各自漂移。
2. 新增 `tests/test_feedback_loop_end_to_end.py`，用明確的 oracle 表參數化三種模式：

   | mode | diagnoses | health | outcome | candidate | proposal |
   |---|---|---|---|---|---|
   | `edge` | 空 | healthy | `no_action_needed` | 0 | 無 |
   | `noise` | **待決** | **待決** | **待決** | ? | **必須無** |
   | `weak-signal` | `negative_edge` | degraded | `candidates_proposed` | `neutral_band` ↑ | 無（Gate 具名拒絕） |

   `noise` 那一列**寫下去就會紅**，因為現行行為與 docstring 相反。
   這是先要決定「對的答案是什麼」，不是先改程式。
3. sessions 降到能跑出同樣結論的最小值（現行 7–15s／模式，全套才 23s），
   或掛 `@pytest.mark.slow` 移出預設批次。

### T3 — 種子穩健性（`slow` 標記，不進預設批次）

- `test_weak_signal_diagnosis_is_stable_across_seeds` — 20 個種子，≥18 個必須診斷出 `negative_edge`。
  防「400 sessions 那次是幸運抽樣」。
- `test_gate_never_files_a_proposal_on_noise_across_seeds` — **整套系統最重要的安全性質**：
  Gate 不得把雜訊升級。這條若紅，其他都不重要。
- `test_detection_power_curve` — sessions ∈ {60, 120, 240, 400} × 偵測率。
  把記憶裡那句「約需 120 sessions」釘成可重跑的數字，而不是一次實驗的軼事。

### T4 — O-6 起改為嚴格 test-first

D-13 四組參數，每組依序：

1. 紅：白名單測試斷言參數存在且能產生候選 → 失敗。
2. 紅：`_reachable_values()` 對離散型的不變量 → 失敗（`generate_candidates` 目前只支援數值單步）。
3. 綠：實作離散型 mutation。
4. 紅：T2 oracle 表加一列涵蓋新旋鈕。

`regime_gate` 另需 `EvaluationSlice` 先長出 regime 維度——那也先寫紅的。

### T5 — 突變檢查常態化

`scripts/mutate_check.py`：對「決定升級與否」的那約 20 行維護一份
`(檔案, 舊字串, 新字串)` 突變清單，跑全套，**任何存活即為覆蓋率破洞**。
不是完整的 mutmut，是針對關鍵路徑的廉價版。本次四個存活突變體就是用這個方法找到的。

---

## 四、順序與阻擋

T0 → T2 → T1 → T3 → T4，T5 隨時可加。

**T2 被一個決策擋住**：`noise` 模式下迴路對純雜訊產生候選，是對還是錯？

- 若「對」：候選只是被記錄、Gate 會擋，產生它不算浪費 → 改 docstring，oracle 寫
  `no_incremental_edge / healthy / candidates_proposed`。
- 若「錯」：`no_incremental_edge` 在 health 仍 healthy 時不該觸發突變 → 改 orchestrator，
  oracle 寫 `no_action_needed`。

這是語意決策，不是 bug 修法，需要人決定。
