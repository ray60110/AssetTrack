# 類股演算法：十個固定 yfinance 樣本窗驗證

執行日期：2026-08-16  
資料 SHA-256：`869593edb746b5a42ab9f307ca9abb8f06217b2917cd1bba61cbb1fe1ca56b4f`  
資料範圍：2015-07-01～2026-08-14

## 固定流程

- 十個固定年度窗：2016-08-15～2026-08-15；W1–W6 development、W7–W8 selection、W9–W10 final holdout。
- 每窗沿全期固定相位、每隔 20 個 QQQ 交易 session 取樣；預測未來第 20 個 session，結果不重疊。
- 六個目前設定的類股群組一起評估；所有方法使用完全相同的行情矩陣與 anchor。
- 現行方法與絕對趨勢 challenger 預測板塊等權報酬方向；輪動法預測相對 QQQ 超額方向。
- TUI gate：全期與 W9–W10 命中率 >60%、holdout balanced accuracy >60% 且 date-cluster bootstrap 95% CI 下界 >50%、勝過 majority baseline、訊號覆蓋 ≥50%。

## 依序結果

| 順序 | 方法 | 目標 | 訊號/可評估 | 覆蓋 | 全期命中 | 全期 BA | 多數基準 | W9–10 命中 | W9–10 BA（95% CI） | TUI |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| 0 | `current_consensus` | absolute | 275/706 | 39.0% | 57.1% | 54.2% | 62.5% | 55.4% | 52.1% (40.7%～63.6%) | FAIL |
| 1 | `cross_sector_relative_momentum` | relative_qqq | 496/706 | 70.3% | 49.6% | 49.6% | 53.6% | 52.1% | 52.1% (40.5%～63.6%) | FAIL |
| 2 | `time_series_momentum_12m` | absolute | 691/706 | 97.9% | 56.0% | 50.3% | 60.2% | 54.2% | 47.9% (39.9%～55.7%) | FAIL |
| 3 | `sma_5_150` | absolute | 667/706 | 94.5% | 55.6% | 51.6% | 59.8% | 51.9% | 49.6% (38.9%～60.5%) | FAIL |
| 4 | `breakout_50` | absolute | 117/706 | 16.6% | 56.4% | 50.6% | 59.8% | 66.7% | 48.8% (32.1%～72.2%) | FAIL |
| 5 | `relative_momentum_breadth` | relative_qqq | 254/706 | 36.0% | 52.8% | 52.4% | 52.0% | 64.4% | 63.8% (50.0%～77.4%) | FAIL |
| 6 | `agreement_absolute` | absolute | 66/706 | 9.3% | 54.5% | 51.8% | 63.6% | 63.6% | 58.3% (22.7%～94.4%) | FAIL |
| 7 | `agreement_relative_qqq` | relative_qqq | 66/706 | 9.3% | 53.0% | 53.7% | 53.0% | 72.7% | 70.8% (30.0%～100.0%) | FAIL |
| 8 | `two_of_three_bullish` | absolute | 199/706 | 28.2% | 64.3% | 50.0% | 64.3% | 69.2% | 50.0% (50.0%～50.0%) | FAIL |

## 十窗明細

### 0. `current_consensus`

現行 breadth ±0.5、等權日報酬 ±0.1%、近 5 日至少 3 日同向

方向拆分：預測 up 172 次／命中 65.7%；預測 down 103 次／命中 42.7%。

| 窗 | 用途 | 訊號數 | 命中率 | balanced accuracy | majority edge |
|---|---|---:|---:|---:|---:|
| W1 | development | 17 | 47.1% | 25.0% | -47.1% |
| W2 | development | 19 | 73.7% | 55.8% | -5.3% |
| W3 | development | 19 | 42.1% | 42.8% | -10.5% |
| W4 | development | 35 | 68.6% | 54.9% | 0.0% |
| W5 | development | 29 | 58.6% | 58.6% | 0.0% |
| W6 | development | 40 | 45.0% | 45.1% | -7.5% |
| W7 | selection | 37 | 62.2% | 61.9% | 8.1% |
| W8 | selection | 23 | 60.9% | 57.4% | -13.0% |
| W9 | final_holdout | 28 | 53.6% | 57.3% | -3.6% |
| W10 | final_holdout | 28 | 57.1% | 47.5% | -14.3% |

### 1. `cross_sector_relative_momentum`

6/12 月動能各半、排除最近 1 月；六板塊 top/bottom 2 預測相對 QQQ 強弱

方向拆分：預測 up 248 次／命中 53.2%；預測 down 248 次／命中 46.0%。

