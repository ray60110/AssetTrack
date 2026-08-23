# 類股共識外部歷史驗證：決策摘要

日期：2026-08-15  
結論：**FAIL — 現行 `5 日中至少 3 日廣度同向` 規則，不能升級為已驗證的前瞻多空訊號。**

## 驗證範圍

- 資料：直接由 yfinance 下載 2016-01-01 至 2026-08-14 的 auto-adjusted 日線；不讀
  AssetTrack 的 sector snapshots。
- Universe：使用者目前六個類股群組，36 個 unique 成分股；QQQ 為主要市場基準、SPY 為次要基準。
- 固定規則：`lookback=5`、`min_days=3`、`breadth_threshold=0.5`、等權報酬門檻 `0.1%`；
  看完結果後沒有調整參數。
- 權重：採等權，避免把今日市值倒灌歷史。至少 70% 成分股有新報酬才評估；80% coverage
  另做敏感度。
- Outcome：精確第 1／5／10 個交易 session，不使用日曆日；同日多板塊先合併為日期簇，
  再 purge 重疊 forward windows。
- Primary：只看方向首次形成／翻轉的 5-session episode；2024 年後作 final holdout，並與
  簡單五日動能方向配對比較。
- 成本：primary 與市場超額報酬扣 10 bps；另要求 25 bps stress cost 後仍為正。

完整預註冊方法、yfinance 資料語意與 source 引用見
[研究規格](./sector_consensus_yfinance_validation_research.md)。

## Primary：2024 年後 final holdout

| 指標 | 結果 | 通過條件 | 判定 |
|---|---:|---:|---|
| 去重疊 episode blocks | 109 | ≥ 50 | 通過樣本門檻 |
| 扣 10 bps 後 signed return | **−0.036%** | 95% CI 下界 > 0 | FAIL；CI −1.304%～+1.293% |
| 相對五日動能 paired improvement | **+0.279%** | 95% CI 下界 > 0 | FAIL；CI −0.786%～+1.501% |
| 25 bps stress-cost signed return | **−0.186%** | > 0 | FAIL |
| development／validation／holdout 同號 | +0.277%／−0.281%／−0.036% | 全部同號 | FAIL |
| leave-one-sector-out | 移除部分板塊後翻負 | 全部仍為正 | FAIL |

Primary 有足夠樣本，但效果接近零且不穩定；這是「沒有預測證據」，不是「資料還不夠」。

## Secondary：持續狀態每天都算一次

| 方向 | 前瞻 | 去重疊 blocks | 命中率 | 無條件基準 | edge | p-value | 相對 QQQ 淨超額 |
|---|---:|---:|---:|---:|---:|---:|---:|
| up | +1 | 1,598 | 53.4% | 53.7% | −0.3pp | 0.607 | −0.043% |
| down | +1 | 1,187 | 44.7% | 46.3% | −1.6pp | 0.869 | −0.220% |
| up | +5 | 404 | 59.4% | 57.5% | +1.9pp | 0.241 | +0.395% |
| down | +5 | 326 | 40.2% | 42.7% | −2.5pp | 0.834 | −0.801% |
| up | +10 | 230 | 60.0% | 59.8% | +0.2pp | 0.499 | +0.653% |
| down | +10 | 204 | 38.7% | 40.5% | −1.8pp | 0.724 | −0.762% |

六個 secondary 組合全部未通過。尤其 `down` 分支在 1／5／10 日的命中 edge 與經濟報酬
均為負，不支持「共同賣出後還會續跌」的文案。

`up +5/+10` 有正的平均 QQQ 超額，但：

- 命中 edge 分別只有 +1.9pp／+0.2pp，皆不顯著；
- family-wise 經濟區間仍跨 0；
- 前後期方向 edge 不一致；
- 這是看完結果才注意到的現象，只能成為新的「相對強勢延續」假說，不能回頭算本次通過。

## 資料敏感度

| Run | Holdout blocks | 淨 signed return | 動能 improvement | 結果 |
|---|---:|---:|---:|---|
| Primary：coverage 70%、repair=False | 109 | −0.036% | +0.279% | FAIL |
| Coverage 提高至 80% | 109 | −0.036% | +0.279% | FAIL；實質相同 |
| 僅美國掛牌成分股 | 112 | −0.146% | −0.008% | FAIL |
| yfinance repair=True | 108 | −0.058% | +0.258% | FAIL |

38／39 個下載標的成功；`INFN` 因 Yahoo 已無 timezone/history 而失敗。coverage gate 會讓缺值
保持缺值，不會把它當成 0% 報酬。repair on/off、coverage 與跨市場版本都沒有翻轉結論。

## 決策

1. 不把現行 `sector_flow` 套用為「共同買進／共同賣出」或正式多空建議。
2. 可以保留為描述性欄位：`近五個合格交易日的板塊同步狀態`。
3. `down` 分支不應用來預測續跌；外部歷史證據反而顯示其後 signed return 明顯偏負。
4. 若要研究 `up +5/+10` 的相對強勢，只能建立新的、單向且相對 QQQ 的預註冊假說，
   並用新的 shadow-forward 資料驗證，不能修改本次規則後重用同一 holdout。
5. 本輪不修改 `_render_conclusions()`；正式 UI 語氣變更應作為下一個明確決策與實作。

## 可重現產物

- [驗證工具](../scripts/validate_sector_consensus_yfinance.py)
- [主結果](./sector_consensus_yfinance_validation_results.md)／
  [JSON](./sector_consensus_yfinance_validation_results.json)
- [80% coverage 敏感度](./sector_consensus_yfinance_validation_coverage80.md)
- [US-only 敏感度](./sector_consensus_yfinance_validation_us_only.md)
- [repair=True 敏感度](./sector_consensus_yfinance_validation_repair.md)
- [方法與來源研究](./sector_consensus_yfinance_validation_research.md)

原始 close matrix 以 SHA-256 固定並保存在 git-ignored `data/research/`，不寫入正式 sector cache。
