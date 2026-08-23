# 方向預測：驗證架構與期權建議重寫

日期：2026-08-17  
範圍：期權方向結論能否變成對使用者有用的投資建議；以及所有方向模型應如何被驗證。  
實測結果：[`docs/direction_forecast_validation.md`](./direction_forecast_validation.md)  
協議實作：`assettrack/direction_forecast_validation.py`（唯一評分 interface：`validate`）

## 結論

現行期權分析**不應再對使用者輸出可執行的多空投資建議**。不是文案太長而已，而是三件事同時成立：

1. **現行期權方向規則尚未被檢驗。** 本機只有 12–15 個正規化 NYSE session。+5 session、去重疊後獨立樣本 n=15，判定 **UNDERPOWERED**。命中率 46.7% 低於同窗無條件上漲率 66.7%，超額 −1.89%，但樣本不夠，這不是 FAIL，也不是可以調參的依據。
2. **在同一宇集上，簡單擇時也失敗。** 五日動能 n=2,915，判定 **FAIL**（命中 51.5% < 基準 55.2%；扣 10bps 後超額 CI 下界為負）。
3. **唯一通過 Scheme B 的政策是「永遠看多這些已在 2026 觀察清單上的股票」。** 這是存活者偏誤下的買進持有，不是期權模型，也不能當成系統會選股。

類股廣度規則已於 2026-08-15 用十年 yfinance 判定 **FAIL**（見 `docs/sector_consensus_yfinance_validation_summary.md`）。ETF 共識本機快照同樣只有約兩週，尚未用本協議評分。

因此：投資建議複雜，是因為系統在**沒有通過驗證的方向預測**之上，又疊了 Greeks、殘差、策略映射與修改指引。先刪掉沒通過驗證的「建議」，驗證架構才能開始累積真正的 Forecast Record。

## 驗證架構（已落地）

比較三種 interface 後，採用 **最小評分 seam + 呼叫端適配器**：

| 方案 | Interface | 取捨 |
|---|---|---|
| 最小 | `validate(forecasts, prices, spec)` | **採用。** 家族盲、可用字面價格測試、深度高。 |
| 彈性 | `evaluate` / `compare` + 大型 Spec | Spec 過大，把 holdout／LOSO／grain 全暴露。 |
| 腳本友善 | `validate(policy, evidence, truth)` | 把「走路」與「評分」綁在一起，測試必須 mock 抓價。 |

**唯一行為入口：**

```python
report = validate(forecasts, prices, spec)
# report.verdict in {"PASS", "FAIL", "UNDERPOWERED"}
```

呼叫端必須知道的契約：

- 輸入已是 **Forecast Record**（Policy Version、Outcome Target、Entry Session、horizon、up/down）。模組不重跑 OI、Greeks、廣度。
- 價格是 `(symbol, NYSE session) → adjusted close`。模組不抓 yfinance。
- 結算必須是 **剛好 +h 個 NYSE session**；缺價 = VOID，不用下一根 K 線頂替。
- 同標的同 horizon 的重疊 label 先 purge；raw n 只供稽核。
- **UNDERPOWERED / FAIL / PASS 分家。** n<30 即使命中 100% 也是 UNDERPOWERED，禁止當調參證據。
- 方向-only 的地板是 ADR 0003 Scheme B：成本調整後相對基準超額，95% CI 下界 > `0.0020`。機率政策用 Brier skill > `0.0200`。
- `gate.passed` **不是 Promotion**。它只是 Promotion Gate 可引用的一項證據。

家族適配器留在呼叫端：`scripts/validate_direction_forecasts.py` 已示範期權規則、Always-Up、五日動能三個 Adapter。ETF／類股下一步用同一 `validate`，不要再各寫一套命中率。

## 建議刪除與保留

### 從使用者可見的期權建議刪除

這些輸出目前假裝是投資貢獻，但其實沒有通過驗證的方向：

- `actionable_direction` 為「多／空」的正式預測（含 Dashboard 期權卡）
- 策略映射：該買 put、call spread、collar、賣波動等（Layer 3）
- 「修改建議：把 `bias_min_pct` 從 0.03 調到 0.05」——在 n=15 上調參是過擬合
- 結論卡上的三段公式（Dollar Delta OI、BS 殘差、Brier）作為**下單理由**
- 以 `(1-p)` 或校準機率當作「未來上漲／下跌機率」展示給使用者

`options_forecasting.assess_option_forecast` 的閘門意圖正確（未驗證就觀望），但失敗時仍丟出原始方向 + 長篇診斷 + QuantTrade 調參指引，等於把研究工作台當成投資建議。

### 保留（改成觀察，不是預測）

- **Observed regime**：現價、已實現報酬、IV／skew 事實、資料品質
- 部位風險：持倉與期權曝險是否同向（描述，不下單指令）
- 本驗證協議與校準頁的 **UNDERPOWERED／FAIL／PASS** 狀態

### 不要在污染目標上重訓

`docs/options_model_redesign_research.md` 已說明 OI 無方向、殘差用 lastPrice／midpoint 不穩。在固定 delta／tenor IV surface 與至少 6–12 個月 EOD 資料之前，**不要**再為 `compute_directional_verdicts` 加子訊號或調門檻。

## 落地順序

1. **立刻（產品）：** 期權正式建議固定為觀望／觀察；畫面只報已發生狀態與「方向預測樣本不足」。不要再顯示看多／看空投資貢獻。
2. **本協議成為唯一評分 seam：** `calibration.backtest_verdicts`、`analysis.backtest_etf_consensus`、`sector_analysis.backtest_sector_flow` 改成 Adapter → `validate`。畫面與研究腳本讀同一份 `DirectionValidationReport`。
3. **期權模型二選一，不要並行：**
   - **A. 刪方向、留觀察。** 期權頁變成持倉／流動性／IV 事實板。這是現在唯一誠實的產品。
   - **B. 重做特徵後再驗證。** 固定 30D 25Δ surface、session 對齊、midpoint；累積足夠獨立 +5 session 樣本後，用本協議對 QQQ 超額評分。未 PASS 不得進入 TUI 方向欄。
4. **使用者投資貢獻改走績效追蹤，不走期權預測。** 本宇集上唯一 PASS 的是買進持有相對 QQQ，那是 `PortfolioPerformanceTracker` 的工作，不是期權卡的工作。

## 重現

```bash
.venv/bin/python scripts/validate_direction_forecasts.py
.venv/bin/pytest verification/test_direction_forecast_validation.py
```