| 窗 | 用途 | 訊號數 | 命中率 | balanced accuracy | majority edge |
|---|---|---:|---:|---:|---:|
| W1 | development | 48 | 47.9% | 47.6% | -20.8% |
| W2 | development | 52 | 46.2% | 46.2% | -3.8% |
| W3 | development | 48 | 52.1% | 52.2% | -8.3% |
| W4 | development | 52 | 53.8% | 53.9% | -3.8% |
| W5 | development | 52 | 42.3% | 41.5% | -23.1% |
| W6 | development | 48 | 54.2% | 54.3% | -4.2% |
| W7 | selection | 52 | 48.1% | 48.1% | -7.7% |
| W8 | selection | 48 | 47.9% | 47.9% | -8.3% |
| W9 | final_holdout | 52 | 38.5% | 38.5% | -11.5% |
| W10 | final_holdout | 44 | 68.2% | 69.6% | 4.5% |

### 2. `time_series_momentum_12m`

12 月板塊等權動能、排除最近 1 月，預測未來 20-session 絕對方向

方向拆分：預測 up 538 次／命中 60.4%；預測 down 153 次／命中 40.5%。

| 窗 | 用途 | 訊號數 | 命中率 | balanced accuracy | majority edge |
|---|---|---:|---:|---:|---:|
| W1 | development | 60 | 73.3% | 52.0% | 0.0% |
| W2 | development | 64 | 60.9% | 50.7% | 0.0% |
| W3 | development | 60 | 38.3% | 38.3% | -11.7% |
| W4 | development | 65 | 60.0% | 47.5% | -9.2% |
| W5 | development | 76 | 68.4% | 49.1% | -1.3% |
| W6 | development | 72 | 48.6% | 51.6% | -6.9% |
| W7 | selection | 78 | 43.6% | 46.2% | -11.5% |
| W8 | selection | 72 | 59.7% | 50.0% | 0.0% |
| W9 | final_holdout | 78 | 46.2% | 41.0% | -12.8% |
| W10 | final_holdout | 66 | 63.6% | 55.9% | 1.5% |

### 3. `sma_5_150`

等權板塊指數 SMA5 與 SMA150 的相對位置

方向拆分：預測 up 471 次／命中 60.9%；預測 down 196 次／命中 42.9%。

| 窗 | 用途 | 訊號數 | 命中率 | balanced accuracy | majority edge |
|---|---|---:|---:|---:|---:|
| W1 | development | 60 | 71.7% | 48.9% | -1.7% |
| W2 | development | 57 | 61.4% | 52.6% | -1.8% |
| W3 | development | 60 | 38.3% | 38.3% | -11.7% |
| W4 | development | 69 | 66.7% | 52.9% | -4.3% |
| W5 | development | 78 | 61.5% | 46.8% | -7.7% |
| W6 | development | 72 | 45.8% | 44.7% | -9.7% |
| W7 | selection | 78 | 55.1% | 54.5% | 0.0% |
| W8 | selection | 64 | 51.6% | 47.0% | -4.7% |
| W9 | final_holdout | 71 | 47.9% | 47.7% | -9.9% |
| W10 | final_holdout | 58 | 56.9% | 51.6% | -1.7% |

### 4. `breakout_50`

等權板塊指數突破/跌破前 50-session 區間才發訊號

方向拆分：預測 up 93 次／命中 60.2%；預測 down 24 次／命中 41.7%。

| 窗 | 用途 | 訊號數 | 命中率 | balanced accuracy | majority edge |
|---|---|---:|---:|---:|---:|
| W1 | development | 13 | 61.5% | 50.0% | 0.0% |
| W2 | development | 5 | 80.0% | 75.0% | 20.0% |
| W3 | development | 10 | 20.0% | 20.0% | -30.0% |
| W4 | development | 16 | 68.8% | 50.0% | 0.0% |
| W5 | development | 11 | 45.5% | 48.3% | -9.1% |
| W6 | development | 9 | 33.3% | 39.3% | -44.4% |
| W7 | selection | 18 | 44.4% | 44.4% | -5.6% |
| W8 | selection | 8 | 87.5% | 91.7% | 12.5% |
| W9 | final_holdout | 13 | 69.2% | 40.9% | -15.4% |
| W10 | final_holdout | 14 | 64.3% | 52.5% | -7.1% |

### 5. `relative_momentum_breadth`

跨板塊相對動能再要求至少 60% 成分股位於 50MA 同側

方向拆分：預測 up 150 次／命中 54.0%；預測 down 104 次／命中 51.0%。

