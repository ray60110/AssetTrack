# 使用者績效追蹤

## 目的

績效追蹤比較「使用者完整資產」與承受相同外部資金流的 QQQ／VT，避免入金看似獲利、出金看似虧損。所有證券部位與現金都必須納入估值。

## 資料契約

每位使用者使用 `data/<user>_total_asset_tracking.json`：

```json
{
  "version": 1,
  "userporfolioperf_trackingsys_toggle": true,
  "userportfolioperf_tracksys": {
    "enabled_at": "ISO-8601",
    "disabled_at": null,
    "has_tracking_gap": false,
    "benchmarks": ["QQQ", "VT"],
    "valuations": []
  },
  "usertotalAsset_tracking": []
}
```

`usertotalAsset_tracking` 的每筆出入金包含：

- 唯一 ID、發生時間、`deposit`／`withdrawal`
- 原幣金額、幣別、換算 USD 金額與當時匯率
- 來源／用途 category、管道 channel、券商、帳戶與備註
- QQQ／VT 當時可取得的最近收盤價及實際市場日期

## 計算

首次估值為 Tracking Baseline。每個 benchmark 的初始單位數：

```text
初始 benchmark units = baseline 完整資產總值 / baseline benchmark 收盤價
```

每次外部資金流同步調整 benchmark units：

```text
入金：units += 入金 USD 等值 / 當日 benchmark 收盤價
出金：units -= 出金 USD 等值 / 當日 benchmark 收盤價
benchmark 等值 = units × 最新 benchmark 收盤價
```

使用者相對大盤的主要畫面指標：

```text
Performance Gap % =
  (使用者完整資產總值 - benchmark 等值) / benchmark 等值 × 100
```

正值代表使用者領先，負值代表落後。使用者累積報酬另以每期外部資金流排除後的 time-weighted return 串接；Position Reallocation 不是外部資金流。

## 寫入規則

- 新帳號註冊時 opt-in 不產生 Tracking Gap。
- 績效比較頁按 `d` 並確認後會停用追蹤、寫入 `disabled_at`，保留既有估值與出入金紀錄，並解除追蹤期間的持股／現金管理限制。
- 既有帳號中途啟用或停用後重新啟用，必須標示 Tracking Gap。
- 首筆完整估值立即建立 baseline；其後每週日最多寫入一次估值。
- 週日 benchmark 使用該日以前最近一個真實美股收盤價，通常是週五。
- 買進證券扣除同券商／帳戶／幣別現金；不足時拒絕。
- 賣出證券把成交價值轉回同帳戶現金。
- 現金只能透過宣告入金或出金改變。
