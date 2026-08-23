# 市面期權分析方法與命中率（導入決策）

日期：2026-08-18  
範圍：TUI 已移除期權方向投資建議後，列出可導入的市場／學術方法，並把「命中率」追到一手來源。  
決定權：由使用者選 0–N 項；**未選定前不接回 TUI**。

## 怎麼讀這份表

文獻幾乎不報「單檔看多／看空命中率」。主流結果是：

- 橫截面：每週或每月把**數百檔**有期權的股票依訊號排序，做多空組合，報平均報酬或 Fama–French alpha。
- 時間序列：對 **S&P 500 整場** 做迴歸，報 \(R^2\)，不是個股方向。

那不是本專案閘門。本專案若要把任何方法變成畫面建議，必須先通過 `direction_forecast_validation.validate`：去重疊獨立樣本 ≥ 30，且扣成本後超額報酬 95% CI 下界 > `0.0020`（Scheme B）。現行 `compute_directional_verdicts` 在觀察清單上是 **UNDERPOWERED**（purged n=15，命中率 46.7%，無條件上漲率 66.7%）。見 `docs/direction_forecast_validation.md`。

因此下表分三欄：

1. **文獻證據**：論文自己寫了什麼（期間、宇集、horizon、是否 purge）。
2. **後續打折**：發表後衰退、複製失敗、或被借券費／非同步報價解釋掉多少。
3. **本專案能不能做**：現況只有 yfinance 日鏈（volume、OI、IV、bid/ask、lastPrice），約 8 檔、12–15 個 NYSE session。沒有 OPRA 逐筆、沒有 open/close 標記、沒有 dealer 持倉符號、沒有 OptionMetrics 標準化 delta surface、沒有高頻已實現變異。

沒有一手命中率的方法，明寫「無」，不捏造數字。

---

## 本專案資料邊界

`quotes.fetch_options_chain_snapshot` 經 yfinance `option_chain()` 取即時鏈，本機再用 jsonl 累積歷史。官方文件與程式註解都寫明：這是**當日快照**，不是帶方向的成交帶。

| 欄位 | 有 | 沒有 |
|---|---|---|
| 合約 volume、open interest、yfinance IV | 有 | OI 無買賣方向（OIC：一口新 OI = 一多一空） |
| bid / ask / lastPrice | 有（last 常過期） | NBBO 逐筆、Lee–Ready 主動買／賣 |
| 到期日與履約價 | 有（約 60 DTE、±20% strike） | 固定 30D 25Δ 的無套利 surface |
| 標的日收盤 | 有（另用 yfinance history） | 借券費、early-exercise 標記、高頻 RV |

---

## 1. 開倉買方 put–call 量比（Pan & Poteshman 2006）

**定義。** 用「買家**新開倉**」的 put volume / (put+call volume)，不是全部成交量，也不是 OI。

**預測標的。** 個股次日／一週報酬（橫截面）。

**文獻數字。** Pan & Poteshman, *The Information in Option Volume for Future Stock Prices*, Review of Financial Studies 19(3), 2006。樣本 1990–2001，CBOE 專有資料（16 類：open-buy / open-sell / close-buy / close-sell × 投資人分類）。最低五分位 put–call 比的股票，次日風險調整後勝過最高五分位 **超過 40 bps**，一週超過 **1%**。等權多空組合次日約 **42 bps**（t=28.55）。公開可觀察、用 Lee–Ready 推的「主動買」量，預測力較弱且較快消失；經濟來源是**非公開**的開倉方向，不是市場沒把公開 PCR 讀進去。

