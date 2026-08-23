# Option analysis 與預測校正模型重設研究

日期：2026-07-30  
範圍：`_build_options_flow_panel`、`CalibrationScreen.run_backtest`、
`options_analysis.compute_directional_verdicts` 與 options 回測／校正介入機制。

## 結論摘要

目前問題不是單一門檻設錯，而是四個層次同時混在一起：

1. **現在市場處於什麼狀態（nowcast）**沒有獨立輸出；只要 forward prediction
   沒通過回測閘門，Dashboard 就把方向改成「觀望」，因此連已發生的下跌 regime
   也消失。
2. **方向特徵設計不可靠**：OI 本身沒有買賣方向；重定價殘差使用可能陳舊的
   `lastPrice`，再用 `call residual - put residual` 當方向。兩側殘差都為負時，
   只要 Put 更負便會被判成 bullish。
3. **資料時間軸不一致**：快照以台北日曆日去重，沒有以美國交易 session
   去重；OI 通常也是上一交易日結算值，卻會和當日盤中 spot／quote 混用。
4. **options 沒有接進校正排程**：`CalibrationScreen.run_backtest()` 只顯示報告；
   `calibration_schedule.PARAM_SPEC` 與 `_run_calibration_cycle()` 只有 ETF、sector。

模型不應「把 Greeks 相加後猜方向」。Greeks 是期權價格對輸入變數的局部敏感度，
適合做風險歸因、moneyness／notional 標準化與 dealer-hedging 特徵；未來方向應來自
固定 tenor／delta 的 IV surface 變化、可辨識成交方向的 option flow、spot regime，
並以 walk-forward 的實現報酬校準成真正的機率。

## 本機資料觀察

使用 `data/options_cache/history/*.jsonl` 截至 2026-07-30 的真實累積快照：

- 8 檔中有 6 檔自第一筆至最新一筆下跌至少 5%，但 Dashboard 同一路徑產生
  **0 條看空**。
- raw verdict 在 2026-07-29 為 `6 空 / 0 多 / 2 觀望`，加入 2026-07-30
  一個端點後變成 `1 空 / 4 多 / 3 觀望`，顯示端點高度脆弱。
- AMD 同期 spot 下跌 22.5%、Dollar Delta OI 的 call share 只有 10.8%，但
  call residual = -0.195、put residual = -1.085，相減後 bias = +0.457，
  raw verdict 因而變成看多。兩側都被壓價並不是買權需求偏多證據。
- 把相同合約的最新價格由 `lastPrice` 改成 bid/ask midpoint，AMD bias
  從 `+0.457` 翻成 `-0.135`，TSM 從 `-0.003` 變成 `-0.492`。
  最新 4,242 張合約中，382 張（9.0%）的 last price 與 midpoint 相差超過 20%。
- 7/26→7/27 在 7/8 檔標的出現完全相同 spot；程式仍把它們當成不同日期。
  目前快照新鮮度用台北日期，背景刷新也未以美國交易 session 限制。
- options aggregate backtest 的 1 日 bearish raw `n=36`、命中率 94.4%，但只有
  6 個 distinct signal dates（ESS=6）；這不足以估計穩定係數或宣稱有效。
- 即使傳入一份 `n=100、hit rate=20%` 的故意失敗 options report，
  `propose_adjustments()` 仍回傳空陣列，因為 options 不在 `PARAM_SPEC`。

以上數據只用來診斷現行路徑，不代表可以用 6 個有效交易日訓練新模型。

## 一手研究對模型的含義

### 1. Greeks 不是未來方向訊號

Options Industry Council 將 Delta、Gamma、Theta、Vega 定義為期權理論價對
spot、delta、時間、IV 等輸入的敏感度，並明確說明它們只是理論 guidepost，
不是精確保證。因此：

- **Delta**：用於把 option flow 轉成 delta-equivalent notional，或用固定
  25Δ／50Δ 建立可比 surface bucket；不直接投 bullish/bearish 一票。
- **Gamma**：描述 Delta 的彎曲程度。只有在能推斷 dealer/customer position sign
  時，才可建立 gamma-hedging pressure；只有總 OI 時無法知道 dealer 是 long
  還是 short gamma。
