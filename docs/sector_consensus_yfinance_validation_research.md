# `sector_flow` 類股共識：yfinance 歷史驗證研究與預註冊範圍

日期：2026-08-15  
範圍：`sector_analysis.detect_broad_flow()`、`backtest_sector_flow()`、
`SectorAnalysisScreen._render_conclusions()`；只規劃與驗證「每日累計廣度」這條
`sector_flow`，不把 `sector_predictive` 的 1–3 日條件模型混入同一實驗。

## 結論摘要

應採用使用者建議：**不要再用系統逐日累積的短歷史判斷有沒有預測力，改由
yfinance 下載多年日線，先鎖定實驗規格，再做一次可重現的歷史驗證。**

不過，直接下載日線只能無歧義地重建「成分股漲跌廣度」與調整後價格報酬，不能把
今天的 `fast_info.market_cap` 倒灌到過去，假裝是歷史市值權重。yfinance 1.4.1 的
`FastInfo.market_cap` 是以最近可得股數乘上目前價格；`FastInfo.shares` 本身取最近
548 日中的最後一筆股數，不是逐日 point-in-time 市值。官方原始碼見
[quote.py `shares` 與 `market_cap`](https://github.com/ranaroussi/yfinance/blob/1.4.1/yfinance/scrapers/quote.py#L231-L243)、
[market-cap 計算](https://github.com/ranaroussi/yfinance/blob/1.4.1/yfinance/scrapers/quote.py#L458-L481)。

因此建議同時產生兩份、但用途不同的結果：

1. **literal replication（相容性診斷）**：盡量照現行參數與資料語意重建，目的為回答
   「如果歷史上每天都有當時快照，訊號大致會長什麼樣」；結果不得直接升級成投資訊號。
2. **decision-grade validation（決策級驗證）**：使用可重現的等權主規格、交易日 horizon、
   完整 session/coverage 防線、真正未看的 holdout 與日期群聚推論；只有這條通過預註冊
   門檻，UI 才能從「狀態描述」升級為「具有預測證據」。

若決策級驗證失敗，現行共識仍可保留為「近五日類股同步／趨勢確認」，但不應顯示
「共同買進／共同賣出」或多空投資建議。

## 1. 現行 source of truth

### 1.1 訊號公式

現行單一成分股日報酬 `r[i,t]` 來自 `auto_adjust=True` 的相鄰兩根日線 Close；抓取端
再將它乘 100、四捨五入到小數點二位，寫成 `day_pct`。來源：
[quotes.py](../assettrack/quotes.py#L1531-L1690)。

對板塊 `g` 的日期 `t`：

```text
up[t]      = count(r[i,t] > 0)
down[t]    = count(r[i,t] < 0)
rated[t]   = count(r[i,t] is not None)       # r = 0 會進分母，但不算漲或跌
breadth[t] = (up[t] - down[t]) / rated[t]
```

程式會把 `breadth` 四捨五入到小數點三位；公式見
[sector_analysis.py `_breadth`](../assettrack/sector_analysis.py#L113-L121)。

當日狀態為：

```text
broad_up[t]   = breadth[t] >= +0.5 and weighted_return[t] > +0.1%
broad_down[t] = breadth[t] <= -0.5 and weighted_return[t] < -0.1%
```

最近五筆 history row 中至少三筆為 `broad_up`，方向即為 `up`；至少三筆為
`broad_down`，方向即為 `down`；否則 `none`。這是「5 筆中 3 筆」，不是要求連續三日，
也沒有要求最新一日仍同向。完整條件見
[sector_analysis.py `detect_broad_flow`](../assettrack/sector_analysis.py#L195-L249)。

`_render_conclusions()` 自己不計算訊號；它把先前算好的 flow 傳給
`generate_sector_recommendations()`，再與另一套 `sector_predictive` 推薦合併。
來源：[tui.py](../assettrack/tui.py#L7693-L7738)。本驗證必須只評估 `sector_flow`，否則
不能知道哪套邏輯造成結果。

### 1.2 權重的真實行為

即時摘要會以 `fast_info.market_cap` 加 FX 換成 USD 後加權；若跨幣別但缺 FX，退回
等權。`_cap_weighted()` 只在具有 `day_pct` 的權重內重新正規化。來源：
[sector_analysis.py](../assettrack/sector_analysis.py#L60-L110)。

但歷史 `compute_breadth_history()` 呼叫 `_cap_weighted(members, "day_pct")` 時**沒有傳
FX**。結果是：

- 純 USD 板塊通常使用各快照內的市值權重；
- 只要該日有具市值的非 USD 成分股，就整個板塊退回等權；
- 某檔只有報酬、沒有市值時，不一定觸發等權；其報酬可能直接被市值加權計算忽略。

來源：[歷史計算](../assettrack/sector_analysis.py#L176-L192) 與
[權重 fallback](../assettrack/sector_analysis.py#L71-L110)。所以現行六個預設板塊並非
使用單一一致的「市值加權」制度；預設 universe 也同時包含美國、德國與韓國上市股票，
見 [storage.py](../assettrack/storage.py#L920-L936)。

### 1.3 現行 backtest 不能直接沿用的地方

現行 `backtest_sector_flow()` 的 `h` 是**日曆日**：它先算 `T + timedelta(days=h)`，
再找第一個不早於該日期的 snapshot；並不是第 `h` 個交易 session。來源：
[sector_analysis.py](../assettrack/sector_analysis.py#L382-L414)。新驗證必須改用精確的
session offset，否則「5 日」會依星期與假日變成 3–5 個交易日。

現行統計層用 `floor(n / horizon)` 與 distinct signal dates 壓低 ESS，再做 Wilson CI、
單尾二項檢定和 Bonferroni；這比直接把所有群組／日期當獨立樣本保守，但仍沒有處理
「訊號持續多日、不同板塊同日高度相關、forward return 重疊」的完整群聚結構。來源：
[backtest_stats.py](../assettrack/backtest_stats.py#L72-L129)、
[attach_significance](../assettrack/backtest_stats.py#L190-L229)。決策級驗證應直接使用
日期 block bootstrap／日期群聚標準誤，而不是先算大量重疊樣本再用規則縮小 `n`。

## 2. yfinance 官方資料語意與抓取規格

### 2.1 固定呼叫參數

yfinance 官方文件說明：`start` 為 inclusive、`end` 為 exclusive；`auto_adjust=True`
會調整全部 OHLC；`repair=False` 與 `keepna=False` 是預設；日線合併多時區時
`ignore_tz` 預設為 True。來源：
[yfinance.download API](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)；
對應 1.4.1 原始碼見
[multi.py](https://github.com/ranaroussi/yfinance/blob/1.4.1/yfinance/multi.py#L54-L108)。

驗證程式不得依賴這些預設值，應完整寫出並存入 manifest：

```python
yf.download(
    tickers=...,
    start="2016-01-01",
    end="<最後完整 session 的下一個日曆日>",
    interval="1d",
    auto_adjust=True,
    actions=True,
    repair=False,          # replication dataset
    keepna=True,           # 先保留缺值，讓品質檢查看得見
    prepost=False,
    ignore_tz=False,
    group_by="ticker",
    multi_level_index=True,
    threads=False,         # 基準執行追求可重現；效能版另做一致性測試
    progress=False,
)
```

`auto_adjust` 在官方原始碼中以 `Adj Close / Close` 比率縮放 O/H/L，並以 `Adj Close`
取代 `Close`；所以 AssetTrack 用到的 Close 已不是原始未調整收盤價。來源：
[yfinance 1.4.1 `auto_adjust`](https://github.com/ranaroussi/yfinance/blob/1.4.1/yfinance/utils.py#L496-L513)。

官方 price-repair 文件說 `repair=True` 會嘗試修復 100 倍幣別錯誤、缺資料、錯誤
split／dividend adjustment 等問題，並新增 `Repaired?` 欄位；它是修復演算法，不是
原始資料真值。來源：[Price Repair](https://ranaroussi.github.io/yfinance/advanced/price_repair.html)。
因此需要兩次凍結下載：

- `repair=False`：重現現行 AssetTrack 口徑的主 replication dataset；
- `repair=True`：資料品質敏感度資料集；若核心結論因 repair 開關翻向，視為**資料不穩定、
  不通過**，而不是挑較好看的版本。

目前專案只宣告 `yfinance>=0.2.40`，沒有上限或 exact pin，見
[pyproject.toml](../pyproject.toml#L13-L21)。正式驗證需鎖 exact version，manifest 記錄
Python、pandas、yfinance 版本與下載 UTC timestamp；升級套件後不能覆寫舊原始檔，必須
另建 dataset version。

### 2.2 原始資料保存與 fail-closed

目前即時抓取會壓掉 yfinance log，並以多層 `except Exception: continue` 接受部分 ticker
缺失，見 [quotes.py](../assettrack/quotes.py#L1531-L1690)。驗證程式不可沿用這種
best-effort 行為，應：

1. 保存每次原始 DataFrame、actions、metadata 與 ticker-level error；不把原始資料寫回
   系統 sector snapshot。
2. 保存 universe manifest：板塊名稱、symbol、Yahoo symbol、幣別、exchange、timezone、
   首末 bar、row count、缺值率、重複日期、修復列數、SHA-256。
3. batch 缺 ticker 時，以單 ticker 重試；仍失敗就讓該 ticker/day 明確缺值，不可靜默
   變成 0% 報酬。
4. 用 `keepna=True` 先看見 Yahoo 回傳的 NaN／zero-only rows；官方原始碼顯示
   `keepna=False` 會刪除全 NaN 或全零列，來源：
   [history.py](https://github.com/ranaroussi/yfinance/blob/1.4.1/yfinance/scrapers/history.py#L518-L525)。
5. 對每個 symbol 驗證日期單調、唯一、`High >= max(Open, Close)`、
   `Low <= min(Open, Close)`、價格為正；極端單日報酬（建議 `abs(r)>40%`）不得自動刪除，
   要和 split/dividend/actions、`repair=True` 結果交叉檢查後標記。

### 2.3 使用限制

yfinance 官方 README 明確表示它不受 Yahoo 背書，工具供研究／教育使用，實際下載資料的
權利需依 Yahoo 條款，並提醒 Yahoo Finance API 只供個人使用。來源：
[yfinance README](https://github.com/ranaroussi/yfinance/blob/main/README.md)。本驗證適合作為
個人研究與產品內部判斷，不應把下載的完整歷史資料隨報告一起公開散布。

## 3. 預註冊驗證範圍

### 3.1 Universe 與期間

1. 在第一次下載前，從使用者目前 sector-group 設定產生不可變 universe manifest；只把
   group membership 當設定讀取，**不讀任何既有 sector snapshot、命中率或歷史結論**。
2. 套用現行 `.TW/.TWO` 排除規則；同一 symbol 可屬多個板塊，但統計推論需在日期層群聚，
   不可把它當獨立樣本。
3. 下載 `2016-01-01` 至最後一個已完整收盤的 NYSE session，目標約十年；每個板塊的
   有效起點由成分股 coverage 決定，不因想要較長樣本就補零或回填 IPO 前資料。
4. universe 是 2026 年凍結的 curated 清單；這只能回答「今天這組股票過去曾否出現可用
   規律」，不能消除 delisting／survivorship 與成分選擇偏誤。完整 point-in-time
   membership 不在 yfinance 價格 API 的能力內，必須把此限制寫進最終結論。

建議切分（在看結果前固定）：

| 區段 | 日期 | 可做的事 |
|---|---|---|
| development | 2016-01-01–2020-12-31 | 只修資料解析、session 對齊、品質 gate；不得挑參數讓績效變好 |
| validation | 2021-01-01–2023-12-31 | 驗證實作與預先登記指標，允許發現 bug 後重跑但必須留 audit log |
| final holdout | 2024-01-01–最新完整 session | 規格與程式 hash 鎖定後只開封一次，作 go/no-go 判定 |

若某板塊因 IPO（例如新上市成分股）直到很晚才達 coverage，不能把 development／validation
硬補成可用；該板塊可以被判為「歷史不足」，不應和其他板塊池化後掩蓋。

### 3.2 Session 與跨市場對齊

現行預設板塊含美國、德國、韓國股票，但系統以 NYSE session 為 sector refresh 主鍵，
見 [storage.py](../assettrack/storage.py#L1002-L1009)。直接把不同交易所的 `YYYY-MM-DD`
outer join，會忽略時區、各國假日與「在美股收盤時是否已知」。

主規格以每個 NYSE regular-session close 為 decision timestamp：

1. 對每個外國 symbol，只採用在該 NYSE close 前已完成的最新本地 session bar。
2. 該 bar 的 `day_pct` 是它相對前一個**本地交易 session**的報酬。
3. 同一外國 bar 不可因當地休市而在兩個 NYSE session 重複投票；沒有新本地 bar即視為
   missing，不是 0%。
4. 每個板塊／NYSE session 至少 `max(4, ceil(0.8 * frozen_member_count))` 檔有 fresh return，
   才是 eligible day；否則整個板塊當日不進五日 window。
5. `lookback=5` 指五個 eligible NYSE sessions，不是五個日曆日，也不是含缺資料的五列。

另輸出「US-listed-only」robustness run。若跨市場版本與美股版本方向相反，先判定
時間對齊／FX 暴露敏感，不得直接宣稱類股邏輯有效。

### 3.3 報酬與權重

成分股 local-price return 保留現行語意：

```text
r[i,t] = 100 * (AdjustedClose[i,t] / AdjustedClose[i,t-1] - 1)
```

現行程式在算 `day_pct` 時先四捨五入到 0.01%；literal replication 要照做，
decision-grade 主規格則保留 full precision 到最後才格式化，並把「先 rounding」列為
敏感度測試。

權重分三層，不能混報：

| 標籤 | 作法 | 用途 |
|---|---|---|
| `equal_weight_primary` | eligible 成分股每日等權、缺值後重新正規化 | 決策級主規格；完全由日線可重現，沒有 current-cap 前視偏誤 |
| `historical_cap_experimental` | `get_shares_full(start,end)` 的可得股數以前值填到下一公告點，乘當日未調整 Close，再用當日 FX 換 USD | 只有股數/FX coverage ≥95% 的板塊／期間才報；不得把缺值補成今天市值 |
| `current_cap_counterfactual` | 今天市值固定套到全歷史 | 只量化「錯誤倒灌會差多少」，明確標成 biased，不納入 go/no-go |

yfinance 的 `get_shares_full(start,end)` 公開方法存在，但官方 API 頁沒有承諾歷史完整度；
其 1.4.1 原始碼只是向 Yahoo fundamentals-timeseries endpoint 要指定區間，缺資料會回 None。
來源：[API](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.get_shares_full.html)、
[base.py](https://github.com/ranaroussi/yfinance/blob/1.4.1/yfinance/base.py#L481-L536)。所以歷史市值
只能是 coverage 通過後的次要實驗，不能是預設真值。

對跨幣別股票，訊號重現使用現行 local-price return；另做 USD-unhedged return robustness
（將價格乘同日可得 FX）。因目前程式只用 FX 正規化市值權重、沒有把報酬本身換成 USD，
兩者必須分開命名。

### 3.4 鎖定訊號，不先調參

confirmatory run 鎖定：

```text
lookback          = 5 eligible sessions
min_days          = 3
breadth_threshold = 0.5
return_threshold  = 0.1 percentage point
direction         = up / down / none，完全照 detect_broad_flow
```

不可先 grid-search `breadth_threshold`、`min_days`、`return_threshold` 再回頭把最佳組合稱為
驗證。任何參數探索都只能在 holdout 開封後另列「hypothesis generation」，並需要新的
未來資料或至少 60 個交易日 shadow-forward 才能再確認。

### 3.5 Forward label 與基準

訊號使用 session `T` 的完整收盤資料，因此預測 label 必須從 `T` 之後開始：

- forecast endpoint：`T` adjusted close 至第 `h` 個後續 NYSE session close；若該日 outcome
  coverage 不足，該 label 記為缺值，不得順延到更晚的「下一個可用日」；
- implementable endpoint：下一 NYSE session open 至第 `h` 個 session close，另扣 round-trip
  10 bps 與 25 bps stress cost；
- horizons：`h=5` 是唯一 primary，`h=1,10` 為 secondary；現行 `14,30,60` 只探索性呈現，
  不拿來挑「最好看的前瞻期」。

每個訊號至少計算：

```text
absolute_return      = future group return
signed_return        = +absolute_return for up, -absolute_return for down
SPY_excess           = group return - SPY return
QQQ_excess           = group return - QQQ return  # 科技偏重 universe 的 robustness
direction_hit        = signed_return > 0
```

基準須在相同 eligible dates 上比較：

1. 無條件同方向率／平均報酬（只能使用該 fold 過去資料估計）；
2. 最近五日等權板塊報酬的單純 momentum sign；
3. 永遠做多板塊；
4. SPY、QQQ buy-and-hold 與超額報酬。

這能區分「訊號真的增加資訊」與「科技股長期本來就常上漲」。

## 4. 統計設計與通過門檻

### 4.1 觀測單位

畫面方向可連續數日不變，不能把每天都當全新獨立預測。報告同時提供：

- raw signal-days；
- signal episodes（`none→up/down`，或方向翻轉才開新 episode）；
- purged samples：同一板塊同一 horizon 的 label interval 不重疊；
- distinct signal dates 與每方向、每板塊數量。

主推論使用 purged samples，並以日期為共同 cluster，讓同日六個板塊不會被當六次獨立
市場事件。95% CI 使用 moving-block bootstrap，block length 至少
`max(20 sessions, horizon)`；整個日期 block 要連同所有板塊一起抽樣。

### 4.2 Primary hypothesis

唯一主要假設：在 final holdout，`equal_weight_primary` 的 5-session signal episode
具有正的平均 `signed_return`，並且相對「五日 momentum sign」有正的 paired improvement。

方向命中率、up/down 分開、SPY/QQQ excess、1/10 日 horizon 都是 secondary。Secondary
family 以 Bonferroni 控制 family-wise alpha 0.05；探索性的 14/30/60 日只列 effect size
與 CI，不給「通過」標章。

### 4.3 升級為預測訊號的最低門檻

全部同時成立才算 `PASS`：

1. final holdout 至少 50 個 purged 5-session episodes，且 up/down 若要各自顯示投資方向，
   該方向至少 30 個 episodes；不足只回 `INSUFFICIENT_DATA`，不能回成功或失敗。
2. primary 5-session `signed_return` 的日期-block-bootstrap 95% CI 下界 > 0。
3. 相對五日 momentum 的 paired improvement 95% CI 下界 > 0。
4. 方向命中率相對 rolling unconditional baseline 的 edge > 0，secondary 多重檢定調整後
   仍顯著；命中率本身高於 50% 不足以通過。
5. gross 與 25 bps stress-cost 的平均 signed implementable return 都 > 0。
6. development、validation、holdout 三段 effect 同號；按年或 rolling-year folds 至少 70%
   同號。
7. leave-one-sector-out 後結論不翻負，且單一板塊不得貢獻超過總 signed return 的 50%。
8. `repair=False/True`、full precision/先 rounding、all-market/US-only robustness 不得使主 effect
   翻向；若翻向，回 `DATA_SENSITIVE`。
9. 每個板塊 coverage gate、missingness 與訊號率均揭露；結果不能只靠低 coverage 的極端
   breadth 產生。

若只有 up 或 down 通過，只能升級該方向；另一方向仍應 abstain。若 absolute return 通過
但 SPY/QQQ excess 不通過，最多稱為「市場趨勢確認」，不能稱為板塊選擇 alpha。

## 5. 必須揭露的偏誤與限制

| 風險 | 為何會誤導 | 防線／結論限制 |
|---|---|---|
| 今日 universe 回看歷史 | 存活者、成功公司與今日主題定義已知未來 | 凍結 manifest；結論只適用「今日名單的反事實歷史」；未來開始保存 membership version |
| point-in-time 市值缺失 | 今天市值倒灌會讓後來勝出的大公司在早年權重過大 | 等權作 primary；歷史股數 coverage 不足就不報 cap-weighted |
| 調整後資料會修訂 | Yahoo 可能回溯修正 corporate actions／價格 | 保存 raw dataset hash 與版本；repair on/off 必須同向 |
| 跨市場不同收盤時間 | 韓國、德國、美國同名日期不是同一資訊截止點 | 以 NYSE decision timestamp 做 as-of join；stale bar 不重複投票 |
| IPO／下市缺值 | 低 coverage 可讓少數股票產生 ±1 breadth | 80% 且至少 4 檔 fresh-return gate；板塊分開報告 |
| 訊號持續與 horizon 重疊 | raw n 遠大於獨立事件數 | episode、purge、date-cluster block bootstrap |
| 多板塊共用股票 | NVDA 等同日被重複計成多次證據 | 日期 cluster；leave-one-sector-out；顯示 unique-symbol exposure |
| 市場 beta | 科技多頭期「up」很容易命中 | 同日 SPY/QQQ excess、momentum 與 always-long 基準 |
| 收盤後才知道訊號 | 用 T 收盤成交會有執行前視 | forecast 與 next-open implementable endpoint 分開 |
| yfinance 部分失敗 | 現行 broad exception 會靜默縮小分母 | fail-closed、ticker error manifest、coverage gate |
| 反覆調參／挑 horizon | 一定能挑到偶然漂亮的組合 | 5 日單一 primary、holdout 開封一次、secondary Bonferroni |

## 6. 最終報告與可重現產物

驗證完成時至少輸出：

1. `dataset_manifest.json`：版本、下載參數、時間、universe、每 ticker 品質統計與 hash；
2. immutable raw yfinance files（replication／repair 各一版）；
3. normalized per-symbol bars、session alignment audit、coverage matrix；
4. per-group daily features：full-precision return、breadth、eligible count、weight mode；
5. per-signal records：signal timestamp、五日 window、方向、episode id、所有 forward labels；
6. report：每板塊與 pooled effect、raw n／episode n／purged n、CI、p-value、baseline、
   transaction-cost sensitivity、leave-one-sector-out、各年度 fold；
7. machine-readable verdict：`PASS`、`FAIL`、`INSUFFICIENT_DATA` 或 `DATA_SENSITIVE`，以及逐條
   gate 的通過狀態；
8. 執行程式 git commit、設定檔 hash、dataset hash，確保同資料可重跑得到相同數字。

在上述產物完成前，不應修改 `_render_conclusions()` 的投資語氣或把 yfinance retrospective
結果接進正式 calibration 狀態；驗證工具應先是獨立、只讀的 research pipeline。

## 7. 建議的實作順序

1. **Freeze**：匯出 sector group manifest、參數與此預註冊規格 hash。
2. **Fetch**：直接向 yfinance 下載 2016 至今 prices/actions/metadata；保存 repair off/on。
3. **Audit**：完成 ticker、時區、session、corporate action、missingness、coverage 報告；品質
   gate 未過就停止，不計績效。
4. **Reconstruct**：先做 literal replication 差異表，再建 equal-weight decision-grade series。
5. **Validate code**：只在 development／validation 區間測試，鎖定程式 commit 與 config。
6. **Open holdout once**：產生 primary/secondary/robustness 結果與 machine verdict。
7. **Shadow-forward**：即使歷史 `PASS`，仍至少跑 60 個新交易 sessions；只有歷史與 shadow
   都通過，才將文案升級為可操作建議。

這個順序能直接回答使用者真正關心的問題：不是「舊快照回測看起來幾成」，而是
**在不倒灌市值、不混用日曆日、不重複計算同一市場事件、且保留真正未看資料的前提下，
現行 5 日／3 日類股共識是否仍比單純動能與市場基準更有預測意義。**