來源：[RFS 摘要](https://doi.org/10.1093/rfs/hhj024)；[作者 PDF](https://www.mit.edu/~junpan/volume.pdf)。

**衰退／複製。** 公開 volume PCR 無法重現該效果的核心。後續若只用公開量，不應引用 40 bps／1% 當可達成數字。

**本專案。** **做不到。** yfinance 只有 unsigned volume 與 OI。把 ΔOI 當建倉方向，正是已移除的錯誤路徑。

**導入建議。** 不導入，除非另購帶 open/close 與主動方向的成交資料。

---

## 2. 個股波動 smirk / RR25（Xing, Zhang & Zhao 2010）

**定義。** `SKEW = IV(約 0.95 價外 put) − IV(價平 call)`。市場實務常寫成 25Δ risk reversal：`RR25 = IV(25Δ put) − IV(25Δ call)`。

**預測標的。** 個股後續數週至數月報酬（橫截面）。不是「這檔明天漲跌」的命中率。

**文獻數字。** Xing, Zhang & Zhao, *What Does the Individual Option Volatility Smirk Tell Us About Future Equity Returns?*, JFQA 45(3), 2010。OptionMetrics + CRSP，**1996–2005**。最陡 smirk 五分位相對最平五分位，Fama–French 三因子調整後年化約 **10.9%**（一週持有、跳過一天）。原始週報酬差約 16 bps（t=−2.19）；持有拉長到 4 週，年化 alpha 差降到約 6.52%。樣本中 >90% 觀察有正 smirk，中位 OTM put−ATM call 約 5%。預測至少持續約 6 個月；最陡 smirk 的公司下一季盈餘意外最差。Fama–MacBeth：SKEW 從 25 百分位到 75 百分位，隱含下一週報酬約 −5.52 bps。

來源：[JFQA](https://doi.org/10.1017/s0022109010000220)；[作者 PDF](http://www.ruf.rice.edu/~yxing/option-skew-FINAL.pdf)。

**衰退／複製。**

- 這是 1996–2005 的橫截面多空，不是 8 檔科技股的方向命中率。
- Muravyev, Pearson & Pollet, *Why does options market information predict stock returns?*, Journal of Financial Economics, 2025：IV spread／skew 的可預測性，在排除高借券費股票後 **至少下降三分之二**；與「期權市場有私有訊息」的敘事不完全一致。來源：[JFE](https://doi.org/10.1016/j.jfineco.2025.104153)。
- 工作稿 *Better Opt Out: Revisiting the Predictive Power of Options-implied Signals*（2024）：1996–2008 看起來穩，其後明顯變弱；期權與股票非同步報價造成的前視，把選擇權資料再滯後一天後，2008 年前的預測力也大幅縮小。來源：[SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4766424)。

**本專案。** **特徵可近似，訊號不可直接當建議。** 日鏈可以抽鄰近 0.95 put 與 ATM call 的 yfinance IV，但沒有標準化 30D 25Δ surface；8 檔無法做論文裡的五分位排序；15 個 session 過不了 n≥30。

**導入建議。** 若選，只先當**已觀察 skew 數值**（觀察層），方向政策必須另找大宇集 walk-forward，通過 `validate()` 再談畫面。

---

## 3. Call／Put IV 變化差（An, Ang, Bali & Cakici 2014）

**定義。** 過去一個月 ATM call IV 的變化（ΔCVOL）與 put IV 的變化（ΔPVOL）。Call IV 大升 → 後續報酬高；Put IV 大升（控制 call 後）→ 後續報酬低。

**預測標的。** 個股下一個月報酬（橫截面），效果可延到約 3–6 個月但遞減。

**文獻數字。** An, Ang, Bali & Cakici, *The Joint Cross Section of Stocks and Options*, Journal of Finance 69, 2014（NBER WP 19590）。OptionMetrics 波動曲面。依過去 call IV 變化分十等分，極端組合平均報酬／alpha 約 **每月 1%**。控制 ΔPVOL 後，ΔCVOL 高低組差約 **1.38%/月**（t=5.85）；控制 ΔCVOL 後 ΔPVOL 高低組約 **−1.04%/月**（t=−6.40）。持有第 2 個月降到約 0.63%/月，第 4 個月約 0.34%/月。

來源：[JoF](https://doi.org/10.1111/jofi.12181)；[NBER PDF](https://www.nber.org/system/files/working_papers/w19590/w19590.pdf)。

**衰退／複製。** 同樣是廣宇集月頻橫截面。Muravyev et al.（2025）與 *Better Opt Out*（2024）對「期權 IV 變換可預測股票」整族訊號的質疑一併適用。論文自己也寫：這不是盤中領先，而是月頻。

**本專案。** 需要**跨月**的可比 ATM IV。本機只有約三週日鏈，算不出論文定義的「過去一個月創新」。yfinance IV 也不是 Volatility Surface 的 30D 50Δ。

**導入建議。** 不在現有 8 檔上實作。若選，先用 OptionMetrics 或等價 surface、美股有期權全樣本，月頻 walk-forward。

---

## 4. Put–call parity／IV spread（Cremers & Weinbaum 2010；Bali & Hovakimian 2009）

### 4a. Cremers & Weinbaum（2010）

**定義。** 配對 call 與 put 的 IV 差，衡量偏離 put–call parity。Call 相對貴 → 後續報酬高。

**文獻數字。** *Deviations from Put-Call Parity and Stock Return Predictability*, JFQA 45(2), 2010。相對貴的 call 組合每週勝過相對貴的 put 組合 **50 bps**。兩邊都有異常報酬，不能只用融券限制解釋。期權流動性高、股票流動性低時較強；反向則幾乎沒有。**樣本期內預測力隨時間下降**；作者解讀為早期錯誤定價被套利掉。

來源：[JFQA](https://doi.org/10.1017/s002210901000013x)。

### 4b. Bali & Hovakimian（2009）

**定義。** ATM call IV − ATM put IV（jump-risk proxy）；另有已實現波動 − IV（volatility-risk proxy，方向相反）。

**文獻數字。** *Volatility Spreads and Expected Stock Returns*, Management Science 55(11), 2009。摘要：波動**水準**不能預測橫截面報酬；**價差**可以。call–put IV spread 與預期報酬顯著正相關。Fu, Arisoy, Shackleton & Umutlu (2016, *Journal of Derivatives*) 引用原文表格：等權（市值加權）多空原始月報酬 **1.425%（1.045%）**，t=7.9（4.2）；三因子 alpha **1.486%（1.140%）**。此處數字來自該引用，不是本檔重跑 Bali 原表。

來源：[Management Science](https://doi.org/10.1287/mnsc.1090.1063)；[Fu et al. 引用段](https://eprints.lancs.ac.uk/id/eprint/80351/2/JoD_1_.pdf)。

**衰退／複製。** Cremers & Weinbaum 自己寫預測力下降。Muravyev et al.（2025）把 IV spread 大半歸因於**已知會預測報酬的借券費**。Goncalves-Pinto, Grundy, Hameed, van der Heijden & Zhu, *Why Do Option Prices Predict Stock Returns?*, Management Science 66(9), 2020：期權價可預測股票，原因可以是**股票市場的價格壓力**，不必是期權知情交易。來源：[MS](https://doi.org/10.1287/mnsc.2019.3398)。

**本專案。** 配對同履約、同到期的 call/put IV 在日鏈上**可計算**，但 American 個股選擇權加股利與提早履約，yfinance IV 會把模型誤差算進 spread。8 檔無法複製「流動性高的期權 vs 流動性低的股票」那個交互作用。

**導入建議。** 與 smirk 同一閘門：可當觀察數值；方向建議必須先過大樣本 `validate()`，並控制借券費（本專案目前沒有這項資料）。

---

## 5. 變異數風險溢酬（Bollerslev, Tauchen & Zhou 2009）

**定義。** `VRP = 無模型隱含變異 − 已實現變異`（常見實作：VIX² − 高頻 RV）。高 VRP 預測**高**的後續市場報酬。

**預測標的。** **整體股市**時間序列，不是個股多空。

**文獻數字。** *Expected Stock Returns and Variance Risk Premia*, Review of Financial Studies 22(11), 2009。樣本約 1990–2007。效果在**季頻**最強：單變數迴歸 t=2.86，\(R^2\) **6.82%**。作者強調必須用無模型隱含變異（不是 Black–Scholes 單點）加上**盤中高頻** RV，日頻 RV 不夠。

來源：[RFS](https://doi.org/10.1093/rfs/hhp008)；[作者 PDF](https://public.econ.duke.edu/~get/wpapers/btz.pdf)。

**本專案。** 這是指數層、季頻風險溢酬，不是觀察清單裡 NVDA 明天漲跌。沒有五分鐘 RV，用 ATM IV² 減 20 日日報酬變異，不是論文裡的 VRP。

**導入建議。** 若要做，應做成**大盤風險偏好／區間**觀察（搭配 VIX），不要做成個股看多看空。

---

## 6. 預期波動／ATM 跨式（區間工具，非方向）

**定義。** 本專案已在用：`現價 × ATM IV × √(DTE/365)`，約為風險中性對數常態的 ±1σ。價平跨式權利金是另一個「到期損益兩平寬度」，說明頁已要求不要和 ±1σ 混為一談。

**預測標的。** 「到期前大概會動多少」，**沒有漲跌符號**。

**文獻數字。** 對數常態下 ±1σ 約含 68% 機率——這是分布假設，不是回測命中率。Brenner & Subrahmanyam (1988) 給出 ATM 跨式與 σ 的近似關係；實務 0.85×跨式或 1.25×跨式是同一恆等式的兩種寫法。含入區間的**經驗**頻率取決於變異數風險溢酬：IV 通常高於 RV，所以實際落在 IV 區間內的比例常**高於** 68%。本檔**沒有**找到針對本專案公式、本觀察清單的一手含入率研究；券商部落格的「約 70%」不採用。

TUI 說明目前寫「約 68% 機率落在 ±1σ 內」——應讀成風險中性假設，不是已驗證命中率。

**本專案。** **已在畫面**，且符合「觀察／風險、不是下單指令」。應保留。

**導入建議。** 維持現狀。若要強化，可另做「實現波動 vs 當時預期波動」的校準表（仍非方向建議）。

---

## 7. Dealer gamma／對沖壓力

**定義。** 做市商為維持 delta 中性，在 gamma 為負時必須順勢買賣標的，放大波動；gamma 為正時逆向買賣，壓抑波動。

**文獻。**

- Ni, Pearson & Poteshman, *Stock price clustering on option expiration dates*, Journal of Financial Economics 78(1), 2005：到期日收盤價往履約價聚集；選擇權可交易股票的到期日報酬平均被改變至少 **16.5 bps**。機制是做市商避險再平衡與自營操盤，不是「max pain 命中率」。來源：[JFE](https://doi.org/10.1016/j.jfineco.2004.08.005)；[摘要](https://repository.hkust.edu.hk/ir/Record/1783.1-32183)。
- Baltussen, Da & van Ipperen 等（市場 intraday momentum 與負 gamma）：指數選擇權做市商負 gamma 時，盤中動量較強。這是**盤中指數**現象，需要做市商部位代理變數。來源：[JFE 2021 摘要](https://www.sciencedirect.com/science/article/abs/pii/S0304405X21001598)。

OIC 與既有 `docs/options_model_redesign_research.md` 已寫明：只有總 OI 時，不知道 dealer 是 long 還是 short gamma。

**本專案。** **做不到可辯護的 dealer gamma。** 把 OI 加總成 GEX 並假設「客戶永遠買、dealer 永遠賣」是未經驗證的符號假設。

**導入建議。** 不導入。除非有 signed positioning（例如 OCC 客戶／做市商分類，或可複製的 GEX 方法論並通過 `validate()`）。

---

## 8. 零售工具：unusual options activity、max pain、Cboe PCR

### Unusual options activity（UOA）

商業掃描器對「異常量大、sweep」貼標籤。沒有找到與本專案閘門同定義的一手命中率（期間、宇集、horizon、purge）。Pan & Poteshman 已說明：**公開**量大本身不是他們 40 bps 結果的來源。Johnson & So 等「期權／股票成交量比」是另一條學術線，仍是廣宇集橫截面，不是 UOA 掃描器的廣告勝率。

**導入建議。** 不導入。無一手命中率。

### Max pain

零售定義：使未平倉選擇權到期內在價值總和最小的履約價，並假設現貨會被「吸」過去。沒有找到以該零售規則為準、可引用的期刊命中率。學術上可引用的是上一節 Ni et al.（2005）的**到期日往履約價聚集**，機制是避險與操盤，效果以 **16.5 bps** 計量，不是「收盤會打中 max pain 的百分比」。

**導入建議。** 不導入為方向或價位預測。

### Cboe put/call ratio

Cboe 每日公布 TOTAL / INDEX / ETP / EQUITY 等 put volume ÷ call volume。定義見 [Cboe U.S. Options Daily Market Statistics](https://www.cboe.com/markets/us/options/market-statistics/daily/)。Cboe 說明早期履約會扭曲 equity PCR（[Cboe Insights](https://www.cboe.com/insights/posts/how-early-exercise-order-flow-impacts-equity-option-put-call-ratios/)）。**Cboe 沒有公布「PCR 高則次日上漲」的命中率。** 公開、未分 open/close 的 PCR，正是 Pan & Poteshman 認為較弱的那種訊號。

**導入建議。** 若選，最多當大盤情緒**觀察**（與 VIX 並列），不要當個股看多看空。

---

## 比較表

| 方法 | 預測什麼 | 一手證據（不是命中率則照實） | 發表後打折 | 本專案資料 | 建議 |
|---|---|---|---|---|---|
| 開倉買方 P/C | 個股 1日–1週方向 | 1990–2001；次日 >40 bps、一週 >1% LS | 效果來自非公開 open-buy | 無 | 不導入 |
| IV smirk / RR25 | 個股數週–數月橫截面 | 1996–2005；年化 FF3 ~10.9% LS | 借券費可砍掉 ≥2/3；2008 後變弱；非同步前視 | IV 可近似，n 不夠 | 可觀察；方向須另驗證 |
| Δcall IV − Δput IV | 個股下月橫截面 | 約每月 1% 十等分價差 | 同上整族 IV 訊號 | 無月頻 surface | 不在 8 檔上做 |
| Call–put IV spread | 個股週／月橫截面 | 每週 ~50 bps；月 1.0–1.4%（依論文） | 作者已寫衰退；借券費／股價壓力 | 可算但 IV 品質差 | 可觀察；方向須另驗證 |
| VRP | **大盤**季頻 | 1990–2007；季 \(R^2\) 6.82% | 要無模型 IV + 高頻 RV | 無高頻 RV | 可做大盤觀察，非個股 |
| 預期波動 ±1σ | 區間，無方向 | 理論 ~68%（假設）；無本清單一手含入率 | IV>RV 時含入率常更高 | **已在 TUI** | 維持 |
| Dealer gamma | 盤中波動／到期釘價 | 到期聚集 ≥16.5 bps | 必須有部位符號 | 無 | 不導入 |
| UOA 掃描 | 商業「異常單」 | **無一手命中率** | — | 無逐筆 | 不導入 |
| Max pain | 零售吸價 | **無該規則的期刊命中率**；釘價見 Ni et al. | 常與 GEX 敘事混用 | 僅有 OI | 不導入 |
| Cboe PCR | 大盤情緒 | 官方定義有；**官方無命中率** | 公開 PCR ≠ Pan open-buy | 可從 Cboe 頁抓總量 | 僅觀察，非個股 |

---

## 與現行閘門的對齊

任何被選中的**方向**政策，應：

1. 寫成固定 Policy Version（事前凍結，不在同一份回測上挑 horizon）。
2. 產出 Forecast Record，用 `direction_forecast_validation.validate` 結算。
3. 宇集若只有觀察清單 8 檔，預期仍是 UNDERPOWERED，直到累積足夠不重疊 session，或改用外部大樣本（並誠實標示存活者偏誤）。
4. 通過後才允許 TUI 出現看多／看空。觀察層（樣態、±1σ、Greeks）維持現狀即可，不必等閘門。

---

## 請你勾選（0–N）

回覆時用編號即可，未勾選的不會實作。

- **A.** 維持現狀：TUI 無方向建議；繼續累積日鏈。
- **B.** 觀察層加固定 tenor 的 smirk／RR25／call–put IV spread 數值（仍不寫看多看空）。
- **C.** 用外部大宇集（yfinance 能覆蓋的有期權美股，或你指定的 OptionMetrics）先驗證 smirk 或 IV spread，通過 `validate()` 再決定是否進 TUI。
- **D.** 大盤層：Cboe equity PCR 與／或 VIX–RV 型 VRP 當風險觀察（非個股方向）。
- **E.** 採購 signed flow／借券費／高頻 RV 後再談 Pan 或 dealer gamma。
- **F.** 其他（請寫）。

B／C／D 在 2026-07-23 起近期窗的實測命中率見 `docs/options_bcd_recent_hit_rates.md`。近期全部 UNDERPOWERED；近一年 CBOE SKEW 與 VIX VRP 對 SPY 皆為 FAIL。
