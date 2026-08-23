# 類股方向／輪動演算法：yfinance 可重建方法與十窗驗證設計

日期：2026-08-15  
範圍：只討論可由 yfinance 日線 OHLCV 與目前固定類股成員清單重建、且每個訊號在當日收盤後即可計算的方法。本文是候選方法與預註冊建議，不是回測結果。

## 結論先行

如果現行 `sector_flow` 在十個固定歷史窗都不能得出有效結論，下一輪最值得依序驗證的是：

1. **跨板塊相對動能（首選）**：6 個月與 12 個月報酬、排除最近 1 個月，做跨板塊排名；預測未來 20 個交易日的相對大盤方向。
2. **單板塊時間序列動能**：依板塊本身過去 12 個月（排除最近 1 個月）的正負，預測未來 20 個交易日的絕對方向。
3. **均線趨勢／交易區間突破**：使用預先固定的一組長短窗，不做看完十窗後的 grid search。
4. **動能加 breadth 確認**：只有在前述單一方法先通過之後，才檢驗 breadth 是否有增量價值。

Zweig breadth thrust 可重建，但在只有六個小型板塊籃子時很可能過於稀少；raw-volume confirmation 也不應列入前三順位，因為原始成交量不等於學術研究使用的 turnover。十個年度窗應用來檢查跨市場環境的穩定性，**不能把「7/10 窗成功」直接解讀成已證實 70% 預測力**。

## 資料與公平比較的共同約束

### 固定十個 out-of-sample 視窗

使用與目前研究相同的近十年市場歷史，但切成十個不重疊年度窗：

| Window | 評估區間（左含右不含） | 用途 |
|---|---|---|
| W1 | 2016-08-15 ～ 2017-08-15 | development |
| W2 | 2017-08-15 ～ 2018-08-15 | development |
| W3 | 2018-08-15 ～ 2019-08-15 | development |
| W4 | 2019-08-15 ～ 2020-08-15 | development |
| W5 | 2020-08-15 ～ 2021-08-15 | development |
| W6 | 2021-08-15 ～ 2022-08-15 | development |
| W7 | 2022-08-15 ～ 2023-08-15 | selection |
| W8 | 2023-08-15 ～ 2024-08-15 | selection |
| W9 | 2024-08-15 ～ 2025-08-15 | final holdout |
| W10 | 2025-08-15 ～ 2026-08-15 | final holdout |

為了在 W1 計算 252-session 特徵，下載應至少從 2015-07-01 開始；warm-up 僅用來算訊號，不計入 W1 成績。所有候選演算法必須使用相同的板塊、成員、coverage gate、交易日、forward-return 標籤、成本與 benchmark。W9–W10 在候選與參數固定前不得查看；否則它們不再是 holdout。