- **Vega**：把權利金殘差轉為可比較的 IV shock，應用
  `residual / vega` 或直接比較固定 delta／tenor 的 `ΔIV`，避免一美元殘差在不同
  spot、DTE、strike 間不可比。
- **Theta**：應從跨日權利金變化中扣除；它是 carry，不是方向預測。
- **Rho**：對 60 DTE 內 equity options 通常次要，可作定價輸入，不應作方向訊號。
- **Vanna／Charm**：只在短天期且有 signed dealer exposure 時作次級 hedging-flow
  特徵，不能由未分方向的 OI 直接推論。

來源：[OIC — Understanding Options Greeks](https://www.optionseducation.org/advancedconcepts/understanding-options-greeks)；
[OIC — Volatility & the Greeks](https://www.optionseducation.org/advancedconcepts/volatility-the-greeks)。

### 2. OI 本身不代表多空

OIC 明確指出，新增一口 open interest 同時包含一個 long 與一個 short，
因此 open interest **既不代表 bullish 也不代表 bearish**。Pan and Poteshman
發現的方向預測力來自可辨識為 **buyer-initiated、opening** 的 put/call volume，
不是單純 OI 變化。

這表示現有 `ΔOI × |Delta|` 可以保留作「參與度／曝險規模」，但若資料源只有
yfinance OI，不能再稱為建倉方向。若要重現文獻訊號，需要交易級 signed flow
（相對 NBBO midpoint／bid／ask 分類）及 open/close 標記；否則方向權重應為 0。

來源：[OIC — Is increased open interest bullish?](https://www.optionseducation.org/referencelibrary/faq/general-information)；
[Pan & Poteshman (2006), *The Information in Option Volume for Future Stock Prices*](https://web.mit.edu/junpan/www/volume.pdf)。

### 3. 可用的 IV 特徵是 surface 的相對形狀與變化

研究支持的不是「IV 高就看空」，而是：

- 固定 tenor／moneyness 的 **volatility smirk**；較陡的 downside smirk
  與較低的後續個股報酬相關。
- call 與 put IV 的**變化差**；call IV 上升與較高後續報酬、put IV 上升與較低
  後續報酬相關。
- matched call-put 的 IV spread／put-call parity deviation。
- implied variance 與 realized variance 的差（variance risk premium）對
  aggregate market return 有預測內容，但它不是單一股票的簡單方向符號。

來源：

- [Xing, Zhang & Zhao (2010), *What Does the Individual Option Volatility Smirk Tell Us About Future Equity Returns?*](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/abs/what-does-the-individual-option-volatility-smirk-tell-us-about-future-equity-returns/ECFD16BA9ACBDC8D577D1BD866FBEA72)
- [An, Ang, Bali & Cakici (2014), *The Joint Cross Section of Stocks and Options*](https://www.nber.org/system/files/working_papers/w19590/w19590.pdf)
- [Bali & Hovakimian (2009), *Volatility Spreads and Expected Stock Returns*](https://pubsonline.informs.org/doi/abs/10.1287/mnsc.1090.1063)
- [Cremers & Weinbaum (2010), *Deviations from Put-Call Parity and Stock Return Predictability*](https://ideas.repec.org/a/cup/jfinqa/v45y2010i02p335-367_00.html)
- [Bollerslev, Tauchen & Zhou (2009), *Expected Stock Returns and Variance Risk Premia*](https://public.econ.duke.edu/~get/wpapers/btz.pdf)

這些結果多來自較長歷史、較廣股票橫截面，而且部分論文也指出 predictability
隨時間減弱；不能把論文係數直接搬入 2026 年的 8 檔清單，仍需本地樣本外驗證。

## 建議的 feature pipeline

### A. 報價與 session

1. 以 `America/New_York` 的 market session 當主鍵，休市日不寫新快照。
2. 分開保存 `quote_session`、`spot_asof`、`oi_asof_session`；OI 未結算時不得和
   當日盤中流量混稱同一時點。
3. 價格優先用 bid/ask midpoint（有 NBBO feed 時使用 NBBO）；crossed/locked、
   bid=0、relative spread >20%、過期 last trade 皆降品質或排除。
   `lastPrice` 只可作低品質 fallback。
4. 個股期權多為 American-style；定價／反解 IV 至少要有 dividend yield，
   更穩健可用 American binomial 或 Barone-Adesi–Whaley。現行無 dividend 的
   European Black-Scholes 殘差會混入模型誤差。
5. 不再用「固定合約代碼＋移動中的 spot ±20% strike band」做主要 surface 比較；
   大跌時合約宇集會系統性換掉。改在每個 session 擬合無套利 IV surface，再抽取
   固定 delta／tenor 特徵。

### B. 固定 tenor／delta 特徵

先對 total variance 做到期插值，固定輸出 30D、60D：

- `ATM_IV_30`, `ΔATM_IV_30_1d`, `ΔATM_IV_30_5d`
- `RR25_30 = IV_put_25Δ - IV_call_25Δ`
- `ΔRR25_30_1d/5d`
- `BF25_30 = (IV_put_25Δ + IV_call_25Δ)/2 - ATM_IV_30`
- `TERM = ATM_IV_30 - ATM_IV_60`
- `VRP = ATM_IV_30² - forecast_realized_variance_20d`
- call/put IV change spread（依 An et al. 定義作候選）
- quote coverage、median spread、surface fit error 作 data-quality features
- earnings、ex-dividend、macro event flags 作控制變數

IV rank 分成兩種：20/60 日短期 regime rank 與 252 交易日 valuation rank。
現行 `min_days=8` 不能稱為穩健歷史位階。

### C. flow／Greek 特徵

- 有 signed trade data：buyer-initiated opening put/call ratio、
  signed delta notional、signed vega notional。
- 只有 yfinance：volume/OI 只作 participation/liquidity，方向權重設 0；
  次日 OI 只能用來確認 opening activity，不可推斷買賣方。
- Gamma/Vanna/Charm exposure 只有在 position sign 可辨識時使用。

## 模型分層

### Layer 1：Observed regime（永遠顯示）

這層回答「現在發生什麼」，不能被 forecast confidence 隱藏：

- spot 1/5/20 日報酬、20 日 drawdown、realized volatility
- watchlist／sector 下跌 breadth
- `ΔATM_IV`、`ΔRR25`、term inversion
- 資料品質

輸出三態 `down / range / up` 及原因。例如：

> 目前狀態：下跌 regime（6/8 標的跌逾 5%、短端 IV 上升）；  
> 期權市場：downside skew 是否同步；  
> 這是已觀察狀態，不是對未來的保證。

資料仍只有少數 session 時，先用透明規則或 shrinkage score；不要假裝已訓練 ML。

### Layer 2：Forward forecast（機率）

固定事前選定 1、5、20 個**交易日**三個 horizon，各自估：

`P(return[t→t+h] < 0 | features_t)`

建議起始模型為 regularized hierarchical logistic regression：

- horizon 各有一組係數；
- symbol／sector random intercept 做 partial pooling；
- features 只用截至 t 的 rolling normalization；
- 資料量足夠後再比較 GAM／gradient boosting challenger。

禁止在同一份樣本上從 8 個 horizon 挑 `(1-p)` 最大者。`1 - p_value`
是反對虛無假設的證據程度，不是「股價下跌機率」。方向閘門應用經樣本外校準的
`p_down`，例如初始 `p_down ≥ 0.60` 看空、`≤ 0.40` 看多，中間觀望；
實際閾值再依 transaction cost 與效用函數 walk-forward 選定。

### Layer 3：Strategy mapping

方向與期權貴／便宜分開：

- bearish + low/normal IV：long put 或 put debit spread 類的 defined-risk 候選；
- bearish + high IV／steep put skew：downside protection 已昂貴，優先顯示
  put spread／collar 類 defined-risk 候選，不因方向看空就直接叫使用者追高 naked put；
- neutral + high IV：只描述 volatility-selling 候選及尾部風險，不把高 IV 說成看空；
- bullish 同理依 IV cost 決定 call、call spread 或不交易。

策略輸出必須帶 spread、liquidity、最大損失、earnings/event 與使用者部位風險，
不能只由方向映射。

## 回測與失效介入

### 1. 建立 forecast ledger

每次畫面產生預測時保存：

`forecast_id, as_of_session, symbol, horizon, model_version, feature_version,
p_down, expected_return/quantiles, observed_regime, data_quality, maturity_session`

到 maturity 才寫入 realized return；horizon 尚未成熟時，「目前股價與預測不同」
只能算 live divergence，不能提前當作 forecast miss。Observed regime 則可即時評估。

### 2. 用 proper score，不用 `(1-p)` 當 confidence

每個 horizon／symbol／regime 持續更新：

- Brier score 與 Brier skill（相對 rolling base-rate benchmark）
- log loss
- calibration intercept/slope、reliability bins
- direction hit rate／edge
- realized strategy P&L（扣 bid/ask、滑價與最大資金占用）

walk-forward 必須用交易 session、purge overlapping labels，長 horizon 不可把每日
重疊報酬當獨立樣本。

### 3. 即時 drift monitor + champion/challenger

每個成熟 outcome 都更新 prequential loss；不用等固定雙週才看錯誤。
可採 ADWIN／Page-Hinkley/CUSUM 監控 model loss 與 feature distribution。
ADWIN 的核心是資料平穩時自動擴大視窗、偵測變化時縮短視窗並丟棄過時資料，
比固定 14 天一刀切更適合 regime change。

來源：[Bifet & Gavaldà (2007), *Learning from Time-Changing Data with Adaptive Windowing*](https://www.cs.upc.edu/~gavalda/papers/adwin06.pdf)；
[Diebold & Mariano, *Comparing Predictive Accuracy*](https://www.nber.org/papers/t0169)。

建議的初始工程 guardrail（需用長歷史調整，不是學術常數）：

- 連續 3 次 `p ≥ 0.65` 的成熟方向預測錯誤：標 `warning`，live model 權重減半；
- ADWIN/CUSUM 告警，或 rolling excess Brier 明顯劣於 base-rate benchmark：
  標 `degraded`；依 2026-08-02 D-02 決策採 Warning Mode，方向只有在原門檻已通過時續顯示，
  並強制附近期失配與不可單獨採用限制；
- 有至少 30 個有效獨立 outcomes，且 challenger 在相同 walk-forward
  sessions 的 loss 穩定優於 champion，才產生 promotion proposal；
- 參數／模型 promotion 維持使用者確認；資料不足仍自動 abstain，degraded 則自動顯示
  Warning Mode 限制。是否對 Cross-model 自動降權另立決策，不由 degraded 狀態暗中改權重。

### 4. 校正排程要改的狀態機

目前只記 `last_calibrated`，即使樣本不足、沒有 proposal 也會蓋日期。建議拆成：

- `last_outcome_evaluated`
- `last_drift_check`
- `last_challenger_trained`
- `last_model_promoted`
- `model_health = healthy/warning/degraded/retraining`

options 必須成為獨立 family，保存 surface／probability／quality 參數與 model version。
`CalibrationScreen` 應顯示 per-symbol/horizon 的 matured forecast、Brier skill、
calibration、drift alarm 與 challenger 狀態，而非只有累積命中率。

## 落地順序

1. 先修 market-session 去重與 midpoint／quote-quality，停止新增污染樣本。
2. Dashboard 拆成「Observed regime」與「Forward forecast」兩行，讓下跌狀態永遠可見。
3. 移除 OI 方向投票與 `call residual - put residual` 的硬方向；建立固定
   delta／tenor IV surface features。
4. 建 forecast ledger 與 proper-score/drift monitor。
5. 以歷史供應商資料或至少 6–12 個月真實 EOD surface 累積資料訓練
   hierarchical probability model；目前少數 session 僅可跑 descriptive regime。
6. 建 champion/challenger，最後才接 options 的自動降權與需確認 promotion。

這個順序避免在污染資料與錯誤目標上「調門檻」；先讓資料和輸出語意正確，再談係數。

## 2026-07-30 實作狀態

本輪已完成安全層與資料層修正：

- 舊快照依合約 `lastTradeDate` 映射至美股 session，同 session 只保留最後一次 capture；
- 新快照直接保存 yfinance 最後交易 row 的 `session_date`；
- 權利金重定價與價格震盪只採可靠雙邊報價 midpoint，寬 spread／陳舊
  `lastPrice` 不再進方向特徵；
- horizon 改以市場 session 計，5-session 為事前固定目標；資料不足時只依可得性選
  最近 horizon，不再挑 `(1-p)` 最大者；
- Dashboard 依產品規格把 `(1-p)` 顯示為信心水準，嚴格大於 60% 才輸出
  「預期 +N 天上漲／下跌」；n、ESS、正 edge、Bonferroni 與穩定性仍保留為完整統計
  診斷，不把這個百分比誤稱為單次漲跌的客觀機率；
- Dashboard 已分開顯示 Observed Regime 與 Forward Forecast；
- options 已納入校準 family；偏多／偏空分支分開做近期成熟結果監控，任一分支連續
  三個獨立 session 失配且命中率不高於 40% 時標 `degraded`。依 D-02，原本已通過方向與
  信心門檻的輸出仍保留，但醒目顯示近期失配、可信度受限與不可單獨採用；同時建立
  「提高 IV 殘差門檻」的待確認提案，相同 evidence fingerprint 不重複提案。

尚未完成、不可假裝已完成的部分：固定 delta／tenor IV surface、forecast ledger、
Brier/log-loss、完整 NYSE 假日日曆、purged walk-forward、ADWIN 及
champion/challenger。現有五個有效美股 session 只足以啟動安全降級，不足以訓練或升級
新的機率模型。

## 2026-08-14 架構複查與落地狀態

本輪把「原始方向訊號」與「可採用的未來預測」拆成兩個不同輸出，並新增
`options_forecasting` 深模組作為唯一 interface：

- 預測 horizon 事前固定為 +5 個 NYSE session；不再從同一份資料挑 `(1-p)` 最大的期間。
- 每個預測機率只使用該 signal session 當時已成熟的 outcomes，以 expanding
  empirical-Bayes 向當時基準率收縮；`(1-p value)` 不再被稱為漲跌機率。
- 同標的、同 horizon 的重疊 label interval 在樣本建構時直接 purge；raw n 只供稽核，
  `purged n` 才參與回測與正式方向閘門。
- 回測新增 Brier score、Brier skill、log loss、命中 edge，並保留 Wilson、Bonferroni、
  前後穩定性與近期健康度。只有全部通過，原始方向才成為 `actionable_direction`。
- 未通過時畫面仍顯示模型原始預測，但正式建議為觀望，並依「樣本不足／負 Brier
  skill／無 edge／不穩定／degraded」顯示具體處理方式。樣本不足明確要求不要調參；
  模型失效才建議在 QuantTrade 建立提高 `bias_min_pct` 一步的候選，重新跑相同 purged
  walk-forward，禁止直接改正式參數或在同一份樣本反覆調到過關。
- horizon 計算已接到內建 NYSE 休市日日曆。

Forecast Ledger、Champion／Challenger、Replay 與 Promotion 自 2026-08-06 spin-out 後
屬於 QuantTrade，不在 AssetTrack 重建第二套控制器。仍未完成的是固定 delta／tenor IV
surface、signed opening flow、ADWIN／CUSUM；在這些資料可用前，現行 OI／殘差規則只保留
為候選特徵，不能繞過 proper-score 閘門。

## 2026-08-17 方向預測驗證

已落地家族盲評分模組 `assettrack.direction_forecast_validation.validate`，並用 yfinance
還原收盤結算現行期權規則、Always-Up、五日動能。結果：期權 **UNDERPOWERED**（n=15）、
動能 **FAIL**、Always-Up 在 2026 觀察清單上 **PASS**（存活者偏誤）。產品含義與刪除範圍見
[`docs/direction_forecast_architecture.md`](./direction_forecast_architecture.md)。
