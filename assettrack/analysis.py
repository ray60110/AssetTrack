"""
assettrack/analysis.py — 進階分析：主動式 ETF 跨基金持股趨勢共識（離線運算）

bug#00060: 使用者要求系統能「離線」自動整理主動式 ETF 的買賣趨勢，並回報有多少
比例的 ETF 呈現相似趨勢、以及該趨勢對應的總股數。

bug#00061: 延伸為「多數性」（數個 ETF 同時間區間同向買賣）與「規模性」（單一或多數
ETF 購入大量市值部位）雙維度結論，供 Dashboard 首頁卡片與 ETF 頁面共用。

這個模組不打任何網路請求 —— 純粹讀取 `storage.py` 已經在背景刷新時逐日真實累積
下來的每檔 ETF 持股快照（`load_etf_daily_snapshots`），在本機離線運算完成。

**觀測狀態與方向訊號分開**：兩筆真實日期觀測即可列出期間買入／賣出／持平狀態與
淨部位市值變化；但 ETF 必須在視窗內至少有兩個內容不同的真實持股狀態，才可產生
方向訊號。相同 Yahoo top-holdings 即使跨日抓取，也只算一個方向狀態，不會灌高
`ready`；原始每日觀測仍保留供期間狀態表使用。
"""
from __future__ import annotations

from datetime import date as _date_cls, datetime, timedelta
from typing import Optional

from .quotes import estimate_shares
from .storage import taiwan_now


def _filter_window(snapshots: list[dict], cutoff_date: str) -> list[dict]:
    return [s for s in snapshots if s.get("date", "") >= cutoff_date]


def _portfolio_state_signature(snapshot: dict) -> tuple:
    """Identity of disclosed precise holdings, excluding observation-only data.

    Prices, estimated shares, AUM, and fetch dates can change while Yahoo's
    disclosed top-holdings payload remains the same. Those enrichments must not
    make duplicate holdings look like independent portfolio states. Broad asset
    allocation is deliberately excluded: it has its own analysis and cannot make
    unchanged named positions ready for precise-position analysis.
    """
    return tuple(sorted(
        (
            str(item.get("symbol") or ""),
            round(float(item.get("weight") or 0.0), 6),
            str(item.get("instrument_type") or ""),
            str(item.get("option_type") or ""),
            str(item.get("expiration") or ""),
            str(item.get("strike") or ""),
        )
        for item in (snapshot.get("holdings") or [])
        if item.get("symbol") is not None
    ))


def _collapse_unchanged_states(snapshots: list[dict]) -> list[dict]:
    """Collapse consecutive identical portfolio observations to their newest row."""
    states: list[dict] = []
    signatures: list[tuple] = []
    for snapshot in snapshots:
        signature = _portfolio_state_signature(snapshot)
        if signatures and signature == signatures[-1]:
            # Keep the newest observation because it is most likely to contain
            # the price/share enrichments added by newer fetches.
            states[-1] = snapshot
        else:
            states.append(snapshot)
            signatures.append(signature)
    return states


def _date_span_days(first: Optional[str], last: Optional[str]) -> Optional[int]:
    """Calendar days between two ``YYYY-MM-DD`` strings; None if either is bad."""
    try:
        return (
            datetime.strptime(last, "%Y-%m-%d") - datetime.strptime(first, "%Y-%m-%d")
        ).days
    except (TypeError, ValueError):
        return None


def _reported_share_direction(
    shares0: Optional[float],
    shares1: Optional[float],
    rel_threshold: float,
) -> str:
    """Direction from an *exactly reported* share count (SEC 13F).

    Returns "up"/"down" only when the reported share count moved by at least
    ``rel_threshold`` of the larger endpoint, so a rounding-level restatement is
    not reported as accumulation. A brand-new or fully exited position is always
    material. Missing counts are "flat" — never guessed.
    """
    if shares0 is None or shares1 is None:
        return "flat"
    delta = shares1 - shares0
    if delta == 0:
        return "flat"
    base = max(abs(shares0), abs(shares1))
    if base <= 0:
        return "flat"
    if abs(delta) / base < rel_threshold:
        return "flat"
    return "up" if delta > 0 else "down"


def consensus_from_counts(
    n_up: int, n_down: int, evaluated: int, threshold: float,
) -> tuple[str, float]:
    """The single definition of cross-source consensus (extracted, bug#00124).

    bug#00107（使用者審查 #1）：需嚴格多於反向才算共識。舊版 `pct_up >= pct_down`
    讓 2 上 2 下的平手一律判為「up」，注入系統性多頭偏誤；平手一律
    歸 mixed，多空對稱處理。Both the headline comparison and the quarter-by-
    quarter consistency pass call this, so the two can never drift apart.
    """
    if evaluated <= 0:
        return "flat", 0.0
    pct_up, pct_down = n_up / evaluated, n_down / evaluated
    if not n_up and not n_down:
        return "flat", 0.0
    if pct_up >= threshold and pct_up > pct_down:
        return "up", pct_up
    if pct_down >= threshold and pct_down > pct_up:
        return "down", pct_down
    return "mixed", max(pct_up, pct_down)


def _aggregate_allocation(contribs: list[dict]) -> dict:
    """Share of the combined book this position occupies, at each endpoint.

    Aggregated as sum(position value) / sum(fund AUM) rather than as an average
    of per-fund weights: a 5% position in a $1B fund and a 0.1% position in a
    $600B fund are not two comparable "weights" to be averaged, they are two
    very different dollar commitments. Returns None where the inputs are not
    all real — an allocation figure is only meaningful if both the numerator
    and the denominator are complete.
    """
    def side(value_key: str, aum_key: str) -> Optional[float]:
        pairs = [
            (c.get(value_key), c.get(aum_key)) for c in contribs
            if c.get(value_key) is not None and c.get(aum_key)
        ]
        if len(pairs) != len(contribs) or not pairs:
            return None
        total_aum = sum(aum for _, aum in pairs)
        if total_aum <= 0:
            return None
        return sum(value for value, _ in pairs) / total_aum * 100.0

    start = side("value_start", "aum_earliest")
    end = side("value_end", "aum_latest")
    return {
        "start_pct": start,
        "end_pct": end,
        "delta_pp": (end - start) if (start is not None and end is not None) else None,
    }


def _sum_or_none(values) -> Optional[float]:
    """Sum of the real values, or None when there are none — so "no data" never
    collapses into a confident 0."""
    real = [v for v in values if v is not None]
    return sum(real) if real else None


def _median(xs: list) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if n == 0:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _endpoint_view(snaps_subset: list[dict]) -> tuple[dict, dict, dict, dict, Optional[float], dict]:
    """bug#00110（使用者審查 #4）：把視窗一端的數筆快照聚合成穩健代表值——每檔持股
    的權重／價格取該端各快照的中位數、AUM 取中位數，避免單一異常端點快照翻轉整個
    方向訊號。缺真實值者不臆造（僅對有值者取中位數）。"""
    weights: dict[str, list] = {}
    prices: dict[str, list] = {}
    shares: dict[str, list] = {}
    values: dict[str, list] = {}
    metadata: dict[str, dict] = {}
    aums: list = []
    for s in snaps_subset:
        a = s.get("aum")
        if a is not None:
            aums.append(a)
        for h in s.get("holdings", []) or []:
            sym = h.get("symbol")
            if sym is None:
                continue
            w = h.get("weight", 0.0)
            if w is not None:
                weights.setdefault(sym, []).append(w)
            p = h.get("price")
            if p is not None:
                prices.setdefault(sym, []).append(p)
            sh = h.get("shares")
            if sh is not None:
                shares.setdefault(sym, []).append(sh)
            value = h.get("value")
            if value is not None:
                values.setdefault(sym, []).append(value)
            metadata[sym] = {
                key: h.get(key)
                for key in (
                    "name", "issuer", "cusip", "figi", "instrument_type",
                    "option_type", "expiration", "strike",
                )
                if h.get(key) is not None
            }
    return (
        {k: _median(v) for k, v in weights.items()},
        {k: _median(v) for k, v in prices.items()},
        {k: _median(v) for k, v in shares.items()},
        {k: _median(v) for k, v in values.items()},
        _median(aums),
        metadata,
    )