| 窗 | 用途 | 訊號數 | 命中率 | balanced accuracy | majority edge |
|---|---|---:|---:|---:|---:|
| W1 | development | 21 | 47.6% | 38.5% | -14.3% |
| W2 | development | 21 | 52.4% | 56.7% | -19.0% |
| W3 | development | 22 | 40.9% | 40.0% | -13.6% |
| W4 | development | 28 | 50.0% | 44.4% | -10.7% |
| W5 | development | 25 | 56.0% | 53.9% | 0.0% |
| W6 | development | 27 | 55.6% | 54.0% | -3.7% |
| W7 | selection | 24 | 41.7% | 44.4% | -20.8% |
| W8 | selection | 27 | 48.1% | 47.2% | -18.5% |
| W9 | final_holdout | 26 | 53.8% | 54.2% | 0.0% |
| W10 | final_holdout | 33 | 72.7% | 72.7% | 6.1% |

### 6. `agreement_absolute`

breadth 3-of-5 與相對動能＋50MA breadth 同向；評估板塊絕對漲跌

方向拆分：預測 up 40 次／命中 65.0%；預測 down 26 次／命中 38.5%。

| 窗 | 用途 | 訊號數 | 命中率 | balanced accuracy | majority edge |
|---|---|---:|---:|---:|---:|
| W1 | development | 4 | 25.0% | 16.7% | -50.0% |
| W2 | development | 4 | 75.0% | 50.0% | 0.0% |
| W3 | development | 5 | 60.0% | 58.3% | 0.0% |
| W4 | development | 13 | 76.9% | 61.7% | 0.0% |
| W5 | development | 9 | 44.4% | 50.0% | -22.2% |
| W6 | development | 5 | 20.0% | 25.0% | -40.0% |
| W7 | selection | 11 | 54.5% | 55.0% | 0.0% |
| W8 | selection | 4 | 25.0% | — | -75.0% |
| W9 | final_holdout | 2 | 50.0% | — | -50.0% |
| W10 | final_holdout | 9 | 66.7% | — | -33.3% |

### 7. `agreement_relative_qqq`

完全相同 AND 訊號；評估板塊相對 QQQ 強弱

方向拆分：預測 up 40 次／命中 50.0%；預測 down 26 次／命中 57.7%。

| 窗 | 用途 | 訊號數 | 命中率 | balanced accuracy | majority edge |
|---|---|---:|---:|---:|---:|
| W1 | development | 4 | 25.0% | 16.7% | -50.0% |
| W2 | development | 4 | 50.0% | 50.0% | 0.0% |
| W3 | development | 5 | 80.0% | 87.5% | 0.0% |
| W4 | development | 13 | 53.8% | 51.2% | 0.0% |
| W5 | development | 9 | 22.2% | 12.5% | -66.7% |
| W6 | development | 5 | 60.0% | 37.5% | -20.0% |
| W7 | selection | 11 | 54.5% | 55.0% | 0.0% |
| W8 | selection | 4 | 50.0% | 66.7% | -25.0% |
| W9 | final_holdout | 2 | 50.0% | — | -50.0% |
| W10 | final_holdout | 9 | 77.8% | 87.5% | -11.1% |

### 8. `two_of_three_bullish`

breadth 3-of-5、相對動能＋50MA breadth、SMA5/150 至少兩票多方且零偏空票；只發多方候選

方向拆分：預測 up 199 次／命中 64.3%；預測 down 0 次／命中 —。

| 窗 | 用途 | 訊號數 | 命中率 | balanced accuracy | majority edge |
|---|---|---:|---:|---:|---:|
| W1 | development | 25 | 64.0% | 50.0% | 0.0% |
| W2 | development | 20 | 70.0% | 50.0% | 0.0% |
| W3 | development | 7 | 14.3% | 50.0% | -71.4% |
| W4 | development | 35 | 77.1% | 50.0% | 0.0% |
| W5 | development | 22 | 68.2% | 50.0% | 0.0% |
| W6 | development | 14 | 42.9% | 50.0% | -14.3% |
| W7 | selection | 18 | 50.0% | 50.0% | 0.0% |
| W8 | selection | 19 | 68.4% | 50.0% | 0.0% |
| W9 | final_holdout | 16 | 68.8% | 50.0% | 0.0% |
| W10 | final_holdout | 23 | 69.6% | 50.0% | 0.0% |

## 決策

現行規則 FAIL。沒有 challenger 通過預先固定的 >60% 與穩定性 gate。
產品決策覆寫：使用者於 2026-08-16 明確選擇 `two_of_three_bullish` 作為實驗性多方 gate；此接線不改變上表 FAIL 判定；偏空票只作風險警示，TUI 必須揭露其仍待 forward shadow 驗證。

## 限制

- 這是目前成分清單回套歷史，仍有存活者偏誤；不等同 point-in-time 指數成分。
- yfinance 沒有可靠逐日歷史市值，因此全部使用等權，避免以今日市值倒灌歷史。
- 十個年度窗是穩定性檢查；holdout CI 以日期為 cluster，但跨窗 regime 仍可能相依。
- 候選方法通過本輪也只代表可進 TUI shadow/提示，不代表保證獲利。