這種分層是必要的，因為 White 將「同一資料重複用於推論或模型選擇」明確定義為 data snooping，並指出看似良好的結果可能只是多次嘗試造成的偶然。[White, *A Reality Check for Data Snooping*, Econometrica (2000)](https://doi.org/10.1111/1468-0262.00152)

### yfinance 的固定讀法

- 日線、完整收盤後資料；`start` 是 inclusive、`end` 是 exclusive。
- 價格訊號使用 `auto_adjust=True`，避免股利與拆股造成機械式跳空；成交量仍保留原始 Volume。
- 主結果固定 `repair=False`，另以 `repair=True` 做敏感度檢查；官方文件說明 repair 會嘗試修復 100 倍單位錯誤、缺值、錯誤股利調整等問題。[yfinance `PriceHistory.history()` 原始碼與參數說明](https://github.com/ranaroussi/yfinance/blob/main/yfinance/scrapers/history.py)；[yfinance Price Repair 文件](https://ranaroussi.github.io/yfinance/advanced/price_repair.html)
- yfinance 自己聲明它不是 Yahoo 官方產品、主要供研究教育用途，Yahoo API 資料使用還受 Yahoo 條款限制；因此本資料適合作內部研究，不應被視為正式授權行情源。[yfinance README](https://github.com/ranaroussi/yfinance/blob/main/README.md?plain=1)

### 標籤與評分

對每個板塊在訊號日 `t` 計算：

```text
absolute_return_h = sector_price[t+h] / sector_price[t] - 1
relative_return_h = (1 + absolute_return_h) / (1 + benchmark_return_h) - 1
hit = predicted_sign == sign(target_return)
```

- **板塊輪動方法**以 `relative_return_20` 為主要標籤，因為普遍多頭行情會讓「永遠猜漲」在 absolute accuracy 上虛高。
- **單板塊方向方法**以 `absolute_return_20` 為主要標籤，但必須同時報告 majority-class/no-skill accuracy。
- 5 與 10 sessions 可做次要 horizon；不能在看結果後挑其中最高者作正式結論。
- 同一訊號持續多天時只取 episode 起點，下一筆至少相隔 `h` 個交易日；同日六板塊結果以同一 date block 處理，避免把共同市場衝擊當成六個獨立觀察。
- 多空分開報告 hit rate、balanced accuracy、coverage、平均 forward return、扣除 10 bps 後報酬，以及相對簡單 momentum baseline 的 paired improvement。

## 候選方法

### A. 跨板塊相對動能（優先級 1）

**固定定義**

```text
r6_i  = sector_i[t-21] / sector_i[t-126-21] - 1
r12_i = sector_i[t-21] / sector_i[t-252-21] - 1
z6_i, z12_i = 當日六板塊橫截面 z-score
score_i = 0.5 * z6_i + 0.5 * z12_i

top 2    -> predict relative up for next 20 sessions
bottom 2 -> predict relative down for next 20 sessions
middle 2 -> abstain
```

如要採風險調整版本，可在實驗前固定以歷史日報酬波動率縮放；不得在結果出來後才選 raw 或 risk-adjusted 版本。MSCI 官方 momentum 方法把排除最近一個月的 6 個月與 12 個月價格動能各占一半，再以波動率調整並標準化；這提供了可稽核、無前視的公式來源。[MSCI Momentum Indexes Methodology, §2.2](https://www.msci.com/indexes/documents/methodology/2_MSCI_Momentum_Indexes_Methodology_20231120.pdf)

此方法也直接對應產業動能的原始研究：Moskowitz 與 Grinblatt 以 past-winning industries 對 past-losing industries 建立策略，發現產業成分可解釋相當一部分個股 momentum。[Moskowitz & Grinblatt, *Do Industries Explain Momentum?*, Journal of Finance (1999)](https://onlinelibrary.wiley.com/doi/pdf/10.1111/0022-1082.00146)

**合理 horizon**：形成期為 6–12 個月，故以 20 sessions 為主要持有期，比 1–5 日更符合來源方法的中期延續假說。原始 winners/losers 研究報告 3–12 個月形成與持有期的延續，不支持把它任意縮成隔日預測。[Jegadeesh & Titman, *Returns to Buying Winners and Selling Losers* (1993), DOI](https://doi.org/10.1111/j.1540-6261.1993.tb04702.x)

**十窗適用性**：高。每天均可排名、每窗有足夠 episode；但必須 purge 20-session 重疊，並用六板塊 macro-average 防止某一板塊支配結果。

### B. 單板塊時間序列動能（優先級 2）

**固定定義**

```text
mom_i = sector_i[t-21] / sector_i[t-252-21] - 1
mom_i > 0 -> predict absolute up for next 20 sessions
mom_i < 0 -> predict absolute down for next 20 sessions
```

可另加「相對 benchmark 的 mom 正負」作預先註冊的 secondary variant，但不能與 absolute 版本混合挑勝者。Moskowitz、Ooi、Pedersen 的原始 time-series momentum 研究在股票指數、匯率、商品與債券期貨上觀察到 1–12 個月的報酬持續、較長期部分反轉；它支持中期方向訊號，但並未證明目前這六個自訂籃子的 5 日預測。[Moskowitz, Ooi & Pedersen, *Time Series Momentum* (2012)](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID2089463_code753937.pdf?abstractid=2089463&mirid=1)

**合理 horizon**：20 sessions primary；5/10 sessions secondary。  
**十窗適用性**：高，公式單純、訊號密度充足；限制是多頭樣本下 up 比例高，必須和「永遠猜多」及當期 base rate 比較。

### C. 均線趨勢與交易區間突破（優先級 3）

Brock、Lakonishok、LeBaron 檢驗的是兩類最簡單且常見的技術規則：moving-average 與 trading-range break，並使用 bootstrap 比較多種報酬 null model。[Brock, Lakonishok & LeBaron, *Simple Technical Trading Rules and the Stochastic Properties of Stock Returns* (1992)](https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.1992.tb04681.x)

為避免產生大量參數組合，專案只應預先挑一個版本：

```text
trend:
  SMA5 > SMA150 -> predict up
  SMA5 < SMA150 -> predict down

breakout:
  close[t] > max(close[t-50:t]) -> predict up
  close[t] < min(close[t-50:t]) -> predict down
  otherwise -> abstain
```

trend 與 breakout 應視為兩個候選，不可事後 OR 在一起增加 hit 數。  
**合理 horizon**：20 sessions primary。  
**十窗適用性**：trend 高；breakout 中等，因訊號較少。每窗至少要揭露 episode 數與 coverage。

### D. Breadth 與 breadth thrust（只作增量／事件驗證）

現行 `(up-down)/eligible` breadth 可繼續作候選特徵，但既有 `3-of-5` 規則若已失敗，不應只微調 0.5、3、5 等門檻再稱為新方法。較可解釋的替代是先讓 price-momentum 或 trend 產生方向，再要求成員確認：

```text
pct_above_50dma = count(member_close > member_SMA50) / eligible
up confirmation   = base predicts up   and pct_above_50dma >= 0.60
down confirmation = base predicts down and pct_above_50dma <= 0.40
```

0.60/0.40 是待預註冊的對稱工程門檻，不是文獻已證明的最佳值；真正問題應是「breadth gate 是否改善 base method 的 paired accuracy／return」，而不是單獨挑一個漂亮 hit rate。

經典 Zweig Breadth Thrust 的常見定義，是 10 日期間內，advancing issues 比率的平均值由 40% 以下上升至 61.5% 以上。[StockCharts ChartSchool glossary](https://chartschool.stockcharts.com/table-of-contents/glossary/glossary-b) 這個來源是專業技術分析文件而非原始論文，因此只能支持重建定義，不能支持預測效力。此訊號原本面向廣泛交易所 universe；套到每組僅數檔成員的自訂類股籃子，分母過小、訊號會跳躍且事件稀少。

**合理 horizon**：10–20 sessions 的事件研究。  
**十窗適用性**：低。若多數年度窗零訊號，就應判定「資料不足／不適用」，不可用有訊號的少數年份宣稱成功。

### E. 量價確認（探索性，不列前三）

Lee 與 Swaminathan 的原始研究發現過去 trading volume 與 momentum 的幅度、持續及長期反轉時間有關；但研究使用的是 turnover／排序脈絡，不等同於「今日 raw volume 大於 20 日平均就確認上漲」。[Lee & Swaminathan, *Price Momentum and Trading Volume*, Journal of Finance (2000)](https://doi.org/10.1111/0022-1082.00280)

yfinance OHLCV 只有 raw Volume，無法可靠取得每一歷史日的 point-in-time shares outstanding，因此不能無前視地重建 turnover。ETF volume 又會受基金規模與交易熱度長期漂移影響。若仍要探索，應固定為：sector ETF 當日 dollar-volume 的 60-session rolling percentile > 80%，且價格動能同向；只檢查它相對純動能是否有 paired improvement。

**合理 horizon**：5–20 sessions secondary。  
**十窗適用性**：中低；只用作增量測試，不應成為第一個替代演算法。

### F. 多訊號組合（最後才測）

只有 A–D 各自完成相同十窗測試後，才可預先固定一個簡單組合，例如：

```text
relative momentum、time-series momentum、SMA trend 三者中至少 2 個同向
否則 abstain
```

組合不得使用十窗結果最佳化權重或門檻。技術形態的系統化研究顯示部分形態可能含有增量資訊，但也強調應以客觀演算法取代主觀辨識。[Lo, Mamaysky & Wang, *Foundations of Technical Analysis* (NBER Working Paper 7613, 2000)](https://business.columbia.edu/sites/default/files-efs/pubfiles/19268/Lo-Mamaysky_wang_foundations.pdf) 每多試一個 variant 都增加 data-snooping 風險，因此需要 family-wise bootstrap／Reality Check，而不是只報最佳模型。

## 「預測成功 >60%」應怎麼判定

### 十個窗不是十次足以定案的獨立試驗

NIST 對 binomial distribution 的定義要求固定成功機率與相互獨立的 Bernoulli trials。[NIST/SEMATECH Binomial Distribution](https://itl.nist.gov/div898/handbook/eda/section3/eda366i.htm) 年度股票報酬會共享 regime、訊號也會跨窗自相關，因此十個窗甚至比十個獨立投擲更不理想。

即使暫時假設十窗獨立且無技巧基準為 50%：

- `7/10 = 70%` 已符合「>60%」，但單尾 `P(X >= 7 | p=0.5) = 17.19%`，遠高於 5%。
- `8/10 = 80%` 的單尾機率仍為 5.47%。
- 至少 `9/10` 才低於 5%（1.07%）。

所以不能只用「十窗中七窗命中」作套用 TUI 的門檻。十窗的用途是看 regime stability；統計推論應使用窗內經 purge 的 episode，並以 date/window cluster bootstrap 保留相依性。

### 建議的正式通過條件

候選演算法只有同時滿足以下條件，才可稱為「可考慮套用」：

1. W9–W10 合併、purged episodes 的主要 horizon **balanced accuracy > 60%**。
2. window/date-cluster bootstrap 的 95% CI 下界 > 50%，而非只有點估計 >60%。
3. 相較 majority/no-skill 與現行 `sector_flow` 的 paired improvement CI 下界 > 0。
4. up 與 down 各自至少有預先設定的最低樣本數；建議 final holdout 各方向至少 50 個不重疊 episode，否則結論標示 `insufficient`。
5. 至少 8/10 個年度窗的效果方向不劣於 no-skill，且 final W9、W10 不可一正一負到完全抵銷；此條是穩定性 gate，不取代第 2 點統計檢定。
6. 扣除 10 bps 後平均 signed/relative return 為正，並報 25 bps stress。
7. 多候選、多 horizon 結果必須做 family-wise 修正或 Reality Check；不能從同一組結果中挑最高 accuracy 當唯一結論。
8. coverage 必須揭露；高 accuracy 但只在極少數日期出訊號，不等同於可用的 TUI 判斷器。

若產品需求堅持「點估計 >60% 就套用」，最少也應把 TUI 文案限制為實驗性提示，展示樣本數與信賴區間，不能顯示確定性的「共同買進／共同賣出」。

## 重要限制

1. **Survivorship／成員歷史**：用今日成員清單回填十年，只能回答「今天這籃子若回看過去如何」，不能重建當時可投資的歷史 index universe。若這些是刻意固定的 thematic baskets，可接受為條件式研究；若宣稱正式產業指數，則需要 point-in-time constituents。
2. **小型籃子 breadth 不穩定**：少一檔就會大幅改變 breadth；coverage gate 應至少 80%，且不得以 0 補缺值。
3. **基準選擇**：科技導向籃子可用 QQQ，跨產業更應同時報 SPY；主基準必須事前固定。
4. **調整價格與成交量語意不同**：adjusted price 適合 total-return 方向，raw volume 不能因價格調整而自動成為可比 turnover。
5. **短 horizon 噪音**：本文最強的一手依據集中在 1–12 個月 momentum 或較長均線規則；如果產品只接受 1–5 日預測，所有候選都應降低先驗信心，不能借用中期文獻替短期結果背書。

## 建議實驗順序

1. 在固定十窗重跑現行 `sector_flow`，作唯一 legacy baseline。
2. 跑 A（跨板塊相對動能），不調參。
3. 若 A 未通過，跑 B（時間序列動能）。
4. 若 B 未通過，分別跑 C 的 trend 與 breakout，並套多重比較修正。
5. 只有 base method 接近或通過時，才跑 D breadth gate 的 paired incremental test。
6. E volume confirmation 僅列探索性；F ensemble 最後測，且須另留新資料確認。
7. 任一方法若在 W9–W10 滿足全部正式通過條件，才撰寫 TUI 變更；若只是 pooled accuracy >60% 而 CI、方向、coverage 或成本失敗，仍維持舊邏輯停用或描述性呈現。

這個順序讓每一個後續方法都在完全相同的市場樣本與評分規則下競爭，同時避免把十年歷史無限重用成「直到找到 60%」的搜尋程序。
