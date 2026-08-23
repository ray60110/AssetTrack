# 方向預測驗證結果

執行時間：2026-08-17T23:45:23  
協議：`direction-forecast-validation-v1`  
宇集：AMD, INTC, MU, NVDA, PLTR, SPCX, TSLA, TSM  
主前瞻期：+5 個 NYSE session；基準 QQQ，成本 10 bps。  
通過條件：去重疊獨立樣本 ≥ 30，且成本調整後超額報酬 95% CI 下界 > 0.0020。

## 凍結假說

1. **options-directional-verdicts-v1**：現行 `compute_directional_verdicts`（Dollar Delta OI skew + 重定價殘差），看完結果後不調參。
2. **naive-always-up-v1**：每個有收盤價的 session 都看多。
3. **naive-momentum-5-v1**：過去 5 個 session 上漲則看多，否則看空。

結算一律用 yfinance `auto_adjust=True` 收盤，不用期權快照裡的 spot。缺剛好 +h session 的收盤記 VOID，不拿下一根 K 線頂替。

## 本機期權快照覆蓋

| 標的 | 正規化後 session 數 |
|---|---:|
| AMD | 15 |
| INTC | 15 |
| MU | 15 |
| NVDA | 15 |
| PLTR | 15 |
| SPCX | 12 |
| TSLA | 15 |
| TSM | 15 |

現行期權規則在這些快照上發出 225 筆（含 1／5／10 session）Forecast Record。

## 主要結果（primary +5 session）

| Policy Version | 判定 | 去重疊 n | 命中率 | 無條件上漲率 | 扣 10bps 後超額 | 95% CI | 原因 |
|---|---|---:|---:|---:|---:|---|---|
| 現行期權方向 | **UNDERPOWERED** | 15 | 46.7% | 66.7% | -1.886% | [-5.611%, +1.647%] | purged n=15<30 |
| 永遠看多 | **PASS** | 2916 | 56.1% | 56.1% | +0.515% | [+0.299%, +0.739%] | signed excess +0.0051, CI lower +0.0030 > floor 0.002 |
| 五日動能 | **FAIL** | 2915 | 51.5% | 55.2% | +0.101% | [-0.123%, +0.325%] | signed excess CI lower -0.0012292830802545774 ≤ floor 0.002 |

## 既有外部驗證（不重跑）

類股 `5 日中 3 日廣度同向` 規則已於 2026-08-15 用 2016–2026 yfinance 判定 **FAIL**（holdout 扣 10bps 後 signed return −0.036%，CI 跨 0）。見 `docs/sector_consensus_yfinance_validation_summary.md`。

## 決策含義

UNDERPOWERED 不是 FAIL：表示測試尚未發生，不能據此調參或宣稱有效。
FAIL 表示樣本足夠且未清過 Scheme B 地板。PASS 才是可提交 Promotion Gate 的證據，仍不自動升級 Champion。

**宇集存活者偏誤：** Always-Up 的 PASS 是在 2026 年已選定的觀察清單上回溯 2016–2026。這不是 2016 年可交易的發現，不能解讀成「系統會選股」。它只證明：在這個已存活的清單上，買進持有相對 QQQ 有正超額；擇時（五日動能）即使享有同一偏誤也 FAIL。

架構、刪除範圍與下一步見 `docs/direction_forecast_architecture.md`。