def compute_symbol_trends(
    snapshots_by_etf: dict[str, list[dict]],
    window_days: int = 14,
    flat_threshold_pp: float = 0.5,
    consensus_threshold: float = 0.5,
    as_of: Optional[str] = None,
    *,
    endpoint_k: int = 3,
    reported_share_signal: bool = False,
    rel_share_threshold: float = 0.05,
) -> dict:
    """Compute cross-ETF holding-weight trend consensus from real daily snapshots.

    bug#00123 keyword-only additions:

      - ``endpoint_k`` caps how many snapshots each window endpoint aggregates
        into a robust median. Quarterly sources (SEC 13F) must pass ``1`` so the
        latest filing is compared against the immediately preceding one instead
        of blending several quarters into a single "endpoint".
      - ``reported_share_signal`` switches direction classification to the exact
        **reported** share count. Yahoo ETF top-holdings never disclose shares,
        so `share_dir` there is derived from AUM x weight / price and can only
        corroborate the weight signal — hence the AND rule below. SEC 13F does
        report the exact share count per position, so requiring a >=0.5pp weight
        move as well would discard real, exactly-reported trades (a 15,000-line
        13F portfolio has per-position weights around 0.001pp; no genuine trade
        can ever clear a 0.5pp bar). With this flag the reported share delta is
        the signal and ``rel_share_threshold`` is the materiality filter.
      - ``rel_share_threshold`` is the minimum |share delta| / max(shares) that
        counts as a real position change under ``reported_share_signal``.

    snapshots_by_etf: {etf_symbol: [{"date": "YYYY-MM-DD", "aum": float|None,
                       "holdings": [{"symbol": str, "weight": float}, ...]}, ...]}
                       (as returned by storage.load_etf_daily_snapshots; order doesn't
                       matter, this function re-sorts and re-filters defensively.)

    For each ETF with >= 2 real observations inside the trailing `window_days`,
    compares its earliest vs. latest observation for the period-status table.
    Directional signals additionally require >= 2 distinct disclosed holdings
    states. For every holding symbol seen in either endpoint (a symbol dropping out
    of the top list is treated as its weight going to 0 — a real observed signal,
    not a guess), direction is classified using **two independent real signals that
    must agree** (bug#00061 follow-up):

      - share_dir:  sign of the real share-count delta (shares1 - shares0), each
                    computed from real AUM x weight / real holding price at that
                    snapshot's date (quotes.estimate_shares). None when either
                    snapshot lacks a real price for that holding (e.g. snapshots
                    recorded before this feature started capturing prices).
      - weight_dir: sign of the raw holding-weight delta (w1 - w0), thresholded by
                    flat_threshold_pp (percentage points) — this is the OLD, sole
                    signal used before this fix.

    A symbol is only "up" if share_dir == "up" AND weight_dir == "up" (real shares
    increased AND its proportion of the fund increased); only "down" if both agree
    downward. Every other case — including when only one signal moved, they
    disagree, or the real-price data needed for share_dir isn't available yet —
    is classified "flat" and excluded from consensus/scale ranking.

    Why: weight_dir alone can't tell a real purchase from a stock simply rallying
    while the fund does nothing (rising price mechanically raises that holding's
    weight with zero trading). Requiring share_dir to agree filters that out,
    since share_dir is computed from real per-date prices and stays flat when the
    real share count didn't change, even if price and weight both moved.

    Then, across all ready ETFs, each held symbol gets a cross-ETF consensus (「多數
    性」): the fraction of ETFs (that actually hold/held it) moving the same
    direction. A symbol only gets a "up"/"down" consensus label when that fraction
    is >= consensus_threshold (default 50%); otherwise it's "mixed".

    Two dollar/share estimates are computed per (etf, symbol) contribution:
      - value_delta: aum_latest*(w1/100) - aum_earliest*(w0/100) — a direct real
        dollar estimate, the primary basis for 「規模性」 (scale) ranking. Still
        includes price-return effects (it's a raw dollar-exposure delta), but
        `direction` filtering above already keeps price-only drift out of what
        gets reported as a "move" in the first place.
      - share_delta: real share-count delta as described above (None when a real
        price wasn't available for either snapshot — never fabricated/guessed).

    Returns a dict with `symbols` (all comparable positions for the status table;
    directional ranking remains in rank_symbol_trends) and `raw_contributions`
    (every individual etf/symbol up|down|flat period result with value_delta).
    """
    as_of_date = as_of or taiwan_now().strftime("%Y-%m-%d")
    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")

    etf_coverage: dict[str, dict] = {}
    all_contributions: list[dict] = []

    for etf_sym, raw_snaps in snapshots_by_etf.items():
        observations = sorted(
            _filter_window(raw_snaps or [], cutoff_date),
            key=lambda s: s.get("date", ""),
        )
        snaps = _collapse_unchanged_states(observations)
        observations_in_window = len(observations)
        distinct_states = len(snaps)
        comparable = observations_in_window >= 2
        ready = distinct_states >= 2
        etf_coverage[etf_sym] = {
            # Keep the legacy field for callers, but distinguish raw daily
            # observations from independently disclosed portfolio states.
            "days_in_window": observations_in_window,
            "observations_in_window": observations_in_window,
            "distinct_states": distinct_states,
            "comparable": comparable,
            "first_date": observations[0]["date"] if observations else None,
            "last_date": observations[-1]["date"] if observations else None,
            "ready": ready,
            # bug#00123: a single disclosed state across the whole window means
            # the upstream provider has not published a new portfolio since
            # `state_since` — that is a source-freshness fact, not a measured
            # "no change". Callers must be able to say so instead of printing a
            # row of zeroes that looks like a completed comparison.
            "source_unchanged": comparable and not ready,
            "state_since": (
                observations[0]["date"] if (observations and not ready) else None
            ),
            "state_unchanged_days": (
                _date_span_days(observations[0]["date"], observations[-1]["date"])
                if (observations and not ready) else None
            ),
        }
        if not comparable:
            continue

        # A repeated disclosed state is still a useful period observation for
        # the detail table (for example, "no holdings change" while exposure
        # value moved with AUM). It must not make the ETF signal-ready. Use raw
        # endpoints for that flat status, and distinct states only when there is
        # a real state transition eligible for directional analysis.
        comparison_snaps = snaps if ready else observations
        earliest, latest = comparison_snaps[0], comparison_snaps[-1]
        # bug#00110（使用者審查 #4）：不再用單一頭尾快照，改取視窗兩端各 k 筆的中位數
        # 代表值（k = min(3, len//2)，兩端不重疊；只有 2 筆時退化為原本的兩點比較），
        # 降低單一異常端點快照翻轉整個方向訊號的脆弱度。日期標籤仍取真實頭尾（span）。
        k = max(1, min(endpoint_k, len(comparison_snaps) // 2))
        early_w, early_price, early_shares, early_values, early_aum, early_meta = _endpoint_view(comparison_snaps[:k])
        late_w, late_price, late_shares, late_values, late_aum, late_meta = _endpoint_view(comparison_snaps[-k:])

        for sym in set(early_w) | set(late_w):
            w0 = early_w.get(sym) or 0.0
            w1 = late_w.get(sym) or 0.0
            weight_delta = w1 - w0

            if weight_delta > flat_threshold_pp:
                weight_dir = "up"
            elif weight_delta < -flat_threshold_pp:
                weight_dir = "down"
            else:
                weight_dir = "flat"

            if sym not in early_w:
                shares0 = 0
            elif sym in early_shares:
                shares0 = early_shares[sym]
            else:
                shares0 = estimate_shares(sym, w0, early_aum, early_price.get(sym))
            if sym not in late_w:
                shares1 = 0
            elif sym in late_shares:
                shares1 = late_shares[sym]
            else:
                shares1 = estimate_shares(sym, w1, late_aum, late_price.get(sym))
            share_delta = (shares1 - shares0) if (shares0 is not None and shares1 is not None) else None

            if share_delta is None:
                share_dir = None
            elif share_delta > 0:
                share_dir = "up"
            elif share_delta < 0:
                share_dir = "down"
            else:
                share_dir = "flat"

            if reported_share_signal:
                # bug#00123: the filer reports the exact share count, so the
                # share delta *is* the trade. Weight agreement is deliberately
                # not required — in a several-thousand-line 13F the per-position
                # weight is a rounding error against `flat_threshold_pp`, and a
                # position's weight also moves purely because the rest of the
                # portfolio moved. Materiality comes from the relative size of
                # the reported share change instead.
                direction = _reported_share_direction(
                    shares0, shares1, rel_share_threshold,
                )
            else:
                # bug#00061 follow-up (user decision): only count a move as real
                # accumulation/reduction when the real share-count signal AND the
                # weight/AUM-proportion signal agree — a symbol rallying in price
                # with zero trading would otherwise show up as "up" on weight_dir
                # alone. Any disagreement, or missing share_dir (no real price yet),
                # is "flat" — excluded from consensus/scale ranking rather than guessed.
                if share_dir == "up" and weight_dir == "up":
                    direction = "up"
                elif share_dir == "down" and weight_dir == "down":
                    direction = "down"
                else:
                    direction = "flat"

            # bug#00123: value_delta must stay None when *every* input it could
            # be derived from is byte-identical at both endpoints. The AUM
            # fallback below otherwise evaluates to exactly 0.0 whenever the
            # provider republished the same AUM and the same weight — printing
            # "$0 net change" for what is really "the source never updated".
            # Same principle as bug#00119: a degenerate computation must not be
            # presented as a completed measurement.
            value_delta = None
            value_basis = None
            if sym in early_values or sym in late_values:
                value_delta = (late_values.get(sym) or 0.0) - (early_values.get(sym) or 0.0)
                value_basis = "reported_value"
            elif early_aum is not None and late_aum is not None:
                if early_aum == late_aum and w0 == w1:
                    value_delta, value_basis = None, "source_unchanged"
                else:
                    value_delta = late_aum * (w1 / 100.0) - early_aum * (w0 / 100.0)
                    value_basis = "aum_weight"

            # bug#00123: money actually traded, not exposure revalued.
            # |value_delta| was being reported as 買入/賣出總額, but a position
            # whose share count fell 30% while its price rallied has a POSITIVE
            # value_delta — it was then printed as "賣出 $1.3B" next to a
            # +$1.3B net. When both the reported share count and the reported
            # market value exist, |share delta| x implied price is the real
            # transacted amount and cannot disagree with the direction.
            trade_value = None
            price_ref = None
            for values, shares in ((late_values, late_shares), (early_values, early_shares)):
                value_at, shares_at = values.get(sym), shares.get(sym)
                if value_at and shares_at:
                    price_ref = float(value_at) / float(shares_at)
                    break
            if price_ref and share_delta is not None:
                trade_value = abs(float(share_delta)) * price_ref

            # bug#00124: allocation view — the *share of the book* this position
            # occupies at each endpoint. Money moves with price; allocation is
            # what the manager actually chose. Derived from the reported value
            # when there is one, otherwise from AUM x weight (identical
            # arithmetic, so the two sources aggregate together).
            value_start = early_values.get(sym)
            if value_start is None and early_aum is not None:
                value_start = early_aum * (w0 / 100.0)
            value_end = late_values.get(sym)
            if value_end is None and late_aum is not None:
                value_end = late_aum * (w1 / 100.0)

            contribution = {
                "etf": etf_sym,
                "symbol": sym,
                "direction": direction,
                "weight_start": round(w0, 6),
                "weight_end": round(w1, 6),
                "value_start": value_start,
                "value_end": value_end,
                "aum_earliest": early_aum,
                "weight_delta": round(weight_delta, 4),
                "share_delta": share_delta,
                "value_delta": value_delta,
                "trade_value": trade_value,
                "value_basis": value_basis,
                "source_unchanged": value_basis == "source_unchanged",
                "aum_latest": late_aum,
                "first_date": earliest["date"],
                "last_date": latest["date"],
            }
            contribution.update(early_meta.get(sym) or {})
            contribution.update(late_meta.get(sym) or {})
            all_contributions.append(contribution)

    etfs_ready = [e for e, c in etf_coverage.items() if c["ready"]]
    etfs_comparable = [e for e, c in etf_coverage.items() if c["comparable"]]

    # ── 多數性 (multiplicity): aggregate per held symbol across ETFs ───────────
    by_symbol: dict[str, list[dict]] = {}
    for c in all_contributions:
        by_symbol.setdefault(c["symbol"], []).append(c)

    symbols_report: dict[str, dict] = {}
    for sym, contribs in by_symbol.items():
        etfs_up = [c["etf"] for c in contribs if c["direction"] == "up"]
        etfs_down = [c["etf"] for c in contribs if c["direction"] == "down"]
        etfs_flat = [c["etf"] for c in contribs if c["direction"] == "flat"]
        evaluated = len(contribs)
        if evaluated == 0:
            continue

        consensus, consensus_pct = consensus_from_counts(
            len(etfs_up), len(etfs_down), evaluated, consensus_threshold,
        )
        pct_up = len(etfs_up) / evaluated
        pct_down = len(etfs_down) / evaluated
        consensus_etfs = (
            etfs_up if consensus == "up" else etfs_down if consensus == "down" else []
        )

        est_total_share_delta = None
        est_total_value_delta = None
        if consensus in ("up", "down"):
            sd = [c["share_delta"] for c in contribs if c["etf"] in consensus_etfs and c["share_delta"] is not None]
            vd = [c["value_delta"] for c in contribs if c["etf"] in consensus_etfs and c["value_delta"] is not None]
            if sd:
                est_total_share_delta = int(sum(abs(d) for d in sd))
            if vd:
                est_total_value_delta = sum(abs(d) for d in vd)

        # bug#00123: separate "compared two real states and nothing moved"
        # (flat) from "the provider never published a second state" (stale).
        # The old UI rendered both as 持平, which reads as an analysis result.
        source_unchanged = all(c.get("source_unchanged") for c in contribs)
        if consensus in ("up", "down"):
            status = "signal"
        elif source_unchanged:
            status = "source_unchanged"
        elif consensus == "mixed":
            status = "mixed"
        else:
            status = "flat"

        allocation = _aggregate_allocation(contribs)

        symbols_report[sym] = {
            "status": status,
            "source_unchanged": source_unchanged,
            # 配置權重：這檔部位佔「所有納入比較的基金／申報人合計資產」的百分比，
            # 期初與期末各一個真值。缺 AUM 或缺市值時回 None，不以單一基金權重
            # 冒充整體配置。
            "allocation_start_pct": allocation["start_pct"],
            "allocation_end_pct": allocation["end_pct"],
            "allocation_delta_pp": allocation["delta_pp"],
            "etfs_up": etfs_up,
            "etfs_down": etfs_down,
            "etfs_flat": etfs_flat,
            "etfs_evaluated": evaluated,
            "pct_up": round(pct_up * 100, 1),
            "pct_down": round(pct_down * 100, 1),
            "consensus": consensus,
            "consensus_pct": round(consensus_pct * 100, 1),
            "est_total_share_delta": est_total_share_delta,
            "est_total_value_delta": est_total_value_delta,
            "buy_value": sum(
                abs(c["value_delta"]) for c in contribs
                if c["direction"] == "up" and c["value_delta"] is not None
            ),
            "sell_value": sum(
                abs(c["value_delta"]) for c in contribs
                if c["direction"] == "down" and c["value_delta"] is not None
            ),
            # Raw signed exposure change remains useful context even when the
            # strict share+weight rule classifies the position as flat. It is
            # deliberately not labelled as a purchase or sale because price/AUM
            # movement can contribute to this number.
            "net_value_delta": (
                sum(c["value_delta"] for c in contribs if c["value_delta"] is not None)
                if any(c["value_delta"] is not None for c in contribs)
                else None
            ),
            # bug#00123: net of the *confirmed* buys and sells only. The raw
            # figure above also moves with price, so it can carry the opposite
            # sign to the consensus label (a filer whose share count barely
            # changed still revalues) — printing those two side by side reads
            # as a contradiction. Callers showing a direction column next to a
            # money column should use this one.
            # Transacted amounts (see `trade_value` above). None when the source
            # does not report share counts, in which case callers fall back to
            # the exposure-based buy_value/sell_value.
            "buy_trade_value": _sum_or_none(
                c.get("trade_value") for c in contribs if c["direction"] == "up"
            ),
            "sell_trade_value": _sum_or_none(
                c.get("trade_value") for c in contribs if c["direction"] == "down"
            ),
            "confirmed_net_trade_value": _sum_or_none(
                (
                    c["trade_value"] if c["direction"] == "up" else -c["trade_value"]
                )
                for c in contribs
                if c["direction"] in ("up", "down") and c.get("trade_value") is not None
            ),
            "confirmed_net_value_delta": (
                sum(
                    c["value_delta"] for c in contribs
                    if c["direction"] in ("up", "down") and c["value_delta"] is not None
                )
                if any(
                    c["direction"] in ("up", "down") and c["value_delta"] is not None
                    for c in contribs
                )
                else None
            ),
            "first_date": min(
                (c["first_date"] for c in contribs if c.get("first_date")),
                default=None,
            ),
            "last_date": max(
                (c["last_date"] for c in contribs if c.get("last_date")),
                default=None,
            ),
            "position": next((
                {
                    key: c.get(key)
                    for key in (
                        "name", "issuer", "cusip", "figi", "instrument_type",
                        "option_type", "expiration", "strike",
                    )
                    if c.get(key) is not None
                }
                for c in reversed(contribs)
                if any(c.get(key) is not None for key in (
                    "name", "issuer", "cusip", "figi", "instrument_type",
                    "option_type", "expiration", "strike",
                ))
            ), {}),
        }

    ac_trends = compute_asset_class_trends(
        snapshots_by_etf, window_days=window_days,
        flat_threshold_pp=flat_threshold_pp,
        consensus_threshold=consensus_threshold, as_of=as_of_date,
    )

    directional = [c for c in all_contributions if c["direction"] in ("up", "down")]
    stale_sources = [e for e, c in etf_coverage.items() if c.get("source_unchanged")]
    stale_spans = [
        c["state_unchanged_days"] for c in etf_coverage.values()
        if c.get("state_unchanged_days") is not None
    ]
    return {
        "window_days": window_days,
        "as_of": as_of_date,
        "etf_coverage": etf_coverage,
        # bug#00123: source-freshness is a first-class output. When every
        # provider republished an unchanged portfolio all window long there is
        # nothing to analyse, and the caller must say that instead of drawing a
        # table of zeroes.
        "source_freshness": {
            "sources_total": len(etf_coverage),
            "sources_comparable": len(etfs_comparable),
            "sources_state_changed": len(etfs_ready),
            "sources_unchanged": len(stale_sources),
            "unchanged_sources": sorted(stale_sources),
            "max_unchanged_days": max(stale_spans) if stale_spans else None,
            "oldest_state_since": min(
                (
                    c["state_since"] for c in etf_coverage.values()
                    if c.get("state_since")
                ),
                default=None,
            ),
            "all_sources_unchanged": bool(etfs_comparable) and not etfs_ready,
        },
        "etfs_ready_count": len(etfs_ready),
        "etfs_comparable_count": len(etfs_comparable),
        "etfs_total_count": len(etf_coverage),
        "etfs_ready_pct": round(len(etfs_ready) / len(etf_coverage) * 100, 1) if etf_coverage else 0.0,
        "etfs_comparable_pct": (
            round(len(etfs_comparable) / len(etf_coverage) * 100, 1)
            if etf_coverage else 0.0
        ),
        "symbols": symbols_report,
        "raw_contributions": all_contributions,
        "flow_totals": {
            "positions_bought": sum(1 for c in directional if c["direction"] == "up"),
            "positions_sold": sum(1 for c in directional if c["direction"] == "down"),
            "buy_value": sum(
                abs(c["value_delta"]) for c in directional
                if c["direction"] == "up" and c["value_delta"] is not None
            ),
            "sell_value": sum(
                abs(c["value_delta"]) for c in directional
                if c["direction"] == "down" and c["value_delta"] is not None
            ),
            "net_value_delta": (
                sum(
                    c["value_delta"] for c in all_contributions
                    if c["value_delta"] is not None
                )
                if any(c["value_delta"] is not None for c in all_contributions)
                else None
            ),
        },
        "asset_classes": ac_trends,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SEC 13F institutional quarter-over-quarter analysis (bug#00123)
# ─────────────────────────────────────────────────────────────────────────────
# Yahoo's ETF top-holdings feed refreshes on each fund's *disclosure* cadence,
# not daily, and never discloses share counts — so on a 14-day window it can
# produce no directional signal at all. SEC 13F filings are the opposite: fewer
# observations (quarterly), but every line carries an exactly reported share
# count and market value, which is precisely what the buy/sell/net columns need.
# These helpers run the same consensus engine over 13F snapshots.

# bug#00124 — why the observation window is expressed in *report periods*, not days.
#
# Form 13F-HR is due 45 days after each calendar quarter end. So between one
# filing deadline (Q+45) and the next (Q+136), the second-newest report period
# is somewhere between 136 and 227 days old. A fixed calendar window therefore
# drifts in and out of usefulness:
#
#   window        days with >= 2 report periods (2026 simulation)
#   4 months 122d    0.0%  — below the 136-day floor, can NEVER hold two periods
#   6 months 183d   52.3%  — four blackout stretches of ~43 days every year
#   8 months 243d  100.0%  — but only 18% of days reach a third period
#  12 months 365d  100.0%  — and 100% of days reach three
#
# Hence: retain 12 months (four report periods, which also matches
# storage.ANALYSIS_CACHE_RETENTION_DAYS), and never key the window off "today".
INSTITUTION_HISTORY_QUARTERS = 4
# The comparison itself stays quarter-over-quarter. Widening the *comparison*
# to the full retained window would silently turn a quarterly trade signal into
# a year-over-year drift measurement — a different question with a different
# meaning. The extra retained quarters exist to judge *consistency*, not to
# stretch the endpoints.
INSTITUTION_QUARTERS_COMPARED = 2
# 13F-HR statutory filing deadline, in days after the report period end.
INSTITUTION_FILING_DEADLINE_DAYS = 45
# Wide enough to span any two consecutive 13F report periods after the caller
# has already trimmed to the quarters being compared.
_INSTITUTION_WINDOW_DAYS = 4000


def _institution_join_key(holding: dict) -> Optional[str]:
    """Cross-filer join key for one 13F position.

    Institutional snapshots key positions on ``figi or cusip`` (see
    ``institutional.parse_13f_information_table``). That is stable per filer but
    NOT comparable across filers: one manager reports a FIGI for a security and
    another reports only the CUSIP, so the same holding lands under two
    different keys and cross-institution consensus silently never forms. CUSIP
    is mandatory on every 13F line, so it is the only identifier guaranteed to
    be shared — prefer it, and fall back to the existing key otherwise.
    """
    suffix = holding.get("option_type") or holding.get("amount_type") or "SH"
    cusip = holding.get("cusip")
    if cusip:
        return f"{cusip}:{suffix}"
    return holding.get("symbol")


def normalize_institution_snapshots(snapshots: list[dict]) -> list[dict]:
    """Re-key one filer's snapshots onto the cross-filer CUSIP join key.

    Pure transformation of the in-memory copy — the stored history log keeps its
    original per-filer identifiers untouched.
    """
    out: list[dict] = []
    for snapshot in snapshots or []:
        holdings = []
        for holding in snapshot.get("holdings") or []:
            key = _institution_join_key(holding)
            if not key:
                continue
            item = dict(holding)
            item["symbol"] = key
            holdings.append(item)
        new_snapshot = dict(snapshot)
        new_snapshot["holdings"] = holdings
        out.append(new_snapshot)
    return out


def _latest_quarters(snapshots: list[dict], quarters: int) -> list[dict]:
    """The newest `quarters` distinct report dates, oldest first."""
    ordered = sorted(
        (s for s in (snapshots or []) if s.get("date")),
        key=lambda s: s["date"],
    )
    seen: dict[str, dict] = {}
    for snapshot in ordered:
        seen[snapshot["date"]] = snapshot  # last write wins per report date
    dates = sorted(seen)[-max(2, quarters):]
    return [seen[d] for d in dates]


def _next_quarter_end(after: _date_cls) -> _date_cls:
    for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
        candidate = _date_cls(after.year, month, day)
        if candidate > after:
            return candidate
    return _date_cls(after.year + 1, 3, 31)


def institution_provenance(
    snapshots_by_entity: dict[str, list[dict]],
    compared: list[str],
    today: Optional[str] = None,
) -> dict:
    """Answer "when is this data from, and when does it change?" — explicitly.

    A 13F row describes a quarter that ended months ago and was published weeks
    after that. Both dates matter and neither is guessable from the other, so
    both are surfaced. `filing_date_estimated` marks periods stored before the
    filing date was persisted (bug#00124): the statutory deadline is shown, and
    labelled as the deadline rather than passed off as the real filing date.
    """
    as_of = _date_cls.fromisoformat(today) if today else taiwan_now().date()
    filing_dates: dict[str, str] = {}
    for snaps in (snapshots_by_entity or {}).values():
        for snapshot in snaps or []:
            date_str, filed = snapshot.get("date"), snapshot.get("filing_date")
            if date_str and filed:
                # The last filer to report this period sets the public date.
                filing_dates[date_str] = max(filing_dates.get(date_str, ""), filed)

    def deadline(period: str) -> str:
        return (
            _date_cls.fromisoformat(period)
            + timedelta(days=INSTITUTION_FILING_DEADLINE_DAYS)
        ).isoformat()

    periods = []
    for period in compared:
        real = filing_dates.get(period)
        periods.append({
            "report_date": period,
            "filing_date": real or deadline(period),
            "filing_date_estimated": real is None,
        })

    latest = compared[-1] if compared else None
    next_period = (
        _next_quarter_end(_date_cls.fromisoformat(latest)) if latest else None
    )
    return {
        "periods": periods,
        "report_date_from": compared[0] if compared else None,
        "report_date_to": latest,
        "data_age_days": (
            (as_of - _date_cls.fromisoformat(latest)).days if latest else None
        ),
        "filing_lag_days": INSTITUTION_FILING_DEADLINE_DAYS,
        "next_report_date": next_period.isoformat() if next_period else None,
        "next_filing_due": (
            (next_period + timedelta(days=INSTITUTION_FILING_DEADLINE_DAYS)).isoformat()
            if next_period else None
        ),
        # 13F discloses quarter-end snapshots only — never a trade date. The
        # honest statement about timing is an interval, not a point.
        "trade_window_from": compared[0] if compared else None,
        "trade_window_to": latest,
        "trade_date_disclosed": False,
    }


def _quarter_consistency(
    snapshots_by_entity: dict[str, list[dict]],
    latest_direction_by_symbol: dict[str, str],
    consensus_threshold: float,
    rel_share_threshold: float,
    max_periods: int = INSTITUTION_HISTORY_QUARTERS,
) -> dict[str, dict]:
    """How many of the retained quarter-over-quarter transitions moved the same
    way as the most recent one.

    "Three consecutive quarters of accumulation" and "accumulated once, this
    quarter" are different strength claims, and only the retained history can
    tell them apart. This is why the window keeps four periods while the
    headline comparison stays on the latest two.
    """
    periods = sorted({
        s["date"] for snaps in (snapshots_by_entity or {}).values()
        for s in (snaps or []) if s.get("date")
    })[-max_periods:]
    if len(periods) < 3:
        return {}

    def shares_at(snaps: list[dict], period: str) -> dict[str, float]:
        for snapshot in snaps or []:
            if snapshot.get("date") == period:
                return {
                    h["symbol"]: h.get("shares")
                    for h in snapshot.get("holdings") or []
                    if h.get("symbol")
                }
        return {}

    transitions: list[dict[str, str]] = []
    for older, newer in zip(periods, periods[1:]):
        # Direction-only pass: consistency needs the consensus *label* per
        # period, not allocation/trade-value maths. Re-running the full engine
        # once per transition tripled the screen's load time for numbers that
        # are then thrown away.
        counts: dict[str, list[int]] = {}
        for snaps in snapshots_by_entity.values():
            before, after = shares_at(snaps, older), shares_at(snaps, newer)
            if not before or not after:
                continue
            for symbol in set(before) | set(after):
                direction = _reported_share_direction(
                    before.get(symbol, 0), after.get(symbol, 0), rel_share_threshold,
                )
                tally = counts.setdefault(symbol, [0, 0, 0])
                tally[2] += 1
                if direction == "up":
                    tally[0] += 1
                elif direction == "down":
                    tally[1] += 1
        transitions.append({
            symbol: consensus_from_counts(
                up, down, evaluated, consensus_threshold,
            )[0]
            for symbol, (up, down, evaluated) in counts.items()
        })

    out: dict[str, dict] = {}
    for symbol, latest in latest_direction_by_symbol.items():
        if latest not in ("up", "down"):
            continue
        same = sum(1 for t in transitions if t.get(symbol) == latest)
        out[symbol] = {
            "same_direction_quarters": same,
            "transitions_evaluated": len(transitions),
        }
    return out


def compute_institution_trends(
    snapshots_by_entity: dict[str, list[dict]],
    quarters: int = INSTITUTION_QUARTERS_COMPARED,
    consensus_threshold: float = 0.5,
    rel_share_threshold: float = 0.05,
    as_of: Optional[str] = None,
    history_quarters: int = INSTITUTION_HISTORY_QUARTERS,
    today: Optional[str] = None,
) -> dict:
    """Quarter-over-quarter 13F consensus across institutional filers.

    Each filer is trimmed to its newest `quarters` report periods and compared
    endpoint-to-endpoint (``endpoint_k=1`` — quarterly filings must not be
    median-blended, the previous filing IS the baseline). Direction comes from
    the exactly reported share count (``reported_share_signal=True``); the
    weight-agreement rule that guards the estimated ETF path would reject every
    real trade here, since a multi-thousand-line portfolio has no position
    anywhere near the 0.5pp weight bar.

    `history_quarters` (default 4 = 12 months) is retained *beyond* the compared
    pair purely to compute directional consistency; see the module constants for
    why a calendar-day window cannot do this job.
    """
    normalized = {
        entity: normalize_institution_snapshots(snaps)
        for entity, snaps in (snapshots_by_entity or {}).items()
    }
    retained = {
        entity: _latest_quarters(snaps, history_quarters)
        for entity, snaps in normalized.items()
    }
    trimmed = {
        entity: _latest_quarters(snaps, quarters)
        for entity, snaps in normalized.items()
    }
    trimmed = {e: w for e, w in trimmed.items() if len(w) >= 2}

    if not trimmed:
        return {
            "window_days": 0, "as_of": as_of or taiwan_now().strftime("%Y-%m-%d"),
            "etf_coverage": {}, "etfs_ready_count": 0, "etfs_comparable_count": 0,
            "etfs_total_count": 0, "etfs_ready_pct": 0.0, "etfs_comparable_pct": 0.0,
            "symbols": {}, "raw_contributions": [], "quarters_compared": quarters,
            "report_dates": [], "consistency": {},
            "provenance": institution_provenance({}, [], today=today),
            "flow_totals": {
                "positions_bought": 0, "positions_sold": 0,
                "buy_value": 0, "sell_value": 0, "net_value_delta": None,
            },
            "source_freshness": {
                "sources_total": 0, "sources_comparable": 0,
                "sources_state_changed": 0, "sources_unchanged": 0,
                "unchanged_sources": [], "max_unchanged_days": None,
                "oldest_state_since": None, "all_sources_unchanged": False,
            },
            "asset_classes": {},
        }

    latest_date = max(s[-1]["date"] for s in trimmed.values())
    report = compute_symbol_trends(
        trimmed,
        window_days=_INSTITUTION_WINDOW_DAYS,
        flat_threshold_pp=0.0,
        consensus_threshold=consensus_threshold,
        as_of=as_of or latest_date,
        endpoint_k=1,
        reported_share_signal=True,
        rel_share_threshold=rel_share_threshold,
    )
    report["quarters_compared"] = quarters
    report["history_quarters"] = history_quarters
    report["rel_share_threshold"] = rel_share_threshold
    report["report_dates"] = sorted({
        s["date"] for snaps in trimmed.values() for s in snaps
    })
    report["retained_report_dates"] = sorted({
        s["date"] for snaps in retained.values() for s in snaps
    })[-history_quarters:]
    report["provenance"] = institution_provenance(
        snapshots_by_entity, report["report_dates"], today=today,
    )
    report["consistency"] = _quarter_consistency(
        retained,
        {s: info["consensus"] for s, info in report["symbols"].items()},
        consensus_threshold,
        rel_share_threshold,
        max_periods=history_quarters,
    )
    for symbol, info in report["symbols"].items():
        info.update(report["consistency"].get(symbol) or {})
    return report


# ── Offline issuer-name → ticker resolution ──────────────────────────────────
# 13F lines carry an issuer name, CUSIP and (sometimes) FIGI, but never a
# ticker; the user's own positions and sector groups are keyed by ticker. The
# ETF top-holdings snapshots already on disk carry BOTH (ticker + company name),
# so they can seed an offline index. Anything the index cannot resolve keeps its
# issuer name and is simply not claimed to be a known ticker.

_NAME_NOISE_TOKENS = frozenset({
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES",
    "LTD", "LIMITED", "PLC", "LP", "LLC", "SA", "NV", "AG", "SE",
    "HOLDING", "HOLDINGS", "GROUP", "GRP", "THE", "NEW", "ORD", "SHS",
    "CLASS", "CL", "A", "B", "C", "COM", "COMMON", "STOCK", "SHARES",
    "ADR", "ADS", "SPON", "SPONSORED", "TR", "TRUST", "REIT", "&", "DEL",
})

# SEC filers abbreviate aggressively ("TAIWAN SEMICONDUCTOR MFG"), fund data
# providers spell it out ("Taiwan Semiconductor Manufacturing"). Expanding a
# short list of abbreviations is what makes the two sides comparable at all.
_NAME_ABBREVIATIONS = {
    "MFG": "MANUFACTURING",
    "TECH": "TECHNOLOGY",
    "TECHS": "TECHNOLOGIES",
    "INTL": "INTERNATIONAL",
    "NATL": "NATIONAL",
    "PHARMS": "PHARMACEUTICALS",
    "PHARM": "PHARMACEUTICAL",
    "SYS": "SYSTEMS",
    "COMM": "COMMUNICATIONS",
    "COMMS": "COMMUNICATIONS",
    "IND": "INDUSTRIES",
    "INDS": "INDUSTRIES",
    "SVCS": "SERVICES",
    "RES": "RESOURCES",
    "FINL": "FINANCIAL",
    "ELEC": "ELECTRIC",
    "MTRS": "MOTORS",
    "LABS": "LABORATORIES",
    "SEMICON": "SEMICONDUCTOR",
}


def normalize_company_name(name: Optional[str]) -> str:
    """Comparable form of a company name: uppercase alphanumerics, common SEC
    abbreviations expanded, legal-form and share-class noise words removed."""
    import re
    cleaned = re.sub(r"[^A-Z0-9 ]+", " ", (name or "").upper())
    tokens = [
        _NAME_ABBREVIATIONS.get(t, t)
        for t in cleaned.split()
        if t and t not in _NAME_NOISE_TOKENS
    ]
    return " ".join(t for t in tokens if t not in _NAME_NOISE_TOKENS)


def build_ticker_name_index(snapshots_by_etf: dict[str, list[dict]]) -> dict[str, str]:
    """{normalized company name: ticker} from real ETF holdings snapshots.

    Both the full normalized name and its leading two tokens are indexed, so
    "TAIWAN SEMICONDUCTOR MANUFACTURING" also answers to "TAIWAN SEMICONDUCTOR".
    Ambiguous keys (two different tickers claiming the same string) are dropped
    rather than resolved by guesswork — a wrong ticker here would mis-file a
    position into the user's "positions I hold" tier.
    """
    full: dict[str, set] = {}
    prefix: dict[str, set] = {}
    for snaps in (snapshots_by_etf or {}).values():
        for snapshot in snaps or []:
            for holding in snapshot.get("holdings") or []:
                ticker, name = holding.get("symbol"), holding.get("name")
                if not ticker or not name or ":" in str(ticker):
                    continue
                key = normalize_company_name(name)
                if not key:
                    continue
                upper = str(ticker).upper()
                full.setdefault(key, set()).add(upper)
                tokens = key.split()
                if len(tokens) > 2:
                    prefix.setdefault(" ".join(tokens[:2]), set()).add(upper)

    index = {k: next(iter(v)) for k, v in prefix.items() if len(v) == 1}
    index.update({k: next(iter(v)) for k, v in full.items() if len(v) == 1})
    return index


def resolve_position_ticker(position: dict, name_index: dict[str, str]) -> Optional[str]:
    """Best-effort ticker for one analysed position, or None.

    ETF-sourced positions already are tickers. 13F-sourced positions are matched
    on their normalized issuer name (exact first, then the leading two tokens);
    an unmatched issuer returns None so callers render the issuer name instead
    of inventing a ticker.
    """
    symbol = str(position.get("symbol") or "")
    if symbol and ":" not in symbol:
        return symbol.upper()
    index = name_index or {}
    for field in ("issuer", "name"):
        key = normalize_company_name(position.get(field))
        if not key:
            continue
        if key in index:
            return index[key]
        tokens = key.split()
        if len(tokens) > 2 and " ".join(tokens[:2]) in index:
            return index[" ".join(tokens[:2])]
    return None


_ASSET_CLASS_NAMES = {
    "stock": "股票 (Stock)",
    "bond": "債券 (Bond)",
    "cash": "現金 (Cash)",
    "preferred": "特別股 (Preferred)",
    "convertible": "可轉債 (Convertible)",
    "other": "黃金/大宗商品/其他 (Gold/Commodities/Other)",
}


def compute_asset_class_trends(
    snapshots_by_etf: dict[str, list[dict]],
    window_days: int = 14,
    flat_threshold_pp: float = 0.5,
    consensus_threshold: float = 0.5,
    as_of: Optional[str] = None,
) -> dict:
    """Compute cross-ETF asset class allocation trends (Stock, Bond, Cash, Gold/Commodities/Other)
    over trailing window_days from real daily snapshots (bug#00103).

    Compares earliest vs latest snapshot's asset_classes breakdown for each ready ETF.
    Identifies cross-ETF majority consensus for broad asset allocation shifts.
    """
    as_of_date = as_of or taiwan_now().strftime("%Y-%m-%d")
    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")

    by_class: dict[str, list[dict]] = {}
    etfs_ready = 0

    for etf_sym, raw_snaps in snapshots_by_etf.items():
        snaps = sorted(_filter_window(raw_snaps or [], cutoff_date), key=lambda s: s.get("date", ""))
        if len(snaps) < 2:
            continue
        earliest, latest = snaps[0], snaps[-1]
        early_ac = earliest.get("asset_classes") or {}
        late_ac = latest.get("asset_classes") or {}
        if not early_ac and not late_ac:
            continue

        etfs_ready += 1
        all_keys = set(early_ac) | set(late_ac)
        for cls_key in all_keys:
            w0 = float(early_ac.get(cls_key, 0.0) or 0.0)
            w1 = float(late_ac.get(cls_key, 0.0) or 0.0)
            delta = w1 - w0
            if delta > flat_threshold_pp:
                direction = "up"
            elif delta < -flat_threshold_pp:
                direction = "down"
            else:
                direction = "flat"

            by_class.setdefault(cls_key, []).append({
                "etf": etf_sym,
                "direction": direction,
                "delta_pp": delta,
            })

    classes_report: dict[str, dict] = {}
    for cls_key, contribs in by_class.items():
        evaluated = len(contribs)
        if evaluated == 0:
            continue
        up_contribs = [c for c in contribs if c["direction"] == "up"]
        down_contribs = [c for c in contribs if c["direction"] == "down"]
        pct_up = len(up_contribs) / evaluated
        pct_down = len(down_contribs) / evaluated

        # bug#00107（使用者審查 #1）：同 compute_symbol_trends——平手須歸 mixed，不偏多。
        if pct_up >= consensus_threshold and pct_up > pct_down:
            consensus, consensus_pct = "up", pct_up
        elif pct_down >= consensus_threshold and pct_down > pct_up:
            consensus, consensus_pct = "down", pct_down
        else:
            consensus, consensus_pct = "mixed", max(pct_up, pct_down)

        avg_delta = sum(c["delta_pp"] for c in contribs) / evaluated
        classes_report[cls_key] = {
            "name": _ASSET_CLASS_NAMES.get(cls_key, cls_key),
            "etfs_up": [c["etf"] for c in up_contribs],
            "etfs_down": [c["etf"] for c in down_contribs],
            "evaluated": evaluated,
            "pct_up": round(pct_up * 100, 1),
            "pct_down": round(pct_down * 100, 1),
            "consensus": consensus,
            "consensus_pct": round(consensus_pct * 100, 1),
            "avg_delta_pp": round(avg_delta, 2),
        }

    return {"etfs_ready": etfs_ready, "classes": classes_report}


def rank_symbol_trends(
    report: dict,
    min_etfs_evaluated: int = 4,
    top_n: int = 20,
) -> list[tuple[str, dict]]:
    """Rank held symbols by cross-ETF consensus strength (「多數性」) for display."""
    items = [
        (sym, info) for sym, info in report.get("symbols", {}).items()
        if info["etfs_evaluated"] >= min_etfs_evaluated
        and (info.get("etfs_up") or info.get("etfs_down"))
    ]
    items.sort(
        key=lambda kv: (
            kv[1]["consensus"] != "mixed",
            kv[1]["consensus_pct"],
            kv[1]["etfs_evaluated"],
        ),
        reverse=True,
    )
    return items[:top_n]


def rank_scale_events(
    report: dict,
    top_n: int = 15,
    min_abs_value: float = 5_000_000.0,
    min_relative_to_aum: float = 0.005,
) -> list[dict]:
    """Rank individual etf/symbol moves by real dollar scale (「規模性」)."""
    events = []
    for c in report.get("raw_contributions", []):
        if c["direction"] == "flat" or c["value_delta"] is None:
            continue
        aum = c.get("aum_latest")
        rel_floor = (aum * min_relative_to_aum) if aum else min_abs_value
        threshold = max(min_abs_value, rel_floor)
        if abs(c["value_delta"]) >= threshold:
            events.append(c)

    events.sort(key=lambda c: abs(c["value_delta"]), reverse=True)
    return events[:top_n]


def _fmt_usd(v: float) -> str:
    av = abs(v)
    if av >= 1e9:
        return f"${av / 1e9:.2f}B"
    if av >= 1e6:
        return f"${av / 1e6:.1f}M"
    if av >= 1e3:
        return f"${av / 1e3:.0f}K"
    return f"${av:,.0f}"


def _etf_backtest_section(direction: str, backtest: "Optional[dict]"):
    """把 etf_backtest_note() 的回測命中率結論收成第三層 breakdown section（bug#00117）。
    以同一份 note 為 substitution，維持單一真理來源、不另重算統計。"""
    from .shared import _section
    note = etf_backtest_note(direction, backtest).replace("　▶ 回測：", "").strip()
    return _section(
        "回測驗證（walk-forward 命中率）",
        formula="命中率 = 訊號後前瞻 h 日方向正確次數 ÷ 可評估訊號數；超額 edge = 命中率 − 同宇集基準上漲率",
        substitution=note,
        explanation=("回測呼叫與畫面同一判斷函式（compute_symbol_trends），每個歷史日只餵 ≤T 的真實快照、"
                     "結構上無前視偏誤；顯著性經 Wilson CI＋對基準單尾二項檢定（以 ESS 消重疊視窗自相關、"
                     "Bonferroni 多重比較調整）。可評估訊號 < 20 時誠實標『資料累積中』。"))


def _etf_position_note(sym: str, up: bool, stance: dict) -> "Optional[str]":
    """與使用者部位方向一致性的一句話。無部位資料或賣出且未持有則回 None。"""
    if not stance:
        return None
    held = stance.get(sym.upper())
    signal_dir = "多" if up else "空"
    if held == "混合":
        return "你在此標的多空部位並存。"
    if held is not None:
        if held == signal_dir:
            return f"與你目前偏{held}的部位方向一致。"
        return f"與你目前偏{held}的部位方向相反，留意是否調節。"
    if up:
        return "你尚未持有此標的，可留意是否符合進場條件。"
    return None


def _etf_stance_section(sym: str, up: bool, stance: dict):
    """與使用者部位方向一致性的第三層 section（bug#00117）。無部位資料則回 None。"""
    from .shared import _section
    note = _etf_position_note(sym, up, stance)
    if not note:
        return None
    return _section("與你的部位比對", substitution=note,
                    explanation="以你目前持倉的淨多空立場（position_stance_by_symbol）與此訊號方向交叉比對，作為建設性提示，非加減碼指令。")


def generate_etf_recommendations(
    report: dict,
    min_etfs_evaluated: int = 4,
    top_n: int = 5,
    positions=None,
    backtest: "Optional[dict]" = None,
) -> "list":
    """把多數性／大類輪動／規模性三段結論組成三層結構化建議（bug#00117）。
    第一層＝方向結論；第二層＝如何判斷（多數性/雙真實訊號同向）；第三層＝共識公式＋
    帶入本標的數字＋回測＋部位一致性。generate_etf_conclusions 為其薄 wrapper。"""
    from .shared import Recommendation, _section, position_stance_by_symbol
    stance = position_stance_by_symbol(positions) if positions else {}
    window = report.get("window_days", 14)
    recs: list = []
    asset_recs: list = []

    # 1. 大類資產輪動共識 (Asset-Class Allocation Consensus)
    ac_data = (report.get("asset_classes") or {}).get("classes", {})
    for cls_key, info in ac_data.items():
        if info.get("consensus") in ("up", "down"):
            up = info["consensus"] == "up"
            verb = "增碼" if up else "減碼"
            n = len(info["etfs_up"] if up else info["etfs_down"])
            action_desc = "機構資金轉向風險配置/加碼" if (up and cls_key == "stock") else "機構資金防守/轉向避險資產" if (up and cls_key in ("cash", "bond")) else "機構適度減碼防守性資產" if (not up and cls_key in ("cash", "bond")) else "機構籌碼調整"
            asset_recs.append(Recommendation(
                rec_id=f"etf_ac:{cls_key}", category="etf",
                direction="多" if up else "空",
                verdict=(f"🌐 【大類資產輪動】{info['consensus_pct']:.0f}% 主動式 ETF 同步{verb}"
                         f"「{info['name']}」▶ {action_desc}"),
                basis=(f"近 {window} 天內 {n}/{info['evaluated']} 檔主動式 ETF 同向調整此大類資產配置，"
                       f"達 ≥50% 多數性共識即成立。"),
                detail_sections=[_section(
                    "大類資產輪動共識公式",
                    formula="共識比例 = 同向調整此大類的 ETF 數 ÷ 有評估此大類的 ETF 數；≥ consensus_threshold(0.5) 且方向多於反向才成立",
                    substitution=(f"= {n}/{info['evaluated']} = {info['consensus_pct']:.0f}%　"
                                  f"平均資產配置權重變動 {info['avg_delta_pp']:+.1f}pp（{verb}）"),
                    explanation="讀取各主動式 ETF 每日真實 asset_classes 快照，於 14 天緊湊視窗比較最早 vs 最新配置；平手（多空檔數相等）歸 mixed 不計入，避免系統性多頭偏誤。")],
            ))

    # 2. 個股同時買入/賣出共識 (Symbol Level Buy/Sell Consensus)
    multi = [
        (sym, info) for sym, info in rank_symbol_trends(report, min_etfs_evaluated=min_etfs_evaluated, top_n=top_n)
        if info["consensus"] in ("up", "down")
    ]
    for sym, info in multi:
        up = info["consensus"] == "up"
        emoji = "🟢 【同時買入】" if up else "🔴 【同時賣出】"
        verb = "同步增碼 (買入)" if up else "同步減碼 (賣出)"
        n = len(info["etfs_up"] if up else info["etfs_down"])
        value_s = (f"，估計合計{'加碼' if up else '減碼'}約 {_fmt_usd(info['est_total_value_delta'])}"
                   if info.get("est_total_value_delta") else "")
        secs = [_section(
            "個股多數性共識公式（雙真實訊號同向）",
            formula="共識比例 = 同向 ETF 數 ÷ 評估 ETF 數；需 pct_up > pct_down（平手歸 mixed）；每檔須『真實股數變化 Δ』與『權重變化 Δ（門檻 0.5pp）』同向才計為增/減碼",
            substitution=f"= {n}/{info['etfs_evaluated']} = {info['consensus_pct']:.0f}% 同向{value_s}",
            explanation="真實股數由各快照當日 AUM×權重÷真實持股價反推；兩個獨立真實訊號任一缺席或方向不一致一律歸『持平』、不計入，確保訊號為真實交易而非雜訊。")]
        stance_sec = _etf_stance_section(sym, up, stance)
        if stance_sec:
            secs.append(stance_sec)
        secs.append(_etf_backtest_section("up" if up else "down", backtest))
        basis = (f"跨基金多數性共識 {info['consensus_pct']:.0f}% 一致，且每檔皆為真實股數變化與權重變化"
                 f"同向才計入。")
        position_note = _etf_position_note(sym, up, stance)
        if position_note:
            basis = f"{basis} {position_note}"
        period = _period_label(info.get("first_date"), info.get("last_date"))
        period_s = f"（{period}）" if period != "—" else ""
        recs.append(Recommendation(
            rec_id=f"etf_sym:{sym}", category="etf",
            direction="多" if up else "空",
            verdict=f"{emoji}{sym}：{n}/{info['etfs_evaluated']} 檔追蹤中的主動式 ETF {verb}{period_s}",
            basis=basis,
            detail_sections=secs,
        ))

    # Asset-class rotation is context, not a substitute for a named position.
    # Append it only after exact security-level buy/sell consensus so a generic
    # stockPosition row cannot crowd the requested position breakdown out.
    recs.extend(asset_recs)

    # 3. 規模性大額變動
    for c in rank_scale_events(report, top_n=top_n):
        up = c["direction"] == "up"
        verb = "大幅加碼" if up else "大幅減碼"
        recs.append(Recommendation(
            rec_id=f"etf_scale:{c['etf']}:{c['symbol']}", category="etf",
            direction="多" if up else "空",
            verdict=f"💰 【規模性大額變動】{c['etf']} {verb} {c['symbol']}（{c['first_date']}～{c['last_date']}）",
            basis="單一基金的大額真實部位變動——刻意不套用跨基金共識門檻，讓單一大額動作也能被看見。",
            detail_sections=[_section(
                "規模性大額變動門檻",
                formula="計入條件：|市值變化| ≥ $5M 且 |市值變化| ÷ 該基金 AUM ≥ 0.5%（雙重門檻避免小基金雜訊）",
                substitution=f"{c['etf']} 於 {c['first_date']}～{c['last_date']} {verb} {c['symbol']}，估計市值變化約 {_fmt_usd(c['value_delta'])}",
                explanation="以連續真實快照的持股市值差分衍生；規模性與多數性互補——前者看單一大額動作，後者看跨基金一致性。")],
        ))

    return recs


def generate_etf_conclusions(
    report: dict,
    min_etfs_evaluated: int = 4,
    top_n: int = 5,
    positions=None,
    backtest: "Optional[dict]" = None,
) -> list[str]:
    """薄 wrapper（bug#00117）：以 generate_etf_recommendations 為單一真理來源，投影為
    主頁用的「一句話」字串清單（結論＋判斷依據）。畫面完整三層改由 recs 直接渲染。"""
    from .shared import dashboard_line
    return [dashboard_line(r) for r in generate_etf_recommendations(
        report, min_etfs_evaluated=min_etfs_evaluated, top_n=top_n,
        positions=positions, backtest=backtest)]


# ─────────────────────────────────────────────────────────────────────────────
# Per-ETF active stock-selection tilt + daily cross-fund breadth stance
# ─────────────────────────────────────────────────────────────────────────────
# 使用者需求（2026-07）：「透過各 ETF 主動選股，觀察出趨勢並且 by daily check 顯示
# 多空建議」。現行 compute_symbol_trends 只有「個股跨基金共識」，缺少每一檔 ETF
# 自己這段視窗在主動加/減什麼、淨傾向偏多還偏空。此層補上
# 該視圖，並把各檔傾向聚合成一個「每日主動選股多空廣度」讀數。
#
# 紀律不變：不重算、不打網路——完全從 compute_symbol_trends() 已算好的同一份 report
# 的 raw_contributions（雙真實訊號同向的個股加/減碼事件）衍生，單一真理來源。


def compute_etf_selection_tilt(
    report: dict,
    tilt_min_net: float = 0.1,
    stance_breadth_min: float = 0.2,
) -> dict:
    """Per-ETF active stock-selection tilt + daily cross-fund breadth stance,
    derived from the SAME report that compute_symbol_trends() returns (single
    source of truth; no recompute, no network).

    「主動選股」= the dual-signal (real share-count delta AND weight delta must
    agree) individual-holding accumulate/reduce events already present in
    report['raw_contributions']. For each ETF we net its up vs down holdings:

      net_score = (up_n - down_n) / evaluated        (evaluated incl. flats,
                                                       so noise is diluted, not
                                                       amplified — conservative)

    tilt: 'long' if net_score > tilt_min_net, 'short' if < -tilt_min_net, else
    'neutral'. Aggregate breadth = (etfs_long - etfs_short) / etfs_evaluated,
    mapped to a daily stance ('long'/'short'/'neutral'); 'insufficient' when no
    ETF is ready (honest "資料累積中", never a fabricated direction).
    """
    contribs = report.get("raw_contributions", [])
    by_etf: dict[str, list[dict]] = {}
    for c in contribs:
        by_etf.setdefault(c["etf"], []).append(c)

    etfs: dict[str, dict] = {}
    for etf, cs in by_etf.items():
        ups = [c for c in cs if c["direction"] == "up"]
        downs = [c for c in cs if c["direction"] == "down"]
        flats = [c for c in cs if c["direction"] == "flat"]
        evaluated = len(cs)
        moved = len(ups) + len(downs)
        net_score = ((len(ups) - len(downs)) / evaluated) if evaluated else 0.0
        value_net = sum(
            (c["value_delta"] or 0.0)
            if c["direction"] in ("up", "down") else 0.0
            for c in cs
        )
        if moved == 0:
            tilt = "neutral"
        elif net_score > tilt_min_net:
            tilt = "long"
        elif net_score < -tilt_min_net:
            tilt = "short"
        else:
            tilt = "neutral"
        top_buys = sorted(
            [c for c in ups if c["value_delta"] is not None],
            key=lambda c: abs(c["value_delta"]), reverse=True,
        )[:3]
        top_sells = sorted(
            [c for c in downs if c["value_delta"] is not None],
            key=lambda c: abs(c["value_delta"]), reverse=True,
        )[:3]
        etfs[etf] = {
            "up_n": len(ups),
            "down_n": len(downs),
            "flat_n": len(flats),
            "evaluated": evaluated,
            "net_score": round(net_score, 3),
            "value_net": value_net,
            "tilt": tilt,
            "top_buys": [c["symbol"] for c in top_buys],
            "top_sells": [c["symbol"] for c in top_sells],
        }

    evaluated_etfs = [e for e, v in etfs.items() if v["evaluated"] > 0]
    longs = [e for e in evaluated_etfs if etfs[e]["tilt"] == "long"]
    shorts = [e for e in evaluated_etfs if etfs[e]["tilt"] == "short"]
    neutrals = [e for e in evaluated_etfs if etfs[e]["tilt"] == "neutral"]
    n = len(evaluated_etfs)
    breadth = ((len(longs) - len(shorts)) / n) if n else 0.0
    if n == 0:
        stance = "insufficient"
    elif breadth >= stance_breadth_min:
        stance = "long"
    elif breadth <= -stance_breadth_min:
        stance = "short"
    else:
        stance = "neutral"

    return {
        "as_of": report.get("as_of"),
        "window_days": report.get("window_days"),
        "etfs": etfs,
        "aggregate": {
            "etfs_long": len(longs),
            "etfs_short": len(shorts),
            "etfs_neutral": len(neutrals),
            "etfs_evaluated": n,
            "breadth": round(breadth, 3),
            "stance": stance,
        },
    }


def _etf_window_label(tilt_or_report: dict | None) -> str:
    days = (tilt_or_report or {}).get("window_days") or 14
    return f"近 {days} 日"


def etf_stance_recommendation(tilt: dict, backtest: "Optional[dict]" = None) -> "list":
    """主動選股廣度 stance 的三層結構化建議（bug#00117）。回傳 list（0 或 1 則），
    與 etf_stance_phrase 共用同一 tilt 輸出，維持首頁卡片與建議頁讀數一致。"""
    from .shared import Recommendation, _section
    agg = (tilt or {}).get("aggregate", {})
    n = agg.get("etfs_evaluated", 0)
    st = agg.get("stance")
    window = _etf_window_label(tilt)
    if not n or st == "insufficient":
        return [Recommendation(
            rec_id="etf_stance", category="etf_stance", direction=None,
            verdict=f"📊 {window}主動選股傾向：資料累積中（就緒 ETF 不足，無法判斷方向）",
            basis="", detail_sections=[_section(
                "主動選股廣度公式",
                formula="廣度 breadth = (偏多 ETF 數 − 偏空 ETF 數) ÷ 就緒 ETF 數；就緒 ETF = 0 時誠實回『資料累積中』",
                explanation="每檔 ETF 先算主動選股淨傾向 net_score，再跨就緒 ETF 聚合成廣度；資料不足絕不臆造方向。視窗是可比較的揭露區間，不是日頻成交。")],
        )]
    label = {"long": "🟢 偏多", "short": "🔴 偏空", "neutral": "⚪ 中性觀望"}.get(st, "⚪ 中性觀望")
    direction = "多" if st == "long" else "空" if st == "short" else "觀望"
    secs = [_section(
        "主動選股廣度公式",
        formula=("每檔 ETF：net_score = (加碼數 − 減碼數) ÷ 評估數（分母含持平以稀釋雜訊、偏保守）；"
                 "偏多/偏空門檻 |net_score| > 0.1。整體廣度 breadth = (偏多 ETF − 偏空 ETF) ÷ 就緒 ETF"),
        substitution=(f"= ({agg['etfs_long']} − {agg['etfs_short']}) ÷ {agg['etfs_evaluated']} "
                      f"= {agg['breadth']:+.2f} → {label}"),
        explanation="完全從 compute_symbol_trends 的 raw_contributions（雙真實訊號同向的加/減碼事件）衍生，不重算、不打網路。Yahoo 前十大常為月頻揭露，未更新不是「今日無交易」。")]
    if st in ("long", "short"):
        secs.append(_etf_backtest_section("up" if st == "long" else "down", backtest))
    return [Recommendation(
        rec_id="etf_stance", category="etf_stance", direction=direction,
        verdict=f"📊 {window}主動選股傾向：{label}",
        basis=(f"就緒 ETF 中 {agg['etfs_long']} 檔偏多／{agg['etfs_short']} 檔偏空／{agg['etfs_neutral']} 檔中性，"
               f"廣度 {agg['breadth']:+.2f} 映射整體傾向。"),
        detail_sections=secs,
    )]


def etf_stance_phrase(tilt: dict) -> str:
    """薄 wrapper（bug#00117）：以 etf_stance_recommendation 為單一真理來源，投影為一句話
    （首頁卡片截取＋ActiveETFsScreen 建議頁共用，兩處讀數一致）。"""
    from .shared import dashboard_line
    recs = etf_stance_recommendation(tilt)
    return dashboard_line(recs[0]) if recs else "📊 近 14 日主動選股傾向：資料累積中"


def recommendation_symbol(rec) -> "Optional[str]":
    """從 rec_id 取出個股代碼。etf_sym:NVDA、etf_scale:ARKK:NVDA；其餘回 None。"""
    rec_id = getattr(rec, "rec_id", "") or ""
    if rec_id.startswith("etf_sym:"):
        symbol = rec_id.split(":", 1)[1]
        return symbol.upper() or None
    if rec_id.startswith("etf_scale:"):
        parts = rec_id.split(":")
        if len(parts) >= 3 and parts[-1]:
            return parts[-1].upper()
    return None


def partition_etf_recommendations(
    recs: list,
    held: "Optional[set]" = None,
    tracked: "Optional[set]" = None,
) -> "tuple[list, list]":
    """把個股／規模性建議分成『與你持倉或追蹤相關』與『其他』。

    大類資產輪動沒有單一代碼，一律歸其他。stance 建議不應傳入（由呼叫端單獨渲染）。
    """
    related_syms = {str(s).upper() for s in (held or set()) | (tracked or set())}
    related: list = []
    other: list = []
    for rec in recs or []:
        symbol = recommendation_symbol(rec)
        if symbol and symbol in related_syms:
            related.append(rec)
        else:
            other.append(rec)
    return related, other


def normalize_etf_watchlist_symbol(raw: str) -> "Optional[str]":
    """Normalize a user-entered observation ticker. Taiwan listings are rejected."""
    token = (raw or "").strip().upper().lstrip("$")
    if not token:
        return None
    if token.endswith(".TW") or token.endswith(".TWO"):
        return None
    letters = token.replace(".", "")
    if not letters.isalpha() or len(token) > 10:
        return None
    return token


def suggested_etf_watchlist(positions) -> list[str]:
    """US stock / ETF / option-underlying symbols from the user's positions."""
    from .shared import is_taiwan_position
    seen: list[str] = []
    for position in positions or []:
        if is_taiwan_position(position):
            continue
        raw = (
            getattr(position, "underlying", None)
            if getattr(position, "instrument_type", None) == "option"
            else getattr(position, "symbol", None)
        )
        symbol = normalize_etf_watchlist_symbol(str(raw or ""))
        if symbol and symbol not in seen:
            seen.append(symbol)
    return seen


def _period_label(first, last) -> str:
    if first and last and first != last:
        return f"{first}～{last}"
    return first or last or "—"


def holding_on_watchlist(
    holding: dict,
    watchlist,
    name_index: "Optional[dict]" = None,
) -> bool:
    """True when a holdings / history row is one of the watched US tickers."""
    wanted = {str(s).upper() for s in (watchlist or []) if str(s).strip()}
    if not wanted:
        return False
    for key in ("symbol", "ticker"):
        token = normalize_etf_watchlist_symbol(str(holding.get(key) or ""))
        if token and token in wanted:
            return True
    if name_index:
        ticker = resolve_position_ticker(holding, name_index)
        if ticker and ticker.upper() in wanted:
            return True
    return False


def holding_display_symbol(holding: dict, name_index: "Optional[dict]" = None) -> str:
    """Ticker for the holdings-detail Symbol column.

    ARK / 13F rows often carry CUSIP or FIGI. Those stay as fallbacks only —
    the column is labeled Symbol, so a watched name must show TSLA not 88160R101.
    """
    for key in ("symbol", "ticker"):
        token = normalize_etf_watchlist_symbol(str(holding.get(key) or ""))
        if token:
            return token
    if name_index:
        resolved = resolve_position_ticker(holding, name_index)
        token = normalize_etf_watchlist_symbol(str(resolved or ""))
        if token:
            return token
    return str(
        holding.get("figi")
        or holding.get("cusip")
        or holding.get("symbol")
        or holding.get("ticker")
        or "—"
    )


def watchlist_etf_activity(report: dict, watchlist) -> list[dict]:
    """One row per watched symbol: confirmed ETF buy/sell plus observation dates."""
    ordered: list[str] = []
    for raw in watchlist or []:
        symbol = normalize_etf_watchlist_symbol(str(raw)) or str(raw).strip().upper()
        if symbol and symbol not in ordered:
            ordered.append(symbol)

    by_upper = {
        str(key).upper(): info for key, info in (report.get("symbols") or {}).items()
    }
    contribs = report.get("raw_contributions") or []
    rows: list[dict] = []
    for symbol in ordered:
        info = by_upper.get(symbol) or {}
        related = [
            item for item in contribs
            if str(item.get("symbol") or "").upper() == symbol
        ]
        ups = [item for item in related if item.get("direction") == "up"]
        downs = [item for item in related if item.get("direction") == "down"]
        etfs_up = list(dict.fromkeys(
            list(info.get("etfs_up") or [])
            + [item.get("etf") for item in ups if item.get("etf")]
        ))
        etfs_down = list(dict.fromkeys(
            list(info.get("etfs_down") or [])
            + [item.get("etf") for item in downs if item.get("etf")]
        ))
        dates = [
            value for value in (
                info.get("first_date"),
                info.get("last_date"),
                *[item.get("first_date") for item in related],
                *[item.get("last_date") for item in related],
            )
            if value
        ]
        consensus = info.get("consensus")
        rows.append({
            "symbol": symbol,
            "consensus": consensus,
            "etfs_up": [etf for etf in etfs_up if etf],
            "etfs_down": [etf for etf in etfs_down if etf],
            "etfs_evaluated": info.get("etfs_evaluated") or len(related),
            "consensus_pct": info.get("consensus_pct"),
            "first_date": min(dates) if dates else None,
            "last_date": max(dates) if dates else None,
            "has_trade": consensus in ("up", "down") or bool(ups or downs),
            "status": info.get("status"),
        })
    return rows


def _watchlist_rule_section(substitution: str) -> dict:
    from .shared import _section
    return _section(
        "觀察清單標的的 ETF 買賣",
        formula="只列出觀察清單上的股票；買賣須真實股數變化與權重變化同向。期間＝可比較快照的最早～最晚日期。",
        substitution=substitution,
        explanation="未列入清單的持股不顯示。來源未更新不是「今日無交易」。",
    )


def _watchlist_activity_recommendation(row: dict, positions=None, backtest=None):
    from .shared import Recommendation, position_stance_by_symbol
    symbol = row["symbol"]
    period = _period_label(row.get("first_date"), row.get("last_date"))
    stance = position_stance_by_symbol(positions) if positions else {}
    evaluated = row.get("etfs_evaluated") or 0
    if row.get("status") == "source_unchanged" and not row.get("has_trade"):
        return Recommendation(
            rec_id=f"etf_watch:{symbol}", category="etf", direction=None,
            verdict=f"⚪ {symbol}：來源持股揭露未更新（不是今日無交易）",
            basis=f"最近可比較期間 {period}。",
            detail_sections=[_watchlist_rule_section(
                f"{symbol} {period} 來源未更新　0/{evaluated} 檔達雙真實訊號同向",
            )],
        )
    if not row.get("has_trade"):
        return Recommendation(
            rec_id=f"etf_watch:{symbol}", category="etf", direction=None,
            verdict=f"⚪ {symbol}：本視窗無確認的主動式 ETF 增減持",
            basis=f"觀察期間 {period}。未達雙真實訊號同向，故不列買賣。" if period != "—" else "",
            detail_sections=[_watchlist_rule_section(
                f"{symbol} {period} 無確認買賣　0/{evaluated} 檔達雙真實訊號同向",
            )],
        )
    up = row.get("consensus") == "up" or (
        row.get("etfs_up") and not row.get("etfs_down")
    )
    down = row.get("consensus") == "down" or (
        row.get("etfs_down") and not row.get("etfs_up")
    )
    if up and not down:
        emoji, verb, direction = "🟢", "買入／增碼", "多"
        funds = row.get("etfs_up") or []
    elif down and not up:
        emoji, verb, direction = "🔴", "賣出／減碼", "空"
        funds = row.get("etfs_down") or []
    else:
        emoji, verb, direction = "⚪", "買賣同時出現", "觀望"
        funds = list(dict.fromkeys((row.get("etfs_up") or []) + (row.get("etfs_down") or [])))
    n = len(funds)
    evaluated = row.get("etfs_evaluated") or n
    pct = row.get("consensus_pct")
    pct_s = f"，共識 {pct:.0f}%" if isinstance(pct, (int, float)) else ""
    fund_s = "、".join(funds[:6]) + ("…" if len(funds) > 6 else "")
    basis = f"期間 {period}。{n}/{evaluated} 檔主動式 ETF {verb}{pct_s}。"
    if fund_s:
        basis += f" 來源：{fund_s}。"
    note = _etf_position_note(symbol, direction == "多", stance)
    if note:
        basis = f"{basis} {note}"
    secs = [_watchlist_rule_section(
        f"{symbol} {period} {verb}　{n}/{evaluated} 檔",
    )]
    if direction in ("多", "空"):
        stance_sec = _etf_stance_section(symbol, direction == "多", stance)
        if stance_sec:
            secs.append(stance_sec)
        secs.append(_etf_backtest_section("up" if direction == "多" else "down", backtest))
    return Recommendation(
        rec_id=f"etf_watch:{symbol}", category="etf", direction=direction,
        verdict=f"{emoji} {symbol}：主動式 ETF {verb}（{period}）",
        basis=basis,
        detail_sections=secs,
    )


def etf_source_freshness_lines(report: dict) -> list[str]:
    """資料新鮮度說明。來源未更新不得寫成持平或今日無交易。"""
    window = report.get("window_days", 14)
    ready = report.get("etfs_ready_count", 0)
    total = report.get("etfs_total_count", 0)
    freshness = report.get("source_freshness") or {}
    unchanged = freshness.get("sources_unchanged", 0)
    since = freshness.get("oldest_state_since")
    days = freshness.get("max_unchanged_days")
    lines = [
        f"資料新鮮度：{ready}/{total} 檔 ETF 在近 {window} 日有新的持股狀態"
        f"（{report.get('etfs_ready_pct', 0):.0f}%）　更新於 {report.get('as_of') or '—'}。"
    ]
    if freshness.get("all_sources_unchanged"):
        span = f"（已 {days} 天）" if days else ""
        since_s = since or "—"
        lines.append(
            f"來源持股揭露停滯：本視窗內沒有任何檔出現新的持股狀態，"
            f"最早自 {since_s} 起內容未變{span}。無法產生可執行建議。"
        )
    elif unchanged:
        span = f"（已 {days} 天）" if days else ""
        since_s = f"自 {since} 起" if since else ""
        lines.append(
            f"{unchanged} 檔{since_s}揭露內容未變{span}。"
            "Yahoo 前十大多為月頻，未更新不是今日無交易。"
        )
    return lines


def render_etf_advice_view(
    report: dict,
    tilt: dict,
    *,
    positions=None,
    watchlist=None,
    held: "Optional[set]" = None,
    tracked: "Optional[set]" = None,
    backtest=None,
    tilt_backtest=None,
    min_etfs_evaluated: int = 4,
) -> "tuple[str, dict]":
    """建議頁：只列觀察清單標的的 ETF 買賣與期間。回傳 (markup, recs_by_id)。"""
    from .shared import render_detail_recs

    # held/tracked remain accepted so older callers do not break; display is
    # watchlist-only when a list is provided.
    del held, tracked, tilt_backtest, min_etfs_evaluated
    mapping: dict = {}
    sections: list[str] = []
    freshness = "\n".join(
        f"[dim]{line}[/dim]" for line in etf_source_freshness_lines(report)
    )
    if freshness:
        sections.append(freshness)

    ordered = [
        normalize_etf_watchlist_symbol(str(raw)) or str(raw).strip().upper()
        for raw in (watchlist or [])
    ]
    ordered = [symbol for symbol in dict.fromkeys(ordered) if symbol]
    if not ordered:
        sections.append(
            "[bold yellow]觀察清單的 ETF 買賣[/bold yellow]\n"
            "[dim]尚未設定觀察清單。按 [bold]w[/bold] 加入你在乎的美股標的；"
            "之後只顯示這些股票的大型 ETF 買賣與時間。[/dim]"
        )
        return "\n\n".join(sections), mapping

    recs = [
        _watchlist_activity_recommendation(row, positions=positions, backtest=backtest)
        for row in watchlist_etf_activity(report, ordered)
    ]
    body, mapping = render_detail_recs(
        recs,
        header="[bold yellow]觀察清單的 ETF 買賣[/bold yellow]",
        start=0,
    )
    sections.append(body)
    return "\n\n".join(sections), mapping


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward backtest for the cross-ETF accumulation consensus (bug#00092)
# ─────────────────────────────────────────────────────────────────────────────
# Same discipline as calibration.backtest_verdicts (期權): 100% offline, zero
# network, no backfill, no fabrication — built purely on the real daily ETF
# snapshots storage has been accumulating (etf_cache/history/*.jsonl, each holding
# carrying its real per-date price). The signal being validated is *exactly* the
# one the ETF card shows: compute_symbol_trends()'s cross-ETF「多數性」consensus
# (share-count AND weight must agree). For each historical day T taken "as now",
# we recompute that consensus using ONLY snapshots ≤ T (no look-ahead), then check
# whether the consensus symbol's own real forward price (median real holding price
# across the ETFs that hold it) moved the predicted way over ≥ horizon calendar
# days. Hit rates per look-ahead are compared to the baseline up-rate of the same
# display universe, giving the signal's edge. Sample < min_signals → honestly
# flagged "資料累積中", never a falsely confident number (0 on day one — by design).

_etf_bt_cache: dict = {}
_ETF_BT_CACHE_MAX = 8


def _etf_data_signature(snapshots_by_etf: dict) -> tuple:
    return tuple(sorted(
        (e, len(s or []), (s[-1].get("date") if s else None))
        for e, s in snapshots_by_etf.items()
    ))


def _build_symbol_price_series(snapshots_by_etf: dict[str, list[dict]]) -> dict[str, list]:
    """{symbol: [(date, price), ...] ascending} — for each date, the median of the
    real per-date holding prices reported across every ETF that held the symbol
    that day. Only real prices are used (None dropped, never fabricated)."""
    by_sym_date: dict[str, dict[str, list[float]]] = {}
    for snaps in snapshots_by_etf.values():
        for snap in (snaps or []):
            d = snap.get("date")
            if not d:
                continue
            for h in snap.get("holdings", []) or []:
                sym = h.get("symbol")
                price = h.get("price")
                if sym is None or price is None or price <= 0:
                    continue
                by_sym_date.setdefault(sym, {}).setdefault(d, []).append(price)

    def _median(xs: list[float]) -> float:
        xs = sorted(xs)
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0

    series: dict[str, list] = {}
    for sym, dmap in by_sym_date.items():
        series[sym] = [(d, _median(v)) for d, v in sorted(dmap.items())]
    return series


def _price_on_or_before(series: list, target: str):
    """Most recent (date, price) with date <= target; None if none."""
    out = None
    for d, px in series:
        if d <= target:
            out = px
        else:
            break
    return out


def _price_on_or_after(series: list, parsed_series, target_date):
    """First price whose date >= target_date; None if none. parsed_series is the
    pre-parsed date list aligned with series."""
    for (d, px), pd_ in zip(series, parsed_series):
        if pd_ >= target_date:
            return px
    return None


def backtest_etf_consensus(
    snapshots_by_etf: dict[str, list[dict]],
    horizons: tuple = (1, 5, 10, 14, 30, 60),  # 含 30/60 天長線前瞻期（bug#00106）
    window_days: int = 14,
    min_etfs_evaluated: int = 4,
    consensus_threshold: float = 0.5,
    flat_threshold_pp: float = 0.5,
    min_signals: int = 20,
) -> dict:
    """Walk-forward calibration of the cross-ETF「多數性」consensus (1/5/10-day
    look-aheads). Returns a report shaped identically to
    calibration.backtest_verdicts so calibration.calibration_status_label() works
    on it unchanged.

    by_horizon: {h: {baseline_up_rate, baseline_n, up_n, up_hit_rate, up_mean_fwd,
                     down_n, down_hit_rate, down_mean_fwd, evaluated_signals, ready}}
    """
    cache_key = (_etf_data_signature(snapshots_by_etf), tuple(horizons),
                 window_days, min_etfs_evaluated, round(consensus_threshold, 3),
                 round(flat_threshold_pp, 3), min_signals)
    if cache_key in _etf_bt_cache:
        return _etf_bt_cache[cache_key]

    price_series = _build_symbol_price_series(snapshots_by_etf)
    parsed_series = {
        sym: [datetime.strptime(d, "%Y-%m-%d").date() for d, _ in ser]
        for sym, ser in price_series.items()
    }

    signal_dates = sorted({
        snap.get("date")
        for snaps in snapshots_by_etf.values()
        for snap in (snaps or [])
        if snap.get("date")
    })

    up = {h: [] for h in horizons}
    down = {h: [] for h in horizons}
    baseline = {h: [] for h in horizons}
    records: list = []  # bug#00094: 逐訊號紀錄，供子區間穩定性檢定
    symbols_seen: set = set()

    for T in signal_dates:
        T_date = datetime.strptime(T, "%Y-%m-%d").date()
        upto = {
            e: [s for s in (snaps or []) if s.get("date") and s["date"] <= T]
            for e, snaps in snapshots_by_etf.items()
        }
        upto = {e: s for e, s in upto.items() if s}
        report = compute_symbol_trends(
            upto, window_days=window_days,
            flat_threshold_pp=flat_threshold_pp,
            consensus_threshold=consensus_threshold, as_of=T,
        )
        for sym, info in report.get("symbols", {}).items():
            if info.get("etfs_evaluated", 0) < min_etfs_evaluated:
                continue  # display universe only (same gate as the card)
            ser = price_series.get(sym)
            if not ser:
                continue
            entry = _price_on_or_before(ser, T)
            if not entry or entry <= 0:
                continue
            psd = parsed_series[sym]
            consensus = info.get("consensus")
            contributed = False
            for h in horizons:
                from datetime import timedelta as _td
                exit_px = _price_on_or_after(ser, psd, T_date + _td(days=h))
                if not exit_px or exit_px <= 0:
                    continue
                fwd = exit_px / entry - 1.0
                baseline[h].append(fwd)
                if consensus == "up":
                    up[h].append(fwd)
                    records.append({"date": T, "h": h, "dir": "up", "hit": fwd > 0})
                elif consensus == "down":
                    down[h].append(fwd)
                    records.append({"date": T, "h": h, "dir": "down", "hit": fwd < 0})
                contributed = True
            if contributed:
                symbols_seen.add(sym)

    def _hit_rate(xs, expect_up):
        if not xs:
            return None
        hits = sum(1 for x in xs if (x > 0) == expect_up)
        return hits / len(xs)

    def _mean(xs):
        return (sum(xs) / len(xs)) if xs else None

    by_horizon = {}
    for h in horizons:
        evaluated = len(up[h]) + len(down[h])
        by_horizon[h] = {
            "baseline_up_rate": _hit_rate(baseline[h], True),
            "baseline_n": len(baseline[h]),
            "up_n": len(up[h]),
            "up_hit_rate": _hit_rate(up[h], True),
            "up_mean_fwd": _mean(up[h]),
            "down_n": len(down[h]),
            "down_hit_rate": _hit_rate(down[h], False),
            "down_mean_fwd": _mean(down[h]),
            "evaluated_signals": evaluated,
            "ready": evaluated >= min_signals,
        }

    result = {
        "horizons": list(horizons),
        "window_days": window_days,
        "min_etfs_evaluated": min_etfs_evaluated,
        "min_signals": min_signals,
        "symbols_with_price": len(symbols_seen),
        "total_signal_days": len(signal_dates),
        "first_date": signal_dates[0] if signal_dates else None,
        "last_date": signal_dates[-1] if signal_dates else None,
        "by_horizon": by_horizon,
    }

    from .backtest_stats import attach_significance
    attach_significance(result, records)

    if len(_etf_bt_cache) >= _ETF_BT_CACHE_MAX:
        _etf_bt_cache.clear()
    _etf_bt_cache[cache_key] = result
    return result


def etf_backtest_note(direction: str, backtest: "Optional[dict]", min_signals: int = 20) -> str:
    """One-line 回測 hit-rate suffix for an ETF「多數性」bullet, matching the
    style of the options verdict cards. `direction` is "up"/"down". Picks the
    look-ahead with the most samples for that direction (prefer 5 → 10 → 1)."""
    if not backtest or not backtest.get("by_horizon"):
        return "　▶ 回測：訊號樣本累積中，命中率尚無法估計"
    key_n = "up_n" if direction == "up" else "down_n"
    key_hit = "up_hit_rate" if direction == "up" else "down_hit_rate"
    by_h = backtest["by_horizon"]
    order = [h for h in (5, 10, 1) if h in by_h] + [h for h in by_h if h not in (5, 10, 1)]
    best_h = max(order, key=lambda h: (by_h[h].get(key_n) or 0)) if order else None
    if best_h is None or not by_h[best_h].get(key_n):
        return "　▶ 回測：訊號樣本累積中，命中率尚無法估計"
    st = by_h[best_h]
    n = st[key_n]
    hit = st[key_hit]
    base = st.get("baseline_up_rate")
    edge_s = ""
    if hit is not None and base is not None:
        edge = (hit - base) if direction == "up" else (hit - (1 - base))
        edge_s = f"，超額 {edge * 100:+.0f}pp"
    note = f"　▶ 回測：前瞻{best_h}日同向共識命中率 {hit * 100:.0f}%（n={n}{edge_s}）"
    from .backtest_stats import significance_phrase
    note += significance_phrase(backtest, best_h, direction)
    if n < max(5, backtest.get("min_signals", min_signals) // 2):
        note += "（樣本偏少，僅供參考）"
    return note


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward backtest for the daily active-selection breadth stance (C-b)
# ─────────────────────────────────────────────────────────────────────────────
# 設計決策 C-b：把每日聚合的「主動選股多空廣度 stance」對照一個「持有宇集市場代理」
# 的前瞻報酬來驗證。某歷史日 T「當作當下」，只用 ≤T 的真實快照重推 stance；市場代理
# 的前瞻 h 日報酬 = 該日所有有真實價格個股的 (price_{T+h}/price_T − 1) 的橫斷面中位數
# （_build_symbol_price_series，100% 真實快照、零網路），代表這些主動經理人操作的大盤。
# 與 backtest_etf_consensus 完全相同的 walk-forward 紀律、by_horizon 形狀與顯著性接法，
# 故 calibration_status_label / significance_phrase 可直接沿用。
# 內生性註記：stance 由「真實股數變動」（實際交易）衍生，代理報酬為價格報酬，兩者大致
# 獨立；殘餘動能自相關已由 baseline+edge 與 ESS（backtest_stats）部分抵銷。

_etf_tilt_bt_cache: dict = {}
_ETF_TILT_BT_CACHE_MAX = 8


def backtest_etf_selection_tilt(
    snapshots_by_etf: dict[str, list[dict]],
    horizons: tuple = (1, 5, 10, 14, 30, 60),
    window_days: int = 14,
    tilt_min_net: float = 0.1,
    stance_breadth_min: float = 0.2,
    consensus_threshold: float = 0.5,
    flat_threshold_pp: float = 0.5,
    min_signals: int = 20,
) -> dict:
    """Walk-forward validation of the daily active-selection breadth stance
    against the held-universe market-proxy forward return (design C-b).

    Returns a report shaped identically to backtest_etf_consensus (by_horizon
    with baseline_up_rate / up_* / down_* / ready), so the same calibration
    status label and significance helpers work unchanged. One signal per date
    (the aggregate stance), not per symbol.
    """
    cache_key = (_etf_data_signature(snapshots_by_etf), tuple(horizons),
                 window_days, round(tilt_min_net, 3), round(stance_breadth_min, 3),
                 round(consensus_threshold, 3), round(flat_threshold_pp, 3), min_signals)
    if cache_key in _etf_tilt_bt_cache:
        return _etf_tilt_bt_cache[cache_key]

    price_series = _build_symbol_price_series(snapshots_by_etf)
    parsed_series = {
        sym: [datetime.strptime(d, "%Y-%m-%d").date() for d, _ in ser]
        for sym, ser in price_series.items()
    }

    signal_dates = sorted({
        snap.get("date")
        for snaps in snapshots_by_etf.values()
        for snap in (snaps or [])
        if snap.get("date")
    })

    up = {h: [] for h in horizons}
    down = {h: [] for h in horizons}
    baseline = {h: [] for h in horizons}
    records: list = []

    from datetime import timedelta as _td

    for T in signal_dates:
        T_date = datetime.strptime(T, "%Y-%m-%d").date()
        upto = {
            e: [s for s in (snaps or []) if s.get("date") and s["date"] <= T]
            for e, snaps in snapshots_by_etf.items()
        }
        upto = {e: s for e, s in upto.items() if s}
        report = compute_symbol_trends(
            upto, window_days=window_days,
            flat_threshold_pp=flat_threshold_pp,
            consensus_threshold=consensus_threshold, as_of=T,
        )
        tilt = compute_etf_selection_tilt(
            report, tilt_min_net=tilt_min_net, stance_breadth_min=stance_breadth_min,
        )
        stance = tilt["aggregate"]["stance"]
        if stance not in ("long", "short"):
            continue  # no directional call that day → not a signal

        for h in horizons:
            fwds = []
            for sym, ser in price_series.items():
                entry = _price_on_or_before(ser, T)
                if not entry or entry <= 0:
                    continue
                exit_px = _price_on_or_after(ser, parsed_series[sym], T_date + _td(days=h))
                if not exit_px or exit_px <= 0:
                    continue
                fwds.append(exit_px / entry - 1.0)
            proxy_fwd = _median(fwds)
            if proxy_fwd is None:
                continue
            baseline[h].append(proxy_fwd)
            if stance == "long":
                up[h].append(proxy_fwd)
                records.append({"date": T, "h": h, "dir": "up", "hit": proxy_fwd > 0})
            else:
                down[h].append(proxy_fwd)
                records.append({"date": T, "h": h, "dir": "down", "hit": proxy_fwd < 0})

    def _hit_rate(xs, expect_up):
        if not xs:
            return None
        hits = sum(1 for x in xs if (x > 0) == expect_up)
        return hits / len(xs)

    def _mean(xs):
        return (sum(xs) / len(xs)) if xs else None

    by_horizon = {}
    for h in horizons:
        evaluated = len(up[h]) + len(down[h])
        by_horizon[h] = {
            "baseline_up_rate": _hit_rate(baseline[h], True),
            "baseline_n": len(baseline[h]),
            "up_n": len(up[h]),
            "up_hit_rate": _hit_rate(up[h], True),
            "up_mean_fwd": _mean(up[h]),
            "down_n": len(down[h]),
            "down_hit_rate": _hit_rate(down[h], False),
            "down_mean_fwd": _mean(down[h]),
            "evaluated_signals": evaluated,
            "ready": evaluated >= min_signals,
        }

    result = {
        "horizons": list(horizons),
        "window_days": window_days,
        "min_signals": min_signals,
        "total_signal_days": len(signal_dates),
        "first_date": signal_dates[0] if signal_dates else None,
        "last_date": signal_dates[-1] if signal_dates else None,
        "by_horizon": by_horizon,
    }

    from .backtest_stats import attach_significance
    attach_significance(result, records)

    if len(_etf_tilt_bt_cache) >= _ETF_TILT_BT_CACHE_MAX:
        _etf_tilt_bt_cache.clear()
    _etf_tilt_bt_cache[cache_key] = result
    return result
