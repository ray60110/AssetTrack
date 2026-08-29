"""
AssetTrack Textual TUI — 全螢幕事件驅動即時看板

架構說明：
  - AssetTrackApp (App) 為主應用，啟動後推入 DashboardScreen。
  - 所有子操作（部位調整、歷史、快照）皆以 Textual ModalScreen/Screen 實作。
  - cli.py 已完全移除（bug#00056）；本檔案的 main() 為套件唯一命令列進入點。
  - 自動每 60 秒背景刷新報價（獨立 worker thread）。
  - 支援鍵盤快速鍵 1-N / r / q，以及方向鍵捲動 Holdings。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
import calendar
from typing import Optional
from pathlib import Path
import subprocess

from rich.box import Box as RichBox
from rich.panel import Panel
from rich.table import Table

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Label,
    OptionList,
    Select,
    Static,
    TabbedContent,
    TabPane,
    Collapsible,
)
from textual.widgets.option_list import Option

from .models import (
    CashPosition,
    Position,
    cash_value_usd,
    merge_cash_position,
    portfolio_unrealized_performance,
    total_asset_value_usd,
)
from .quotes import (
    enrich_positions_with_quotes, fetch_usdtwd_rate, fetch_beta, cached_beta,
    cached_usdtwd_rate,
    is_market_open,
    SOX_TICKERS, group_positions_by_broker, fetch_earnings_calendar,
    fetch_active_etf_performance, fetch_etf_holdings,
    fetch_prices_batch, estimate_shares,
)
from .auth import (
    AuthError,
    account_exists,
    lock_vault,
    register_account,
    touchid_enrolled,
    unlock_vault,
    unlock_vault_with_touchid,
    verify_password,
)
from .storage import (
    load_manual_positions, save_manual_positions, get_data_dir, seal_user_files,
    load_etf_symbol_cache, save_etf_symbol_cache, etf_symbol_cache_fresh,
    cleanup_old_etf_caches,
    append_etf_daily_snapshot, load_etf_daily_snapshots, prune_etf_history,
    load_options_daily_snapshots, prune_options_history,
    apply_quote_overlay, save_quote_overlay, drop_quote_overlay_keys,
    load_etf_watchlist, save_etf_watchlist, etf_watchlist_is_configured,
)
from .performance import PortfolioPerformanceTracker, YFinanceBenchmarkPrices
from .exposure import calculate_portfolio_exposure
from .analysis import (compute_symbol_trends,
    backtest_etf_consensus, compute_etf_selection_tilt,
    backtest_etf_selection_tilt, render_etf_advice_view,
    watchlist_etf_activity, holding_on_watchlist, holding_display_symbol,
    suggested_etf_watchlist,
    normalize_etf_watchlist_symbol,
    compute_institution_trends, build_ticker_name_index, resolve_position_ticker)
from .options_analysis import (
    compute_observed_regime, compute_portfolio_greeks,
    compute_expected_move,
)
from .options_valuation import (
    RICHNESS_HISTORY_DAYS,
    RV_WINDOW,
    _select_atm_pair,
    days_to_earnings,
    earnings_remaining_note,
    format_richness_history,
    invert_contract_iv_series,
    richness_from_history,
    richness_series,
)
from .shared import render_detail_recs
from .institutional import (
    active_etf_symbols,
    classify_holdings,
    ensure_active_etf_universe,
    ensure_hedge_fund_filings,
    hedge_fund_records,
    load_active_etf_universe,
    load_hedge_fund_cache,
)
from .sec_identity import (
    delete_sec_identity,
    load_sec_identity,
    masked_sec_identity,
    save_sec_identity,
)

# Rich Box: invisible borders except head_row underline and end_section separator
_SEC_BOX = RichBox(
    "    \n"        # top
    "    \n"        # head
    " \u2500\u2500 \n"  # head_row  — column header underline
    "    \n"        # mid_head
    " \u2500\u2500 \n"  # row       — end_section separator
    "    \n"        # mid_foot
    "    \n"        # foot
    "    \n"        # bottom
)


# ─────────────────────────────────────────────────────────────────────────────
# Caching & Utility helpers
# ─────────────────────────────────────────────────────────────────────────────


_last_rate: Optional[float] = None
_last_rate_time: float = 0.0
_EVENTS_REFRESH_INTERVAL_SECONDS = 6 * 60 * 60
_EVENTS_RETRY_INTERVAL_SECONDS = 15 * 60
_DASHBOARD_ANALYSIS_REFRESH_SECONDS = 5 * 60


def _get_cached_usdtwd_rate() -> float:
    """UI-safe last known USDTWD rate. Never hits the network."""
    global _last_rate, _last_rate_time
    rate = cached_usdtwd_rate(default=_last_rate if _last_rate is not None else 32.0)
    if rate > 0:
        _last_rate = rate
        _last_rate_time = time.time()
    return rate


def _tracking_state(user: str):
    return PortfolioPerformanceTracker(
        user=user,
        data_dir=get_data_dir(),
    ).state()


def _process_declared_cash_flow(
    *,
    user: str,
    positions: list[Position],
    cash_positions: list[CashPosition],
    rate: float,
    declaration: dict,
) -> tuple[list[Position], list[CashPosition]]:
    """Persist a declared external flow and mirror it into the cash holdings."""
    tracker = PortfolioPerformanceTracker(
        user=user,
        data_dir=get_data_dir(),
        benchmark_prices=YFinanceBenchmarkPrices(),
    )
    if not tracker.state().enabled:
        raise ValueError("請先啟用績效追蹤")

    result_cash = [item.model_copy(deep=True) for item in cash_positions]
    broker = declaration["broker"]
    account = declaration.get("account")
    currency = declaration["currency"]
    amount = float(declaration["amount"])
    target = next(
        (
            item
            for item in result_cash
            if item.broker.casefold() == broker.casefold()
            and (item.account or "").casefold() == (account or "").casefold()
            and item.currency == currency
        ),
        None,
    )
    if declaration["direction"] == "withdrawal":
        if target is None or target.amount < amount:
            raise ValueError("出金金額超過該券商帳戶的可用現金")

    amount_usd = amount if currency == "USD" else amount / rate
    tracker.declare_cash_flow(
        direction=declaration["direction"],
        amount=amount,
        currency=currency,
        amount_usd=amount_usd,
        fx_rate_to_usd=1 if currency == "USD" else rate,
        category=declaration["category"],
        channel=declaration["channel"],
        broker=broker,
        account=account,
        notes=declaration.get("notes"),
    )

    if declaration["direction"] == "deposit":
        merge_cash_position(
            result_cash,
            CashPosition(
                broker=broker,
                account=account,
                currency=currency,
                amount=amount,
                notes=declaration.get("notes"),
            ),
        )
    else:
        target.amount -= amount
        if target.amount == 0:
            result_cash.remove(target)

    save_manual_positions(
        positions,
        cash_positions=result_cash,
        user=user,
    )
    total_value = total_asset_value_usd(positions, result_cash, rate)
    if total_value is not None and total_value > 0:
        try:
            tracker.record_valuation(total_value_usd=total_value)
        except Exception:
            # QQQ/VT snapshot can catch up on the next refresh. The declared
            # flow and cash holding are already persisted; failing here used
            # to look like a failed deposit and caused a duplicate retry.
            pass
    return positions, result_cash



def _calc_weights(
    positions: list[Position],
    rate: float,
    cash_positions: Optional[list[CashPosition]] = None,
) -> dict:
    cash_positions = cash_positions or []
    total = total_asset_value_usd(positions, cash_positions, rate) or 0.0
    weights: dict = {}
    for p in positions:
        v = p.value if p.currency == "USD" else p.value / rate
        key = (p.broker, p.account or "", p.symbol)
        weights[key] = (v / total * 100) if total > 0 else 0.0
    for cash in cash_positions:
        value = cash.amount if cash.currency == "USD" else cash.amount / rate
        key = (cash.broker, cash.account or "", f"CASH {cash.currency}")
        weights[key] = (value / total * 100) if total > 0 else 0.0
    return weights


# ─────────────────────────────────────────────────────────────────────────────
# Shared chrome — dim borders, green/red only on P&L numbers
# ─────────────────────────────────────────────────────────────────────────────

_CHROME_BORDER = "#21262d"
_INSTRUMENT_LABEL = {
    "stock": "股票",
    "etf": "ETF",
    "option": "期權",
    "cash": "現金",
}
_SESSION_US = Position(
    broker="session", symbol="SPY", instrument_type="etf",
    quantity=1, currency="USD",
)
_SESSION_TW = Position(
    broker="session", symbol="2330.TW", instrument_type="stock",
    quantity=1, currency="TWD",
)


def _instrument_label(kind: str) -> str:
    return _INSTRUMENT_LABEL.get(kind, kind or "—")


def _signed_money(value: Optional[float], *, decimals: int = 2) -> str:
    if value is None:
        return "[dim]—[/dim]"
    color = "green" if value >= 0 else "red"
    sign = "+" if value >= 0 else "-"
    return f"[{color}]{sign}${abs(value):,.{decimals}f}[/{color}]"


def _signed_pct(value: Optional[float], *, decimals: int = 2) -> str:
    if value is None:
        return "[dim]—[/dim]"
    color = "green" if value >= 0 else "red"
    sign = "+" if value >= 0 else "-"
    return f"[{color}]{sign}{abs(value):.{decimals}f}%[/{color}]"


def _day_cell(
    pct: Optional[float],
    chg: Optional[float],
    currency: str = "USD",
) -> str:
    if pct is None and chg is None:
        return "[dim]—[/dim]"
    ccy = "" if currency == "USD" else f" {currency}"
    if chg is None:
        return _signed_pct(pct)
    color = "green" if chg >= 0 else "red"
    sign = "+" if chg >= 0 else "-"
    pct_s = f"{sign}{abs(pct):.2f}%" if pct is not None else "—"
    return f"[{color}]{pct_s} · {sign}{abs(chg):,.0f}{ccy}[/{color}]"


def _pnl_cell(pnl: Optional[float], pct: Optional[float]) -> str:
    if pnl is None and pct is None:
        return "[dim]—[/dim]"
    if pnl is None:
        return _signed_pct(pct)
    color = "green" if pnl >= 0 else "red"
    sign = "+" if pnl >= 0 else "-"
    pct_s = f" · {sign}{abs(pct):.1f}%" if pct is not None else ""
    return f"[{color}]{sign}${abs(pnl):,.2f}{pct_s}[/{color}]"


def _chrome_line(title: str, *parts: str) -> str:
    bits = [f"[bold]{title}[/bold]"]
    bits.extend(p for p in parts if p)
    return "  [dim]│[/dim]  ".join(bits)


def _chrome_header(title: str, *parts: str) -> Panel:
    return Panel(
        _chrome_line(title, *parts),
        border_style="dim",
        padding=(0, 1),
    )


def _session_pills() -> str:
    us = "[green]US 開市[/green]" if is_market_open(_SESSION_US) else "[dim]US 休市[/dim]"
    tw = "[green]TW 開市[/green]" if is_market_open(_SESSION_TW) else "[dim]TW 休市[/dim]"
    return f"{us}  {tw}"


def _portfolio_day_pnl(
    positions: list[Position],
    rate: float,
) -> tuple[Optional[float], Optional[float]]:
    day_usd = 0.0
    has = False
    for p in positions:
        if p.daily_change is None:
            continue
        has = True
        day_usd += p.daily_change if p.currency == "USD" else p.daily_change / rate
    if not has:
        return None, None
    total = total_asset_value_usd(positions, [], rate)
    if total is None or total == day_usd:
        return day_usd, None
    prev = total - day_usd
    pct = (day_usd / prev * 100.0) if prev else None
    return day_usd, pct


# ─────────────────────────────────────────────────────────────────────────────
# Rich renderable builders (return renderables, never print)
# ─────────────────────────────────────────────────────────────────────────────

def _holding_usd(position: Position, rate: float) -> float:
    return position.value if position.currency == "USD" else position.value / rate


def _asset_composition(
    positions: list[Position],
    cash_positions: list[CashPosition],
    rate: float,
) -> tuple[dict[str, float], dict[str, int], float, float]:
    """USD mix, per-type counts, and USD/TWD asset split."""
    mix = {
        "stock": 0.0,
        "etf": 0.0,
        "option": 0.0,
        "cash": cash_value_usd(cash_positions, rate) or 0.0,
    }
    counts = {"stock": 0, "etf": 0, "option": 0, "cash": len(cash_positions)}
    usd = 0.0
    twd = 0.0
    for position in positions:
        kind = (
            position.instrument_type
            if position.instrument_type in mix
            else "stock"
        )
        value = _holding_usd(position, rate)
        mix[kind] += value
        counts[kind] += 1
        if position.currency.upper() == "TWD":
            twd += value
        else:
            usd += value
    for cash in cash_positions:
        value = cash.amount if cash.currency == "USD" else cash.amount / rate
        if cash.currency.upper() == "TWD":
            twd += value
        else:
            usd += value
    return mix, counts, usd, twd


def _build_metrics_panel(
    positions: list[Position],
    rate: float,
    cash_positions: Optional[list[CashPosition]] = None,
    stale_quotes: bool = False,
    underlying_prices: Optional[dict[str, float]] = None,
    risk_free_rate: float = 0.04,
) -> Panel:
    """Asset composition: NAV, mix, P&L, beta. Exposure lives in the next row."""
    cash_positions = cash_positions or []
    cash_usd = cash_value_usd(cash_positions, rate) or 0.0
    total_usd = total_asset_value_usd(positions, cash_positions, rate)
    has_quotes = all(
        p.market_price is not None or p.market_value is not None
        for p in positions
    )
    performance = portfolio_unrealized_performance(
        positions,
        cash_positions,
        rate,
    )
    pnl_usd, pnl_pct = performance if performance is not None else (None, None)
    day_usd, day_pct = _portfolio_day_pnl(positions, rate)
    mix, counts, usd_leg, twd_leg = _asset_composition(
        positions, cash_positions, rate,
    )
    _ = underlying_prices, risk_free_rate

    b_num = 0.0
    b_den = cash_usd
    for p in positions:
        beta = cached_beta(p.symbol, p.instrument_type, p.underlying, p.currency)
        if beta is not None:
            v = _holding_usd(p, rate)
            b_num += beta * v
            b_den += v
    portfolio_beta = (b_num / b_den) if (b_den > 0 and has_quotes) else None

    if has_quotes and total_usd is not None:
        nav = f"${total_usd:,.2f}"
    else:
        nav = "[dim]載入報價[/dim]"

    if has_quotes and day_usd is not None:
        day_val = _day_cell(day_pct, day_usd)
    else:
        day_val = "[dim]—[/dim]"

    if has_quotes and pnl_usd is not None:
        pnl_val = _pnl_cell(pnl_usd, pnl_pct)
    elif not has_quotes:
        pnl_val = "[dim]—[/dim]"
    else:
        pnl_val = "[dim]無成本[/dim]"

    head = (
        f"[bold]總資產[/bold]  {nav}"
        f"    [dim]今日[/dim] {day_val}"
        f"    [dim]未實現[/dim] {pnl_val}"
    )

    denom = total_usd if (has_quotes and total_usd) else None
    mix_bits = []
    for key, label in (
        ("stock", "股票"),
        ("etf", "ETF"),
        ("option", "期權"),
        ("cash", "現金"),
    ):
        value = mix[key]
        if denom:
            mix_bits.append(f"{label} ${value:,.0f}  {value / denom * 100:.1f}%")
        elif has_quotes:
            mix_bits.append(f"{label} ${value:,.0f}")
        else:
            mix_bits.append(f"{label} —")
    mix_line = "  ·  ".join(mix_bits)

    count_bits = []
    for key, label in (
        ("stock", "股票"),
        ("etf", "ETF"),
        ("option", "期權"),
        ("cash", "現金"),
    ):
        if counts[key]:
            count_bits.append(f"{counts[key]} {label}")
    if not count_bits:
        count_bits.append("0 筆")

    extra: list[str] = ["  ·  ".join(count_bits)]
    if has_quotes and (usd_leg or twd_leg):
        ccy_total = usd_leg + twd_leg
        if ccy_total > 0:
            extra.append(
                f"USD {usd_leg / ccy_total * 100:.0f}%"
                f" / TWD {twd_leg / ccy_total * 100:.0f}%"
            )
    if has_quotes and total_usd is not None:
        extra.append(f"NT${total_usd * rate:,.0f}")
    extra.append(f"USDTWD {rate:.2f}")
    if stale_quotes:
        extra.append("上次價格")
    if has_quotes and portfolio_beta is not None:
        tilt = (
            "接近大盤" if 0.8 < portfolio_beta <= 1.2
            else ("低於大盤" if portfolio_beta <= 0.8 else "高於大盤")
        )
        extra.append(f"Beta {portfolio_beta:.2f} vs SPY  {tilt}")
    elif has_quotes:
        extra.append("Beta —")

    return Panel(
        f"{head}\n{mix_line}\n[dim]{'  ·  '.join(extra)}[/dim]",
        border_style="dim",
        padding=(0, 1),
    )


def _build_holdings_table(
    positions: list[Position], rate: float, weights: dict
) -> Table:
    """Broker-grouped holdings as a Rich Table (8 columns, indented rows)."""
    tbl = Table(
        box=_SEC_BOX,
        padding=(0, 2, 0, 1),
        show_header=True,
        header_style="bold dim",
        expand=True,
    )
    tbl.add_column("代碼",   style="bold white", min_width=8,  no_wrap=True)
    tbl.add_column("種類",   style="dim",         min_width=4,  no_wrap=True)
    tbl.add_column("數量",   justify="right",     min_width=6)
    tbl.add_column("成本",   justify="right",     min_width=8)
    tbl.add_column("現價",   justify="right",     min_width=8)
    tbl.add_column("市值",   justify="right",     style="bold", min_width=10)
    tbl.add_column("今日",   justify="right",     min_width=14)
    tbl.add_column("未實現", justify="right",     min_width=16)

    n_cols = 8
    has_quotes = any(p.market_price is not None or p.market_value is not None for p in positions)
    _ = weights

    for i, (bk, bk_pos) in enumerate(group_positions_by_broker(positions, rate)):
        bk_total = sum(
            p.value if p.currency == "USD" else p.value / rate for p in bk_pos
        )
        if i > 0:
            tbl.add_row(*[""] * n_cols, end_section=False)

        bk_total_s = f"[dim]${bk_total:,.0f}[/dim]" if has_quotes else "[dim]—[/dim]"
        tbl.add_row(
            f"[dim]{bk.upper()}[/dim]",
            *([""] * (n_cols - 2)),
            bk_total_s,
            end_section=True,
        )

        for p in bk_pos:
            qty_s = f"{p.quantity:,.2f}" if p.quantity % 1 != 0 else f"{int(p.quantity):,}"
            cost_s = f"${p.avg_cost:,.2f}" if p.avg_cost is not None else "[dim]—[/dim]"
            price_s = f"${p.market_price:,.2f}" if p.market_price is not None else "[dim]—[/dim]"
            val_s = (
                f"${p.value:,.2f}"
                if (p.market_price is not None or p.market_value is not None)
                else "[dim]—[/dim]"
            )
            tbl.add_row(
                f"  {p.symbol}",
                _instrument_label(p.instrument_type),
                qty_s,
                cost_s,
                price_s,
                val_s,
                _day_cell(p.daily_change_pct, p.daily_change, p.currency),
                _pnl_cell(p.unrealized_pnl, p.unrealized_pnl_pct),
                end_section=False,
            )

    return tbl


def _build_broker_panel(
    positions: list[Position],
    rate: float,
    cash_positions: Optional[list[CashPosition]] = None,
    loading: bool = False,
    underlying_prices: Optional[dict[str, float]] = None,
    risk_free_rate: float = 0.04,
) -> Panel:
    cash_positions = cash_positions or []
    has_quotes = all(
        p.market_price is not None or p.market_value is not None
        for p in positions
    )
    if not has_quotes:
        state = "計算中" if loading else "報價不足"
        return Panel(
            f"[bold]曝險[/bold]  [dim]{state}[/dim]\n"
            "[dim]股票／普通 ETF —  ·  倍數 ETF —  ·  期權 Δ —[/dim]\n"
            "[dim]券商  —[/dim]",
            border_style="dim",
            padding=(0, 1),
        )
    total = total_asset_value_usd(positions, cash_positions, rate) or 0.0
    broker_vals: dict[str, float] = {}
    for p in positions:
        bk = f"{p.broker} ({p.account})" if p.account else p.broker
        broker_vals[bk] = broker_vals.get(bk, 0.0) + _holding_usd(p, rate)
    for cash in cash_positions:
        bk = (
            f"{cash.broker} ({cash.account})"
            if cash.account
            else cash.broker
        )
        value = cash.amount if cash.currency == "USD" else cash.amount / rate
        broker_vals[bk] = broker_vals.get(bk, 0.0) + value
    chips = []
    for bk, bv in sorted(broker_vals.items(), key=lambda x: -x[1]):
        pct = (bv / total * 100) if total > 0 else 0.0
        chips.append(f"{bk} {pct:.0f}%  ${bv:,.0f}")
    alloc = "  ".join(chips) if chips else "—"
    exposure = calculate_portfolio_exposure(
        positions,
        cash_positions,
        rate,
        underlying_prices=underlying_prices,
        risk_free_rate=risk_free_rate,
    )
    if exposure.gross_ratio_pct is None:
        head = "[bold]曝險[/bold]  總曝險 —"
        if exposure.unpriced:
            missing = "、".join(exposure.unpriced[:3])
            head += f"  [dim]缺 {missing}[/dim]"
        buckets = "[dim]股票／普通 ETF —  ·  倍數 ETF —  ·  期權 Δ —[/dim]"
    else:
        gross_x = exposure.gross_ratio_pct / 100.0
        net_x = (exposure.net_ratio_pct or 0.0) / 100.0
        net_sign = "+" if exposure.net_exposure_usd >= 0 else "-"
        head = (
            f"[bold]曝險[/bold]  "
            f"總曝險 {gross_x:.2f}x  ${exposure.gross_exposure_usd:,.0f}"
            f"    淨 {net_sign}{abs(net_x):.2f}x  "
            f"{net_sign}${abs(exposure.net_exposure_usd):,.0f}"
        )
        buckets = (
            f"股票／普通 ETF ${exposure.standard_exposure_usd:,.0f}"
            f"  ·  倍數 ETF ${exposure.leveraged_etf_exposure_usd:,.0f}"
            f"  ·  期權 Δ ${exposure.option_exposure_usd:,.0f}"
        )
    return Panel(
        f"{head}\n{buckets}\n[dim]券商[/dim]  {alloc}",
        border_style="dim",
        padding=(0, 1),
    )


def _simplify_event_label(label: str) -> str:
    time_suffix = ""
    import re
    tm_match = re.search(r'\(((?:盤前|盤後)\s*\d{2}:\d{2}|\d{2}:\d{2})\)', label)
    if tm_match:
        time_suffix = f" ({tm_match.group(1)})"

    if "FED" in label:
        return f"▼ FED 利率決議{time_suffix}"
    if "NFP" in label:
        return f"★ NFP 非農/失業率{time_suffix}"
    if "CPI" in label:
        return f"◆ CPI 通膨指數{time_suffix}"
    
    m = re.search(r'(🔔|💻)\s*(?:\[bold white\])?([A-Z0-9.\-]+)(?:\[/bold white\])?\s*財報公佈', label)
    if m:
        sym = m.group(2)
        is_sox = "SOX" in label
        is_user = "持倉" in label
        if is_user and is_sox:
            return f"🔔 {sym} 財報 (SOX){time_suffix}"
        elif is_user:
            return f"🔔 {sym} 財報{time_suffix}"
        else:
            return f"💻 {sym} 財報{time_suffix}"
            
    return label


@dataclass(frozen=True)
class _CalEvent:
    date: date
    title: str
    badge: str = ""
    when: str = ""
    completed: bool = False
    summary: str = ""
    event_type: str = "OTHER"


def _earnings_badge(is_user: bool, is_sox: bool) -> str:
    if is_user and is_sox:
        return "持倉/SOX"
    if is_user:
        return "持倉"
    if is_sox:
        return "SOX"
    return ""


def _earnings_event_type(is_user: bool, is_sox: bool) -> str:
    if is_user and is_sox:
        return "PORTFOLIO_SOX"
    if is_user:
        return "PORTFOLIO"
    if is_sox:
        return "SOX"
    return "OTHER"


def _event_relative_label(event_date, today) -> str:
    days_away = (event_date - today).days
    if days_away == 0:
        return "今天"
    if days_away > 0:
        return f"{days_away}天後"
    return f"{-days_away}天前"


def _event_history_start(today):
    """Return the first day of the month preceding ``today``."""
    from datetime import timedelta

    last_day_of_previous_month = today.replace(day=1) - timedelta(days=1)
    return last_day_of_previous_month.replace(day=1)


def _retain_event_history(event_date, today) -> bool:
    """Bound retained metadata to last month onward and at most one year ahead."""
    from datetime import timedelta

    return _event_history_start(today) <= event_date <= today + timedelta(days=365)


_EVENT_TYPE_COLOR = {
    "PORTFOLIO": "green",
    "PORTFOLIO_SOX": "green",
    "SOX": "yellow",
    "MACRO": "cyan",
}


def _month_heading(year: int, month: int, count: int) -> str:
    return f"{year}年{month}月 · {count} 件事"


def _grid_day_markup(day: int, types: list[str], all_completed: bool) -> str:
    """Color the day by event type. Completed status stays in the list (✓)."""
    _ = all_completed
    cell = f"{day:2d}"
    if "PORTFOLIO_SOX" in types or ("PORTFOLIO" in types and "SOX" in types):
        return f"[green reverse]{cell}[/green reverse]"
    if "PORTFOLIO" in types:
        return f"[green reverse]{cell}[/green reverse]"
    if "MACRO" in types:
        return f"[cyan reverse]{cell}[/cyan reverse]"
    if "SOX" in types:
        return f"[yellow reverse]{cell}[/yellow reverse]"
    return f"[white reverse]{cell}[/white reverse]"


def _month_grid_panel(year: int, month: int, month_events: list[_CalEvent], today) -> Panel:
    day_to_events: dict[int, list[_CalEvent]] = {}
    for event in month_events:
        day_to_events.setdefault(event.date.day, []).append(event)

    cal = calendar.Calendar(firstweekday=0)
    weeks = cal.monthdayscalendar(year, month)
    is_this_month = today.year == year and today.month == month

    lines = ["[dim]一 二 三 四 五 六 日[/dim]", "┈" * 20]
    for week in weeks:
        cells = []
        for day in week:
            if day == 0:
                cells.append("  ")
                continue
            events = day_to_events.get(day, [])
            if events:
                cell = _grid_day_markup(
                    day,
                    [event.event_type for event in events],
                    all(event.completed for event in events),
                )
            else:
                cell = f"{day:2d}"
            if is_this_month and day == today.day:
                cell = f"[underline]{cell}[/underline]"
            cells.append(cell)
        lines.append(" ".join(cells))

    legend = (
        "[green reverse]持倉[/green reverse] "
        "[yellow reverse]SOX[/yellow reverse] "
        "[cyan reverse]總經[/cyan reverse] "
        "[underline]今[/underline]"
    )
    body = "\n".join(lines) + "\n" + legend
    return Panel(body, border_style="dim", title="月曆", expand=False, padding=(0, 1))


def _month_event_list(month_events: list[_CalEvent], today):
    from rich.console import Group
    from rich.text import Text

    if not month_events:
        return Text("無重要事件", style="dim")

    by_day: dict = {}
    for event in sorted(month_events, key=lambda item: (item.date, item.title)):
        by_day.setdefault(event.date, []).append(event)

    sections = []
    for event_date, events in by_day.items():
        relative = _event_relative_label(event_date, today)
        sections.append(
            Text.from_markup(
                f"[bold]{event_date.strftime('%m-%d')}[/bold]  [dim]{relative}[/dim]"
            )
        )
        for event in events:
            color = _EVENT_TYPE_COLOR.get(event.event_type, "white")
            mark = "✓" if event.completed else "○"
            parts = [f"{mark}  [{color}]{event.title}[/{color}]"]
            if event.badge:
                parts.append(event.badge)
            if event.when:
                parts.append(f"[dim]{event.when}[/dim]")
            if event.summary:
                parts.append(event.summary)
            sections.append(Text.from_markup("  ".join(parts)))
    return Group(*sections)


def _month_detail_panel(month_events: list[_CalEvent], today):
    from rich.console import Group
    from rich.text import Text

    return Group(
        Text.from_markup("[bold]行事曆[/bold]"),
        _month_event_list(month_events, today),
    )


def _render_monthly_calendar(
    year: int,
    month: int,
    month_events: list,
    today,
):
    """Expanded month body: calendar on the left, event list on the right."""
    events = list(month_events)
    split = Table(
        show_header=False,
        box=None,
        expand=True,
        padding=(0, 1),
        pad_edge=False,
    )
    split.add_column("cal", width=28, no_wrap=True, vertical="top")
    split.add_column("detail", ratio=1, overflow="fold", vertical="top")
    split.add_row(
        _month_grid_panel(year, month, events, today),
        _month_detail_panel(events, today),
    )
    return split


# ─────────────────────────────────────────────────────────────────────────────
# Logout confirmation modal
# ─────────────────────────────────────────────────────────────────────────────

class LogoutConfirmModal(ModalScreen[bool]):
    """安全登出確認對話框（Textual Modal，不 suspend）。"""

    DEFAULT_CSS = """
    LogoutConfirmModal {
        align: center middle;
    }

    #logout-dialog {
        width: 44;
        height: auto;
        border: thick $error;
        background: $panel;
        padding: 1 2;
    }

    #logout-buttons {
        height: auto;
        align: center middle;
        margin-top: 1;
    }

    #logout-buttons Button {
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="logout-dialog"):
            yield Static("[bold]確定要安全登出系統？[/bold]", id="logout-msg")
            with Horizontal(id="logout-buttons"):
                yield Button("確認登出", variant="error", id="confirm")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel").focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)
        elif event.key in ("left", "right"):
            confirm_btn = self.query_one("#confirm")
            cancel_btn = self.query_one("#cancel")
            if self.focused == confirm_btn:
                cancel_btn.focus()
            else:
                confirm_btn.focus()


def get_ascii_logo() -> str:
    for name in ("assesttrack_logo.txt", "assettrack_logo.txt"):
        logo_path = Path("AssetTrack_logo") / name
        if logo_path.exists():
            try:
                lines = logo_path.read_text(encoding="utf-8").splitlines()
                art_lines = [l for l in lines if l.strip()]
                if art_lines:
                    min_leading = min(len(l) - len(l.lstrip()) for l in art_lines)
                    cropped_lines = [l[min_leading:].rstrip() for l in lines]
                    start = 0
                    while start < len(cropped_lines) and not cropped_lines[start]:
                        start += 1
                    end = len(cropped_lines)
                    while end > start and not cropped_lines[end - 1]:
                        end -= 1
                    return "\n".join(cropped_lines[start:end])
            except Exception:
                pass

    return ""


class LoginScreen(Screen):
    """登入畫面：全螢幕 GitHub 暗色系，含 ASCII 鷹頭 Logo、User ID 輸入框及密碼/Touch ID 驗證。"""
    
    DEFAULT_CSS = """
    LoginScreen {
        align: center middle;
        background: #0d1117;
        overflow: auto;
    }
    
    #login-container {
        width: 60;
        height: auto;
        border: thick #21262d;
        background: #161b22;
        padding: 2 4;
        align: center middle;
    }
    
    #login-title {
        color: #58a6ff;
        text-align: center;
        text-style: bold;
        height: 1;
        margin-top: 1;
        margin-bottom: 0;
    }
    
    #login-subtitle {
        color: #8b949e;
        text-align: center;
        text-style: italic;
        height: 1;
        margin-bottom: 2;
    }
    
    #login-input-label {
        color: #8b949e;
        margin-bottom: 1;
    }
    
    #user-input {
        margin-bottom: 2;
        border: solid #30363d;
        background: #0d1117;
        color: #f0f6fc;
    }
    
    #login-btn-row {
        height: auto;
        align: center middle;
    }
    
    #login-error-msg {
        color: #ff7b72;
        text-align: center;
        margin-top: 1;
        height: 1;
    }
    """
    
    def __init__(self, default_user: str = "default") -> None:
        super().__init__()
        self.default_user = default_user
    
    def compose(self) -> ComposeResult:
        with Vertical(id="login-container"):
            yield Static("AssetTrack", id="login-title")
            yield Static("投資組合與期權觀察", id="login-subtitle")
            yield Label("帳號", id="login-input-label")
            yield Input(value=self.default_user, placeholder="default", id="user-input")
            yield Label("", id="login-error-msg")
            with Horizontal(id="login-btn-row"):
                yield Button("登入", variant="primary", id="login-btn")

    def on_mount(self) -> None:
        self.query_one("#user-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "login-btn":
            self._handle_login()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "user-input":
            self._handle_login()

    def _handle_login(self) -> None:
        user = self.query_one("#user-input", Input).value.strip()
        if not user:
            user = "default"
            
        if not account_exists(user):
            modal = RegisterModal(user)
            self.app.push_screen(modal, lambda success: self._on_register_complete(success, user))
        else:
            if touchid_enrolled(user):
                self.query_one("#login-error-msg", Label).update("[dim]正在嘗試 Touch ID 登入…[/dim]")
                self.run_touchid_auth(user)
            else:
                modal = PasswordModal(user)
                self.app.push_screen(modal, lambda login_success: self._on_password_complete(login_success, user))

    @work(thread=True)
    def run_touchid_auth(self, user: str) -> None:
        touchid_helper_path = Path(__file__).parent / "touchid_helper"
        success = False
        if touchid_helper_path.exists():
            try:
                res = subprocess.run(
                    [str(touchid_helper_path), user],
                    capture_output=True,
                )
                if res.returncode == 0:
                    unlock_vault_with_touchid(user)
                    success = True
            except Exception:
                success = False
        self.app.call_from_thread(self._on_touchid_complete, success, user)

    def _on_touchid_complete(self, success: bool, user: str) -> None:
        if success:
            self.query_one("#login-error-msg", Label).update("Touch ID 驗證成功！")
            self._login_success(user)
        else:
            self.query_one("#login-error-msg", Label).update("Touch ID 失敗，改用密碼登入。")
            modal = PasswordModal(user)
            self.app.push_screen(modal, lambda login_success: self._on_password_complete(login_success, user))

    def _on_password_complete(self, success: bool, user: str) -> None:
        if success:
            self._login_success(user)
        else:
            self.query_one("#login-error-msg", Label).update("密碼驗證失敗！")

    def _on_register_complete(self, success: bool, user: str) -> None:
        if success:
            self.query_one("#login-error-msg", Label).update("註冊成功，密碼已儲存！")
            self._login_success(user)
        else:
            self.query_one("#login-error-msg", Label).update("取消註冊。")

    def _login_success(self, user: str) -> None:
        try:
            seal_user_files(user)
            positions, cash_positions = load_manual_positions(user=user)
        except AuthError:
            lock_vault()
            self.query_one("#login-error-msg", Label).update(
                "無法解密持倉檔。請用本機原先登入過的帳號，勿覆蓋現有資料。"
            )
            return
        result = (user, positions, cash_positions)
        if load_sec_identity(user) is None:
            self.app.push_screen(
                SECIdentityModal(user),
                lambda _configured: self.dismiss(result),
            )
            return
        self.dismiss(result)


class PasswordModal(ModalScreen[bool]):
    """密碼輸入對話框 (Textual Modal)。"""
    
    DEFAULT_CSS = """
    PasswordModal {
        align: center middle;
    }
    #pwd-dialog {
        width: 44;
        height: auto;
        border: thick #21262d;
        background: #161b22;
        padding: 1 2;
    }
    #pwd-msg {
        margin-bottom: 1;
        text-style: bold;
    }
    #pwd-input {
        margin-bottom: 1;
        border: solid #30363d;
        background: #0d1117;
    }
    #pwd-error {
        color: #ff7b72;
        margin-bottom: 1;
        height: 1;
    }
    #pwd-buttons {
        height: auto;
        align: right middle;
    }
    #pwd-buttons Button {
        margin-left: 1;
    }
    """
    
    def __init__(self, user: str) -> None:
        super().__init__()
        self.user = user
        self._test_mode_reader = None
        self.attempts = 3

    def compose(self) -> ComposeResult:
        with Vertical(id="pwd-dialog"):
            yield Label(f"請輸入 [bold white]{self.user}[/bold white] 的登入密碼:", id="pwd-msg")
            yield Input(placeholder="密碼", password=True, id="pwd-input")
            yield Label("", id="pwd-error")
            with Horizontal(id="pwd-buttons"):
                yield Button("確認", variant="primary", id="confirm")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#pwd-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._submit()
        elif event.button.id == "cancel":
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "pwd-input":
            self._submit()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)

    def _submit(self) -> None:
        val = self.query_one("#pwd-input", Input).value
        error_lbl = self.query_one("#pwd-error", Label)
        if verify_password(self.user, val):
            unlock_vault(self.user, val)
            self.dismiss(True)
        else:
            self.attempts -= 1
            if self.attempts <= 0:
                self.dismiss(False)
            else:
                error_lbl.update(f"密碼錯誤！還剩 {self.attempts} 次機會。")
                self.query_one("#pwd-input", Input).value = ""


class RegisterModal(ModalScreen[bool]):
    """新使用者密碼註冊對話框 (Textual Modal)。"""
    
    DEFAULT_CSS = """
    RegisterModal {
        align: center middle;
    }
    #reg-dialog {
        width: 46;
        height: auto;
        border: thick #e3b341;
        background: #161b22;
        padding: 1 2;
    }
    #reg-title {
        text-style: bold;
        color: #e3b341;
        margin-bottom: 1;
    }
    #reg-desc {
        color: #8b949e;
        margin-bottom: 1;
    }
    .reg-field {
        margin-bottom: 1;
        border: solid #30363d;
        background: #0d1117;
    }
    #reg-error {
        color: #ff7b72;
        margin-bottom: 1;
        height: 1;
    }
    #performance-tracking-copy {
        color: #8b949e;
        height: auto;
        margin: 1 0;
    }
    #performance-tracking-toggle {
        height: auto;
        margin-bottom: 1;
    }
    #reg-buttons {
        height: auto;
        align: right middle;
    }
    #reg-buttons Button {
        margin-left: 1;
    }
    """
    
    def __init__(self, user: str) -> None:
        super().__init__()
        self.user = user

    def compose(self) -> ComposeResult:
        with Vertical(id="reg-dialog"):
            yield Label("[bold]註冊新使用者[/bold]", id="reg-title")
            yield Label("系統偵測到您是第一次使用此 ID，請設定登入密碼：", id="reg-desc")
            yield Input(placeholder="輸入密碼", password=True, id="pwd1", classes="reg-field")
            yield Input(placeholder="再次輸入確認密碼", password=True, id="pwd2", classes="reg-field")
            yield Label(
                "別只看帳面損益，確認每一塊資本是否真正跑贏市場。"
                "\n啟用後，出入金必須宣告，買賣則在持倉與現金間轉換。",
                id="performance-tracking-copy",
            )
            yield Checkbox(
                "啟用績效追蹤（預設比較 QQQ／VT）",
                value=False,
                id="performance-tracking-toggle",
            )
            yield Label("", id="reg-error")
            with Horizontal(id="reg-buttons"):
                yield Button("註冊", variant="primary", id="confirm")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#pwd1", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._submit()
        elif event.button.id == "cancel":
            self.dismiss(False)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)

    def _submit(self) -> None:
        pwd1 = self.query_one("#pwd1", Input).value
        pwd2 = self.query_one("#pwd2", Input).value
        error_lbl = self.query_one("#reg-error", Label)
        
        if not pwd1:
            error_lbl.update("密碼不能為空！")
            return
            
        if pwd1 != pwd2:
            error_lbl.update("兩次輸入密碼不一致！")
            return

        try:
            register_account(self.user, pwd1)
            unlock_vault(self.user, pwd1)
        except ValueError as exc:
            error_lbl.update(f"{exc}")
            return
        if self.query_one("#performance-tracking-toggle", Checkbox).value:
            PortfolioPerformanceTracker(
                user=self.user,
                data_dir=get_data_dir(),
            ).enable(new_account=True)
        self.dismiss(True)


class SECIdentityDeleteConfirmModal(ModalScreen[bool]):
    """Confirm removal of the current account's SEC identity."""

    DEFAULT_CSS = """
    SECIdentityDeleteConfirmModal {
        align: center middle;
    }
    #sec-identity-delete-dialog {
        width: 58;
        height: auto;
        border: thick #f85149;
        background: #161b22;
        padding: 1 2;
    }
    #sec-identity-delete-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    #sec-identity-delete-buttons Button {
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="sec-identity-delete-dialog"):
            yield Label(
                "刪除後，這個帳號將停止自動更新 SEC 13F。"
                "公開持股快取不會被刪除；名稱與信箱會從 Keychain 移除。"
            )
            with Horizontal(id="sec-identity-delete-buttons"):
                yield Button(
                    "確認刪除",
                    variant="error",
                    id="sec-identity-delete-confirm",
                )
                yield Button("取消", id="sec-identity-delete-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sec-identity-delete-confirm":
            self.dismiss(True)
        elif event.button.id == "sec-identity-delete-cancel":
            self.dismiss(False)


class SECIdentityModal(ModalScreen[bool]):
    """Create, update, or delete one account's SEC request identity."""

    DEFAULT_CSS = """
    SECIdentityModal {
        align: center middle;
    }
    #sec-identity-dialog {
        width: 72;
        height: auto;
        max-height: 90%;
        border: solid #21262d;
        background: #161b22;
        padding: 1 2;
    }
    #sec-identity-title {
        color: #58a6ff;
        text-style: bold;
        margin-bottom: 1;
    }
    #sec-identity-privacy {
        height: auto;
        color: #c9d1d9;
        margin-bottom: 1;
    }
    .sec-identity-field {
        margin-bottom: 1;
        border: solid #30363d;
        background: #0d1117;
    }
    #sec-identity-consent {
        height: auto;
        margin-bottom: 1;
    }
    #sec-identity-error {
        color: #ff7b72;
        height: auto;
        min-height: 1;
        margin-bottom: 1;
    }
    #sec-identity-buttons {
        height: auto;
        align: right middle;
    }
    #sec-identity-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, user: str) -> None:
        super().__init__()
        self.user = user
        self.existing = load_sec_identity(user)

    def compose(self) -> ComposeResult:
        existing = self.existing or {}
        with Vertical(id="sec-identity-dialog"):
            yield Label("SEC 13F 存取身分", id="sec-identity-title")
            yield Label(
                "SEC 規定自動下載申報資料時需提供識別名稱與聯絡信箱。"
                "資料只會作為 User-Agent 傳送給 SEC，並依目前 AssetTrack "
                "帳號存入作業系統 Keychain；不寫入投資快取、紀錄檔或 .env。"
                "你可以取消而繼續使用其他功能，也能稍後修改或刪除；刪除後 "
                "13F 自動更新會停止。",
                id="sec-identity-privacy",
            )
            yield Input(
                value=str(existing.get("display_name") or ""),
                placeholder="個人姓名或組織名稱",
                id="sec-identity-name",
                classes="sec-identity-field",
            )
            yield Input(
                value=str(existing.get("email") or ""),
                placeholder="SEC 可聯絡的電子信箱",
                id="sec-identity-email",
                classes="sec-identity-field",
            )
            yield Checkbox(
                "我同意上述資料為取得 13F 公開申報而傳送給 SEC",
                value=bool(existing),
                id="sec-identity-consent",
            )
            yield Label("", id="sec-identity-error")
            with Horizontal(id="sec-identity-buttons"):
                if existing:
                    yield Button(
                        "刪除 SEC 身分",
                        variant="error",
                        id="sec-identity-delete",
                    )
                yield Button("儲存", variant="primary", id="sec-identity-save")
                yield Button("取消", id="sec-identity-cancel")

    def on_mount(self) -> None:
        self.query_one("#sec-identity-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sec-identity-save":
            self._save()
        elif event.button.id == "sec-identity-delete":
            self.app.push_screen(
                SECIdentityDeleteConfirmModal(),
                self._delete_if_confirmed,
            )
        elif event.button.id == "sec-identity-cancel":
            self.dismiss(False)

    def _delete_if_confirmed(self, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            delete_sec_identity(self.user)
        except Exception:
            self.query_one("#sec-identity-error", Label).update(
                "無法存取系統 Keychain，SEC 身分尚未刪除"
            )
            return
        self.dismiss(True)

    def _save(self) -> None:
        name = self.query_one("#sec-identity-name", Input).value
        email = self.query_one("#sec-identity-email", Input).value
        consent = self.query_one(
            "#sec-identity-consent", Checkbox
        ).value
        try:
            save_sec_identity(
                self.user,
                display_name=name,
                email=email,
                consent=consent,
            )
        except Exception as exc:
            if isinstance(exc, ValueError):
                message = str(exc)
            else:
                message = "無法存取系統 Keychain，SEC 身分尚未儲存"
            self.query_one("#sec-identity-error", Label).update(
                f"{message}"
            )
            return
        self.dismiss(True)


class OnboardingModal(ModalScreen[str]):
    """新使用者無持倉引導對話框 (Textual Modal)。"""
    
    DEFAULT_CSS = """
    OnboardingModal {
        align: center middle;
    }
    #onboard-dialog {
        width: 50;
        height: auto;
        border: solid #21262d;
        background: #161b22;
        padding: 1 2;
    }
    #onboard-title {
        text-style: bold;
        color: #58a6ff;
        margin-bottom: 1;
    }
    #onboard-desc {
        margin-bottom: 1;
    }
    #onboard-list {
        height: auto;
        border: solid #30363d;
        margin-bottom: 1;
    }
    """
    
    def compose(self) -> ComposeResult:
        with Vertical(id="onboard-dialog"):
            yield Label("[bold]尚無持倉[/bold]", id="onboard-title")
            yield Label("請選擇開始方式：", id="onboard-desc")
            yield OptionList(
                Option("建立範例部位 (AAPL, TSLA)", id="sample"),
                Option("手動新增持倉", id="manual"),
                Option("保持空白進入看板", id="empty"),
                id="onboard-list"
            )

    def on_mount(self) -> None:
        self.query_one("#onboard-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss("empty")


Holding = Position | CashPosition


class AddPositionModal(ModalScreen[Optional[list[Holding]]]):
    """手動新增/修改持股對話框。

    新增模式支援「批次累積」：每筆填完按「儲存並繼續」加入待存清單，
    最後一次「完成儲存」整批回傳 list[Position]；修改模式回傳單元素 list。
    Symbol 輸入時自動推斷市場/幣別（如 2330 或 2330.TW → TW/TWD），
    非必要欄位（帳戶/交易所/幣別/備註/板塊）收於可展開的「進階欄位」區。
    """

    # Ordered list of all focusable field IDs (Inputs + Selects + adv toggle)
    _FIELD_IDS: list[str] = [
        "add-broker", "add-symbol", "add-type", "add-leverage-factor",
        "add-cash-account", "add-cash-currency", "add-cash-amount", "add-cash-notes",
        "add-underlying", "add-strike", "add-expiry", "add-option-type", "add-multiplier",
        "add-side", "add-qty", "add-cost", "add-market", "adv-toggle",
        "add-account", "add-exch", "add-curr", "add-notes", "add-sector",
    ]

    # Fields hidden until the「進階欄位」toggle is expanded
    _ADV_FIELD_IDS: frozenset[str] = frozenset(
        {"add-account", "add-exch", "add-curr", "add-notes", "add-sector"}
    )

    DEFAULT_CSS = """
    AddPositionModal {
        align: center middle;
    }
    #add-dialog {
        width: 60;
        height: auto;
        border: solid #21262d;
        background: #161b22;
        padding: 1 2;
    }
    #add-title {
        text-style: bold;
        color: #58a6ff;
        margin-bottom: 1;
    }
    #add-hint {
        color: #8b949e;
        margin-bottom: 1;
    }
    #batch-list {
        color: #7ee787;
        margin-bottom: 1;
        height: auto;
    }
    #option-fields-container {
        height: auto;
        layout: vertical;
    }
    #adv-fields-container {
        height: auto;
        layout: vertical;
    }
    #adv-toggle {
        width: 36;
        min-width: 20;
        border: none;
        background: #0d1117;
        color: #8b949e;
        height: 1;
    }
    #adv-toggle:focus {
        color: #58a6ff;
        text-style: bold;
    }
    .form-row {
        height: auto;
        margin-bottom: 1;
        align: left middle;
    }
    .form-label {
        width: 18;
        color: #8b949e;
    }
    .required-star {
        color: #ff7b72;
    }
    .optional-tag {
        color: #484f58;
    }
    .form-input {
        width: 36;
        border: solid #30363d;
        background: #0d1117;
    }
    .form-input:focus {
        border: solid #58a6ff;
        background: #0d1117;
    }
    Select {
        width: 36;
    }
    #add-error {
        color: #ff7b72;
        margin-bottom: 1;
        height: auto;
    }
    #add-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    #add-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, position: Optional[Holding] = None) -> None:
        super().__init__()
        self.position = position
        self._pending: list[Holding] = []   # 批次新增的待存清單（僅新增模式）
        self._adv_visible: bool = False      # 進階欄位是否展開
        # 目前由 Symbol 推斷出的 (market, currency)；僅在使用者未手動改過時才覆寫
        self._inferred: tuple[str, str] = ("US", "USD")

    def compose(self) -> ComposeResult:
        brokers = [("manual", "manual"), ("FT", "FT"), ("IBKR", "IBKR")]
        types   = [
            ("stock", "stock"),
            ("etf", "etf"),
            ("option", "option"),
            ("cash", "cash"),
        ]
        markets = [("US", "US"), ("TW", "TW"), ("HK", "HK"), ("other", "other")]
        opt_types = [("Call 買權", "call"), ("Put 賣權", "put")]

        # Determine pre-populated values
        p = self.position
        
        b_val = "manual"
        if p and p.broker:
            b_lower = p.broker.lower()
            if "firstrade" in b_lower or b_lower == "ft":
                b_val = "FT"
            elif "ibkr" in b_lower:
                b_val = "IBKR"
            elif "manual" in b_lower:
                b_val = "manual"

        acct_val = (p.account or "") if p else ""
        sym_val = p.symbol if isinstance(p, Position) else ""
        
        t_val = "stock"
        if isinstance(p, CashPosition):
            t_val = "cash"
        elif p and p.instrument_type:
            t_lower = p.instrument_type.lower()
            if t_lower in ("stock", "etf", "option"):
                t_val = t_lower

        # Option-specific values
        udl_val = p.underlying if (isinstance(p, Position) and p.underlying) else ""
        strike_val = f"{p.strike}" if (isinstance(p, Position) and p.strike is not None) else ""
        exp_val = p.expiry if (isinstance(p, Position) and p.expiry) else ""
        opt_type_val = p.option_type if (isinstance(p, Position) and p.option_type) else "call"
        mult_val = f"{p.multiplier}" if (isinstance(p, Position) and p.multiplier is not None) else "100"
        leverage_val = (
            f"{p.leverage_factor:g}"
            if isinstance(p, Position) and p.leverage_factor is not None
            else ""
        )
        
        qty_val = ""
        side_val = "long"
        if isinstance(p, Position) and p.quantity is not None:
            side_val = "short" if p.quantity < 0 else "long"
            abs_qty = abs(p.quantity)
            qty_val = f"{abs_qty:,.2f}" if abs_qty % 1 != 0 else f"{int(abs_qty)}"

        cost_val = ""
        if isinstance(p, Position) and p.avg_cost is not None:
            cost_val = f"{p.avg_cost}"

        m_val = "US"
        if isinstance(p, Position) and p.market:
            m_upper = p.market.upper()
            if m_upper in ("US", "TW", "HK", "OTHER"):
                m_val = m_upper
            else:
                m_val = "other"

        exch_val = p.exchange if isinstance(p, Position) else ""
        curr_val = p.currency if p else "USD"
        notes_val = p.notes if p else ""
        sect_val = p.sector if isinstance(p, Position) else ""
        cash_amount_val = f"{p.amount:g}" if isinstance(p, CashPosition) else ""

        title = "[bold]修改持倉[/bold]" if p else "[bold]新增持倉（可連續多筆）[/bold]"
        btn_label = "確認修改" if p else "完成儲存"

        with Vertical(id="add-dialog"):
            yield Label(title, id="add-title")
            yield Label(
                "[dim]↑↓ 切換欄位　Enter 移至下一欄　[red]★[/red] 必填　✦ 建議填寫[/dim]",
                id="add-hint"
            )
            yield Label("", id="batch-list")

            with Horizontal(classes="form-row"):
                yield Label("券商 [dim](Broker)[/dim]:", classes="form-label")
                yield Select(brokers, value=b_val, id="add-broker")

            with Horizontal(classes="form-row", id="symbol-field-row"):
                yield Label("[red]★[/red] 代碼 [dim](Symbol)[/dim]:", classes="form-label")
                yield Input(value=sym_val, placeholder="例如 AAPL 或 2330.TW", id="add-symbol",
                            classes="form-input")

            with Horizontal(classes="form-row"):
                yield Label("商品類型 [dim](Type)[/dim]:", classes="form-label")
                yield Select(types, value=t_val, id="add-type")

            with Horizontal(classes="form-row", id="leverage-factor-row"):
                yield Label("ETF 曝險倍數 [dim](x)[/dim]:", classes="form-label")
                yield Input(
                    value=leverage_val,
                    placeholder="留白自動辨識；正2=2、反1=-1",
                    id="add-leverage-factor",
                    classes="form-input",
                )

            with Vertical(id="cash-fields-container"):
                with Horizontal(classes="form-row"):
                    yield Label("帳戶 [dim](Account)[/dim]:", classes="form-label")
                    yield Input(
                        value=acct_val or "",
                        placeholder="例如 default 或子帳戶",
                        id="add-cash-account",
                        classes="form-input",
                    )
                with Horizontal(classes="form-row"):
                    yield Label("[red]★[/red] 幣別 [dim](Currency)[/dim]:", classes="form-label")
                    yield Select(
                        [("USD 美金", "USD"), ("TWD 新台幣", "TWD")],
                        value=curr_val if curr_val in ("USD", "TWD") else "USD",
                        id="add-cash-currency",
                    )
                with Horizontal(classes="form-row"):
                    yield Label("[red]★[/red] 金額 [dim](Amount)[/dim]:", classes="form-label")
                    yield Input(
                        value=cash_amount_val,
                        placeholder="正數，例如 10000",
                        id="add-cash-amount",
                        classes="form-input",
                    )
                with Horizontal(classes="form-row"):
                    yield Label("備註 [dim](Notes)[/dim]:", classes="form-label")
                    yield Input(
                        value=notes_val or "",
                        placeholder="自訂備註（選填）",
                        id="add-cash-notes",
                        classes="form-input",
                    )

            # Option specific container
            with Vertical(id="option-fields-container"):
                with Horizontal(classes="form-row"):
                    yield Label("[red]★[/red] 標的代碼 [dim](Underlying)[/dim]:", classes="form-label")
                    yield Input(value=udl_val, placeholder="標的股票代碼，例如 AAPL", id="add-underlying",
                                classes="form-input")

                with Horizontal(classes="form-row"):
                    yield Label("[red]★[/red] 履約價 [dim](Strike)[/dim]:", classes="form-label")
                    yield Input(value=strike_val, placeholder="正數，例如 150.0", id="add-strike",
                                classes="form-input")

                with Horizontal(classes="form-row"):
                    yield Label("[red]★[/red] 到期日 [dim](Expiry)[/dim]:", classes="form-label")
                    yield Input(value=exp_val, placeholder="YYYY-MM-DD，例如 2026-06-19", id="add-expiry",
                                classes="form-input")

                with Horizontal(classes="form-row"):
                    yield Label("選擇權類型 [dim](Type)[/dim]:", classes="form-label")
                    yield Select(opt_types, value=opt_type_val, id="add-option-type")

                with Horizontal(classes="form-row"):
                    yield Label("合約乘數 [dim](Multiplier)[/dim]:", classes="form-label")
                    yield Input(value=mult_val, placeholder="預設為 100", id="add-multiplier",
                                classes="form-input")

            with Horizontal(classes="form-row", id="side-field-row"):
                yield Label("持倉方向 [dim](Side)[/dim]:", classes="form-label")
                yield Select(
                    [("Long 多/做多", "long"), ("Short 空/放空", "short")],
                    value=side_val, id="add-side"
                )

            with Horizontal(classes="form-row", id="quantity-field-row"):
                yield Label("[red]★[/red] 數量 [dim](Qty)[/dim]:", classes="form-label")
                yield Input(value=qty_val, placeholder="正數，例如 100", id="add-qty",
                            classes="form-input")

            with Horizontal(classes="form-row", id="cost-field-row"):
                yield Label("[yellow]✦[/yellow] 成本 [dim](Cost)[/dim]:", classes="form-label")
                yield Input(value=cost_val, placeholder="正數，例如 150.5（建議填寫）", id="add-cost",
                            classes="form-input")

            with Horizontal(classes="form-row", id="market-field-row"):
                yield Label("市場 [dim](Market)[/dim]:", classes="form-label")
                yield Select(markets, value=m_val, id="add-market")

            with Horizontal(classes="form-row", id="adv-toggle-row"):
                yield Label("", classes="form-label")
                yield Button("▸ 進階欄位（帳戶/交易所/幣別/備註/板塊）", id="adv-toggle")

            # Advanced (optional) fields — collapsed by default
            with Vertical(id="adv-fields-container"):
                with Horizontal(classes="form-row"):
                    yield Label("帳戶 [dim](Account)[/dim]:", classes="form-label")
                    yield Input(value=acct_val, placeholder="例如 default 或子帳戶", id="add-account",
                                classes="form-input")

                with Horizontal(classes="form-row"):
                    yield Label("交易所 [dim](Exch)[/dim]:", classes="form-label")
                    yield Input(value=exch_val, placeholder="例如 NYSE, TSE [dim](選填)[/dim]", id="add-exch",
                                classes="form-input")

                with Horizontal(classes="form-row"):
                    yield Label("貨幣 [dim](Currency)[/dim]:", classes="form-label")
                    yield Input(value=curr_val, placeholder="例如 USD 或 TWD", id="add-curr",
                                classes="form-input")

                with Horizontal(classes="form-row"):
                    yield Label("備註 [dim](Notes)[/dim]:", classes="form-label")
                    yield Input(value=notes_val, placeholder="自訂備註 [dim](選填)[/dim]", id="add-notes",
                                classes="form-input")

                with Horizontal(classes="form-row"):
                    yield Label("板塊 [dim](Sector)[/dim]:", classes="form-label")
                    yield Input(value=sect_val, placeholder="例如 Technology [dim](選填)[/dim]", id="add-sector",
                                classes="form-input")

            yield Label("", id="add-error")
            with Horizontal(id="add-buttons"):
                if not p:
                    yield Button("儲存並繼續", variant="success", id="confirm-next")
                yield Button(btn_label, variant="primary", id="confirm")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        t_val = self.query_one("#add-type", Select).value
        self._set_type_visibility(str(t_val))
        self.query_one("#adv-fields-container").display = False
        self.query_one("#batch-list", Label).display = False
        if isinstance(self.position, Position):
            # 修改模式：以既有值為推斷基準，避免游標經過 Symbol 時覆寫使用者資料
            m_val = self.query_one("#add-market", Select).value
            c_val = self.query_one("#add-curr", Input).value.strip().upper()
            self._inferred = (str(m_val), c_val)
        if t_val == "cash":
            self.query_one("#add-cash-account", Input).focus()
        elif t_val == "option":
            self.query_one("#add-underlying", Input).focus()
        else:
            self.query_one("#add-symbol", Input).focus()

    def _set_type_visibility(self, instrument_type: str) -> None:
        is_cash = instrument_type == "cash"
        is_opt = instrument_type == "option"
        is_etf = instrument_type == "etf"
        self.query_one("#cash-fields-container").display = is_cash
        self.query_one("#option-fields-container").display = is_opt
        self.query_one("#leverage-factor-row").display = is_etf
        self.query_one("#symbol-field-row").display = not is_cash and not is_opt
        for selector in (
            "#side-field-row",
            "#quantity-field-row",
            "#cost-field-row",
            "#market-field-row",
            "#adv-toggle-row",
        ):
            self.query_one(selector).display = not is_cash
        self.query_one("#adv-fields-container").display = (
            not is_cash and self._adv_visible
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Symbol 輸入時自動推斷市場/幣別（純數字或 .TW/.TWO 結尾 → TW/TWD）。

        僅在市場/幣別欄位仍停留在「上一次推斷值」時才覆寫，
        使用者手動改過的值不會被清掉。
        """
        if event.input.id != "add-symbol":
            return
        import re
        sym = event.value.strip().upper()
        is_tw = (
            sym.endswith(".TW") or sym.endswith(".TWO")
            or bool(re.match(r"^\d{4,6}[A-Z]?$", sym))
        )
        new_inf = ("TW", "TWD") if is_tw else ("US", "USD")
        if new_inf == self._inferred:
            return
        mkt_sel = self.query_one("#add-market", Select)
        curr_in = self.query_one("#add-curr", Input)
        if str(mkt_sel.value) == self._inferred[0]:
            mkt_sel.value = new_inf[0]
        if curr_in.value.strip().upper() == self._inferred[1]:
            curr_in.value = new_inf[1]
        self._inferred = new_inf

    def _toggle_advanced(self) -> None:
        self._adv_visible = not self._adv_visible
        self.query_one("#adv-fields-container").display = self._adv_visible
        arrow = "▾" if self._adv_visible else "▸"
        self.query_one("#adv-toggle", Button).label = (
            f"{arrow} 進階欄位（帳戶/交易所/幣別/備註/板塊）"
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "add-type":
            self._set_type_visibility(str(event.value))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._finish()
        elif event.button.id == "confirm-next":
            self._save_and_continue()
        elif event.button.id == "adv-toggle":
            self._toggle_advanced()
        elif event.button.id == "cancel":
            # 已按過「儲存並繼續」的部位視為已確認，取消僅丟棄目前表單內容
            self.dismiss(self._pending or None)

    def on_key(self, event) -> None:
        key = event.key
        if key == "escape":
            self.dismiss(self._pending or None)
            return

        if key in ("down", "tab", "enter") or key in ("up", "shift+tab"):
            focused = self.focused
            if focused is None:
                return

            inst_type = str(self.query_one("#add-type", Select).value)
            is_cash = inst_type == "cash"
            is_opt = inst_type == "option"
            is_etf = inst_type == "etf"

            visible_fids = []
            for fid in self._FIELD_IDS:
                if fid.startswith("add-cash-"):
                    if is_cash:
                        visible_fids.append(fid)
                elif is_cash:
                    if fid in ("add-broker", "add-type"):
                        visible_fids.append(fid)
                elif fid in ("add-underlying", "add-strike", "add-expiry", "add-option-type", "add-multiplier"):
                    if is_opt:
                        visible_fids.append(fid)
                elif fid == "add-leverage-factor":
                    if is_etf:
                        visible_fids.append(fid)
                elif fid == "add-symbol":
                    # Hidden for option type (bug#00047) — symbol is auto-derived.
                    if not is_opt:
                        visible_fids.append(fid)
                elif fid in self._ADV_FIELD_IDS:
                    # 進階欄位僅在展開時納入導航
                    if self._adv_visible:
                        visible_fids.append(fid)
                else:
                    visible_fids.append(fid)

            current_idx = None
            for i, fid in enumerate(visible_fids):
                try:
                    widget = self.query_one(f"#{fid}")
                    if widget is focused:
                        current_idx = i
                        break
                except Exception:
                    pass

            if current_idx is None:
                return

            if key == "enter":
                from textual.widgets import Select as TxSelect
                if isinstance(focused, (TxSelect, Button)):
                    # Select 展開選單、Button（進階欄位切換）觸發按下，皆交還原生行為
                    return
                event.prevent_default()

            step = -1 if key in ("up", "shift+tab") else 1
            next_idx = (current_idx + step) % len(visible_fids)
            next_fid = visible_fids[next_idx]
            try:
                self.query_one(f"#{next_fid}").focus()
            except Exception:
                pass

    def _collect(self) -> Optional[Holding]:
        """Validate the visible form and build a security or cash holding."""
        broker   = self.query_one("#add-broker", Select).value
        account  = self.query_one("#add-account", Input).value.strip()
        symbol   = self.query_one("#add-symbol", Input).value.strip().upper()
        inst_type = self.query_one("#add-type", Select).value
        side     = self.query_one("#add-side", Select).value
        qty_str  = self.query_one("#add-qty", Input).value.strip()
        cost_str = self.query_one("#add-cost", Input).value.strip()
        market   = self.query_one("#add-market", Select).value
        exch     = self.query_one("#add-exch", Input).value.strip()
        curr     = self.query_one("#add-curr", Input).value.strip().upper()
        notes    = self.query_one("#add-notes", Input).value.strip()
        sector   = self.query_one("#add-sector", Input).value.strip()
        leverage_text = self.query_one("#add-leverage-factor", Input).value.strip()

        error_lbl = self.query_one("#add-error", Label)

        if inst_type == "cash":
            cash_account = self.query_one("#add-cash-account", Input).value.strip()
            cash_currency = self.query_one("#add-cash-currency", Select).value
            cash_amount_text = self.query_one("#add-cash-amount", Input).value.strip()
            cash_notes = self.query_one("#add-cash-notes", Input).value.strip()
            if not cash_amount_text:
                error_lbl.update("[red]★ 現金金額[/red] 為必填")
                self.query_one("#add-cash-amount", Input).focus()
                return None
            try:
                cash_amount = float(cash_amount_text.replace(",", ""))
                if cash_amount <= 0:
                    raise ValueError
            except ValueError:
                error_lbl.update("現金金額必須是大於 0 的數字")
                self.query_one("#add-cash-amount", Input).focus()
                return None
            try:
                return CashPosition(
                    broker=str(broker),
                    account=cash_account or "default",
                    currency=str(cash_currency),
                    amount=cash_amount,
                    notes=cash_notes or None,
                    last_updated=datetime.utcnow(),
                )
            except Exception as exc:
                error_lbl.update(f"資料驗證失敗: {exc}")
                return None

        # For option type, symbol may be left blank (auto-generated from underlying/expiry/strike)
        if not symbol and inst_type != "option":
            error_lbl.update("[red]★ 商品代碼[/red] 為必填，請輸入代碼（例如 AAPL）")
            self.query_one("#add-symbol", Input).focus()
            return

        # Option validation
        underlying = None
        strike = None
        expiry = None
        opt_type = None
        multiplier = None
        leverage_factor = None

        if inst_type == "etf" and leverage_text:
            try:
                leverage_factor = float(leverage_text)
                if leverage_factor == 0 or abs(leverage_factor) > 10:
                    raise ValueError
            except ValueError:
                error_lbl.update("ETF 曝險倍數須介於 -10 至 10，且不可為 0")
                self.query_one("#add-leverage-factor", Input).focus()
                return None

        if inst_type == "option":
            underlying = self.query_one("#add-underlying", Input).value.strip().upper()
            strike_str = self.query_one("#add-strike", Input).value.strip()
            expiry = self.query_one("#add-expiry", Input).value.strip()
            opt_type = self.query_one("#add-option-type", Select).value
            mult_str = self.query_one("#add-multiplier", Input).value.strip()

            if not underlying:
                error_lbl.update("[red]★ 標的代碼[/red] 為必填，請輸入（例如 AAPL）")
                self.query_one("#add-underlying", Input).focus()
                return
            if not strike_str:
                error_lbl.update("[red]★ 履約價[/red] 為必填，請輸入履約價格")
                self.query_one("#add-strike", Input).focus()
                return
            try:
                strike = float(strike_str)
                if strike <= 0:
                    error_lbl.update("履約價必須大於 0")
                    self.query_one("#add-strike", Input).focus()
                    return
            except ValueError:
                error_lbl.update("請輸入有效的履約價（數字）")
                self.query_one("#add-strike", Input).focus()
                return
            if not expiry:
                error_lbl.update("[red]★ 到期日[/red] 為必填，請輸入到期日（YYYY-MM-DD）")
                self.query_one("#add-expiry", Input).focus()
                return
            
            import re
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", expiry):
                error_lbl.update("到期日格式必須為 YYYY-MM-DD，例如 2026-06-19")
                self.query_one("#add-expiry", Input).focus()
                return

            if mult_str:
                try:
                    multiplier = float(mult_str)
                    if multiplier <= 0:
                        error_lbl.update("合約乘數必須大於 0")
                        self.query_one("#add-multiplier", Input).focus()
                        return
                except ValueError:
                    error_lbl.update("請輸入有效的合約乘數（數字）")
                    self.query_one("#add-multiplier", Input).focus()
                    return
            else:
                multiplier = 100.0

            # bug#00047: the top Symbol field is hidden for option type (it's fully
            # derived from underlying/strike/expiry/type), so always (re)generate it
            # here rather than trusting a possibly-stale value left over from editing.
            try:
                from datetime import datetime as _dt
                exp_dt = _dt.strptime(expiry, "%Y-%m-%d")
                yy = exp_dt.strftime("%y")
                mm = exp_dt.strftime("%m")
                dd = exp_dt.strftime("%d")
                cp = "C" if opt_type == "call" else "P"
                if market == "TW":
                    # Taiwan option symbol format: {UNDERLYING}{YYMMD}{C|P}
                    symbol = f"{underlying}{yy}{mm}{dd}{cp}"
                else:
                    # US OCC format: {UNDERLYING}{YYMMDD}{C|P}{STRIKE*1000 zero-padded to 8 digits}
                    strike_int = int(round(strike * 1000))
                    symbol = f"{underlying}{yy}{mm}{dd}{cp}{strike_int:08d}"
            except Exception as _e:
                error_lbl.update(f"無法自動生成選擇權代碼: {_e}")
                self.query_one("#add-expiry", Input).focus()
                return

        if not qty_str:
            error_lbl.update("[red]★ 持股數量[/red] 為必填，請輸入數量")
            self.query_one("#add-qty", Input).focus()
            return

        try:
            qty = float(qty_str)
            if qty <= 0:
                error_lbl.update("數量必須大於 0")
                self.query_one("#add-qty", Input).focus()
                return
        except ValueError:
            error_lbl.update("請輸入有效的數量（數字）")
            self.query_one("#add-qty", Input).focus()
            return

        # Apply side: short → negative quantity
        if side == "short":
            qty = -qty

        avg_cost = 0.0
        if cost_str:
            try:
                avg_cost = float(cost_str)
                if avg_cost < 0:
                    error_lbl.update("平均成本不能為負數")
                    self.query_one("#add-cost", Input).focus()
                    return
            except ValueError:
                error_lbl.update("請輸入有效的成本（數字）")
                self.query_one("#add-cost", Input).focus()
                return

        # Only append .TW suffix for non-option types (option symbols have their own format)
        if market == "TW" and inst_type != "option" and not symbol.endswith(".TW") and not symbol.endswith(".TWO"):
            symbol = symbol + ".TW"

        try:
            if self.position:
                pos = self.position.model_copy(deep=True)
                pos.broker = broker
                pos.account = account or "default"
                pos.symbol = symbol
                pos.instrument_type = inst_type
                pos.quantity = qty
                pos.avg_cost = avg_cost
                pos.market = market
                pos.exchange = exch or None
                pos.currency = curr or "USD"
                pos.notes = notes or None
                pos.sector = sector or None
                pos.last_updated = datetime.utcnow()
                if inst_type == "option":
                    pos.underlying = underlying
                    pos.strike = strike
                    pos.expiry = expiry
                    pos.option_type = opt_type
                    pos.multiplier = multiplier
                else:
                    pos.underlying = None
                    pos.strike = None
                    pos.expiry = None
                    pos.option_type = None
                    pos.multiplier = None
                pos.leverage_factor = leverage_factor if inst_type == "etf" else None
            else:
                pos = Position(
                    broker=broker,
                    account=account or "default",
                    symbol=symbol,
                    instrument_type=inst_type,
                    quantity=qty,
                    avg_cost=avg_cost,
                    market=market,
                    exchange=exch or None,
                    currency=curr or "USD",
                    notes=notes or None,
                    sector=sector or None,
                    leverage_factor=leverage_factor if inst_type == "etf" else None,
                    source="interactive",
                    last_updated=datetime.utcnow()
                )
                if inst_type == "option":
                    pos.underlying = underlying
                    pos.strike = strike
                    pos.expiry = expiry
                    pos.option_type = opt_type
                    pos.multiplier = multiplier
            # bug#00046: invalidate cached quote fields so the next refresh recomputes
            # a fresh market_value (with correct multiplier) instead of reusing stale
            # values computed under the old quantity/avg_cost/multiplier/symbol.
            pos.market_price = None
            pos.market_value = None
            pos.prev_close = None
            Position.model_validate(pos)
            return pos
        except Exception as e:
            error_lbl.update(f"資料驗證失敗: {e}")
            return None

    def _form_is_empty(self) -> bool:
        """主要輸入欄位（代碼/標的與數量）皆為空 → 視為沒有待送出的表單。"""
        inst_type = self.query_one("#add-type", Select).value
        if inst_type == "cash":
            return not self.query_one("#add-cash-amount", Input).value.strip()
        is_opt = inst_type == "option"
        lead_id = "add-underlying" if is_opt else "add-symbol"
        lead = self.query_one(f"#{lead_id}", Input).value.strip()
        qty = self.query_one("#add-qty", Input).value.strip()
        return not lead and not qty

    def _refresh_batch_label(self) -> None:
        lbl = self.query_one("#batch-list", Label)
        if not self._pending:
            lbl.display = False
            return
        shown = self._pending[-5:]
        items = "、".join(
            (
                f"CASH {p.currency} {p.amount:g}"
                if isinstance(p, CashPosition)
                else f"{p.symbol}×{abs(p.quantity):g}"
            )
            for p in shown
        )
        prefix = "…" if len(self._pending) > 5 else ""
        lbl.update(f"📋 待存清單 ({len(self._pending)})：{prefix}{items}")
        lbl.display = True

    def _reset_entry_fields(self) -> None:
        """加入待存清單後清空「本筆專屬」欄位，保留券商/類型/市場等共通設定。"""
        if self.query_one("#add-type", Select).value == "cash":
            self.query_one("#add-cash-amount", Input).value = ""
            self.query_one("#add-cash-notes", Input).value = ""
            self.query_one("#add-cash-amount", Input).focus()
            return
        for fid in ("add-symbol", "add-qty", "add-cost", "add-notes", "add-strike"):
            self.query_one(f"#{fid}", Input).value = ""
        is_opt = (self.query_one("#add-type", Select).value == "option")
        if is_opt:
            self.query_one("#add-underlying", Input).focus()
        else:
            self.query_one("#add-symbol", Input).focus()

    def _save_and_continue(self) -> None:
        pos = self._collect()
        if pos is None:
            return
        self._pending.append(pos)
        self._refresh_batch_label()
        self._reset_entry_fields()
        self.query_one("#add-error", Label).update(
            f"[green]已加入待存清單，可繼續輸入下一筆（或按「完成儲存」寫入全部）[/green]"
        )

    def _finish(self) -> None:
        if self.position:
            # 修改模式：單筆
            pos = self._collect()
            if pos is not None:
                self.dismiss([pos])
            return
        if self._form_is_empty():
            if self._pending:
                self.dismiss(self._pending)
            else:
                self.query_one("#add-error", Label).update(
                    "尚未輸入任何部位（請先填寫代碼與數量）"
                )
            return
        pos = self._collect()
        if pos is not None:
            self.dismiss(self._pending + [pos])


class FieldEditModal(ModalScreen[Optional[str]]):
    """Modal screen for editing a single field of a position inline."""
    DEFAULT_CSS = """
    FieldEditModal {
        align: center middle;
    }

    #edit-dialog {
        width: 50;
        height: auto;
        border: thick $primary;
        background: $panel;
        padding: 1 2;
    }

    #edit-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #edit-input {
        margin-bottom: 1;
    }

    #edit-select {
        margin-bottom: 1;
    }

    #edit-error {
        color: $error;
        margin-bottom: 1;
    }

    #edit-buttons {
        height: auto;
        align: right middle;
    }

    #edit-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, title: str, field_name: str, current_value: str, choices: Optional[list[str]] = None) -> None:
        super().__init__()
        self.title_text: str = title
        self.field_name: str = field_name
        self.current_value: str = current_value
        self.choices: Optional[list[str]] = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Label(self.title_text, id="edit-title")
            if self.choices:
                options = [(c, c) for c in self.choices]
                yield Select(options, value=self.current_value if self.current_value in self.choices else Select.BLANK, id="edit-select")
            else:
                yield Input(value=self.current_value, id="edit-input")
            yield Label("", id="edit-error")
            with Horizontal(id="edit-buttons"):
                yield Button("確認", variant="primary", id="save")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        if not self.choices:
            self.query_one("#edit-input", Input).focus()
        else:
            self.query_one("#edit-select", Select).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._submit()
        elif event.button.id == "cancel":
            self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
        elif event.key == "enter" and not self.choices:
            self._submit()

    def _submit(self) -> None:
        if self.choices:
            val = self.query_one("#edit-select", Select).value
            if val == Select.BLANK:
                val = ""
            self.dismiss(str(val))
        else:
            val = self.query_one("#edit-input", Input).value.strip()
            error_lbl = self.query_one("#edit-error", Label)
            if self.field_name == "quantity":
                try:
                    qty = float(val)
                    if qty <= 0:
                        error_lbl.update("數量必須大於 0")
                        return
                except ValueError:
                    error_lbl.update("請輸入有效的數字")
                    return
            elif self.field_name == "avg_cost":
                if val:
                    try:
                        cost = float(val)
                        if cost < 0:
                            error_lbl.update("成本不能小於 0")
                            return
                    except ValueError:
                        error_lbl.update("請輸入有效的數字")
                        return
            self.dismiss(val)


class PositionActionsModal(ModalScreen[Optional[str]]):
    """Position Actions overlay modal in TUI."""
    DEFAULT_CSS = """
    PositionActionsModal {
        align: center middle;
    }
    #actions-dialog {
        width: 44;
        height: auto;
        border: thick $primary;
        background: $panel;
        padding: 1 2;
    }
    #actions-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #actions-list {
        height: auto;
        margin-bottom: 1;
        border: solid $accent;
    }
    """

    def __init__(self, pos: Position) -> None:
        super().__init__()
        self.pos = pos

    def compose(self) -> ComposeResult:
        with Vertical(id="actions-dialog"):
            yield Label(f"[bold]部位操作[/bold]  [dim]{self.pos.broker} · {self.pos.symbol}[/dim]", id="actions-title")
            yield OptionList(
                Option("修改備註", id="notes"),
                Option("修改分類", id="sector"),
                Option("修改計價幣別", id="currency"),
                Option("修改成本幣別", id="cost_currency"),
                Option("修改券商與帳戶", id="broker_account"),
                Option("移除此持倉", id="delete"),
                Option("取消", id="cancel"),
                id="actions-list"
            )

    def on_mount(self) -> None:
        self.query_one("#actions-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        action = event.option.id
        if action == "cancel":
            self.dismiss(None)
        else:
            self.dismiss(action)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)



class DeleteConfirmModal(ModalScreen[bool]):
    """Confirmation dialog for deleting one or more positions."""
    DEFAULT_CSS = """
    DeleteConfirmModal {
        align: center middle;
    }
    #delete-confirm-dialog {
        width: 50;
        height: auto;
        border: thick red;
        background: $panel;
        padding: 1 2;
    }
    #delete-confirm-title {
        text-style: bold;
        color: red;
        margin-bottom: 1;
    }
    #delete-confirm-msg {
        margin-bottom: 1;
        height: auto;
    }
    #delete-confirm-buttons {
        height: auto;
        align: right middle;
    }
    #delete-confirm-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(
        self,
        position: Holding | list[Holding],
        *,
        sell_to_cash: bool = False,
    ) -> None:
        super().__init__()
        plist = position if isinstance(position, list) else [position]
        self.positions: list[Holding] = plist
        self.position = plist[0]  # 向後相容：單筆呼叫端仍可讀取 .position
        self.sell_to_cash = sell_to_cash

    def compose(self) -> ComposeResult:
        descs = []
        for holding in self.positions:
            label = (
                f"CASH {holding.currency}"
                if isinstance(holding, CashPosition)
                else f"{holding.symbol} ({holding.instrument_type})"
            )
            descs.append(
                f"{holding.broker.upper()} - "
                f"{holding.account or 'default'} - {label}"
            )
        shown = descs[:6]
        if len(descs) > 6:
            shown.append(f"…及其他 {len(descs) - 6} 筆")
        desc = "\n".join(f"[cyan]{d}[/]" for d in shown)
        n_note = f"以下 [bold]{len(descs)}[/bold] 筆部位" if len(descs) > 1 else "以下部位"
        with Vertical(id="delete-confirm-dialog"):
            title = (
                "賣出並轉為現金"
                if self.sell_to_cash
                else "刪除確認"
            )
            yield Label(title, id="delete-confirm-title")
            yield Label(
                (
                    f"您確定要完整賣出{n_note}，並將目前市值轉入同帳戶現金嗎？\n\n{desc}"
                    if self.sell_to_cash
                    else f"您確定要[bold red]完整刪除[/bold red]{n_note}嗎？"
                    f"此操作無法復原：\n\n{desc}"
                ),
                id="delete-confirm-msg"
            )
            with Horizontal(id="delete-confirm-buttons"):
                yield Button(
                    "確認賣出" if self.sell_to_cash else "確認刪除",
                    variant="error",
                    id="confirm",
                )
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel").focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)
        elif event.key in ("left", "right"):
            confirm_btn = self.query_one("#confirm")
            cancel_btn = self.query_one("#cancel")
            if self.focused == confirm_btn:
                cancel_btn.focus()
            else:
                confirm_btn.focus()

# ─────────────────────────────────────────────────────────────────────────────
# 投資建議「查看公式細節」drill-in 共用行為（bug#00118）
# ─────────────────────────────────────────────────────────────────────────────

class _FormulaDrillMixin:
    """讓畫面上的投資建議可點選『查看公式細節』連結推入公式細節頁（bug#00118）。
    render 建議時把 render_detail_recs() 回傳的 {token: rec} 存到 self._recs_by_id，
    detail_headline 內嵌的 [@click=screen.show_formula('token')] 即觸發此 action。
    RecommendationDetailScreen 於方法內（呼叫時）解析，故可定義於本類之後。"""

    def _remember_recs(self, mapping: dict) -> None:
        base = getattr(self, "_recs_by_id", None)
        if base is None:
            base = {}
            self._recs_by_id = base
        base.update(mapping)

    def action_show_formula(self, token: str) -> None:
        rec = getattr(self, "_recs_by_id", {}).get(token)
        if rec is not None:
            self.app.push_screen(RecommendationDetailScreen(rec))


# ─────────────────────────────────────────────────────────────────────────────
# Upcoming Events Screen
# ─────────────────────────────────────────────────────────────────────────────

def _format_financial_value(value: Optional[float], currency: str = "USD") -> str:
    """Format a reported financial statement value without inventing precision."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    amount = abs(value)
    currency_prefix = "$" if currency == "USD" else f"{currency} "
    for divisor, suffix in ((1_000_000_000_000, "T"), (1_000_000_000, "B"), (1_000_000, "M")):
        if amount >= divisor:
            return f"{sign}{currency_prefix}{amount / divisor:,.2f}{suffix}"
    return f"{sign}{currency_prefix}{amount:,.0f}"


def _format_earnings_actuals(actuals: Optional[dict]) -> str:
    """Format the four required earnings metrics with current and YoY values."""
    metrics = (actuals or {}).get("metrics") or {}
    currency = (actuals or {}).get("currency") or "USD"
    names = (
        ("Revenue", "revenue"),
        ("CAPEX", "capex"),
        ("EBIT", "ebit"),
        ("FCF", "fcf"),
    )
    parts = []
    for display_name, key in names:
        metric = metrics.get(key)
        if not metric:
            parts.append(f"{display_name} —")
            continue
        value = _format_financial_value(metric.get("value"), currency)
        prior = _format_financial_value(metric.get("prior_year_value"), currency)
        yoy = metric.get("yoy_pct")
        yoy_text = f"{yoy:+.1f}%" if yoy is not None else "—"
        parts.append(f"{display_name} {value} (YoY {yoy_text}；去年同期 {prior})")
    period = (actuals or {}).get("period")
    prefix = f"{period}｜" if period else ""
    return prefix + "｜".join(parts)


def _format_earnings_reaction(reaction: Optional[dict]) -> str:
    """Compact past-earnings summary: EPS beat/miss plus +3-session move."""
    if not reaction:
        return ""
    parts = []
    verdict_text = {
        "beat": "EPS 擊敗",
        "miss": "EPS 不如",
        "meet": "EPS 符合",
    }.get(reaction.get("verdict"))
    surprise = reaction.get("surprise_pct")
    if verdict_text:
        if surprise is not None:
            parts.append(f"{verdict_text} {surprise:+.1f}%")
        else:
            parts.append(verdict_text)
    pct = reaction.get("price_change_pct")
    if pct is not None:
        end = reaction.get("price_end_date")
        if end is not None and hasattr(end, "strftime"):
            parts.append(f"{pct:+.1f}% →{end.strftime('%m-%d')}")
        else:
            parts.append(f"{pct:+.1f}%")
    return "；".join(parts)


def _fred_unavailable_text(subject: str, series_ids: tuple[str, ...]) -> str:
    """Explain why FRED-backed data is unavailable without exposing secrets."""
    from .quotes import fred_failure_reason

    reason = fred_failure_reason(*series_ids)
    detail = {
        "missing_key": "未載入 FRED_API_KEY",
        "auth_error": "FRED API key 驗證失敗",
        "network_error": "無法連線至 FRED",
        "http_error": "FRED API 暫時回應錯誤",
        "invalid_response": "FRED 回應格式異常",
        "no_data": "FRED 尚未提供可用資料",
    }.get(reason, "FRED 尚未提供可用資料")
    return f"{subject}：{detail}"


def _as_of_month_label(as_of) -> str:
    month = getattr(as_of, "month", None)
    return f"{month}月 " if month else ""


def _format_cpi_event_actuals(result: Optional[dict]) -> str:
    if not result:
        return _fred_unavailable_text("CPI", ("CPIAUCSL", "CPIAUCNS"))
    yoy_prev = result.get("prev_yoy_pct")
    mom_prev = result.get("prev_mom_pct")
    month = _as_of_month_label(result.get("as_of"))
    yoy_cmp = (
        f"{result['yoy_pct'] - yoy_prev:+.2f}pp" if yoy_prev is not None else "—"
    )
    mom_cmp = (
        f"{result['mom_pct'] - mom_prev:+.2f}pp" if mom_prev is not None else "—"
    )
    return (
        f"總指數 CPI {month}YoY {result['yoy_pct']:.2f}%（{yoy_cmp}） "
        f"MoM {result['mom_pct']:.2f}%（{mom_cmp}）"
    )


def _format_nfp_event_actuals(nfp: Optional[dict], unemployment: Optional[dict]) -> str:
    parts = []
    if nfp:
        current_k = nfp["change"] / 1000.0
        previous = nfp.get("prev_change")
        month = _as_of_month_label(nfp.get("as_of"))
        if previous is None:
            parts.append(f"NFP {month}{current_k:+,.0f}K")
        else:
            previous_k = previous / 1000.0
            parts.append(
                f"NFP {month}{current_k:+,.0f}K（{current_k - previous_k:+,.0f}K）"
            )
    else:
        parts.append(_fred_unavailable_text("NFP", ("PAYEMS",)))
    if unemployment:
        parts.append(
            f"失業 {unemployment['rate_pct']:.1f}%"
            f"（{unemployment['change_pp']:+.1f}pp）"
        )
    else:
        parts.append(_fred_unavailable_text("失業率", ("UNRATE",)))
    return " ".join(parts)


def _format_fed_event_actuals(result: Optional[dict]) -> str:
    if not result:
        return _fred_unavailable_text("利率決議", ("DFEDTARU", "DFEDTARL"))
    after = result["range_after"]
    return (
        f"目標區間 {after[0]:.2f}–{after[1]:.2f}%"
        f"（{result['delta_bps']:+d}bp）"
    )


class TimezoneInputModal(ModalScreen[Optional[str]]):
    """Let a user enter any IANA timezone name and validate it before saving."""

    DEFAULT_CSS = """
    TimezoneInputModal { align: center middle; }
    #timezone-dialog {
        width: 68;
        height: auto;
        border: thick $primary;
        background: $panel;
        padding: 1 2;
    }
    #timezone-title { text-style: bold; margin-bottom: 1; }
    #timezone-help { height: auto; margin-bottom: 1; color: $text-muted; }
    #timezone-error { height: auto; color: $error; }
    #timezone-buttons { height: auto; align: right middle; margin-top: 1; }
    #timezone-buttons Button { margin-left: 1; }
    """

    def __init__(self, current_timezone: str) -> None:
        super().__init__()
        self.current_timezone = current_timezone

    def compose(self) -> ComposeResult:
        with Vertical(id="timezone-dialog"):
            yield Label("調整事件顯示時區", id="timezone-title")
            yield Label(
                "輸入 IANA 時區，例如 Asia/Taipei、America/New_York、Europe/London。",
                id="timezone-help",
            )
            yield Input(value=self.current_timezone, id="timezone-input")
            yield Label("", id="timezone-error")
            with Horizontal(id="timezone-buttons"):
                yield Button("套用", variant="primary", id="timezone-save")
                yield Button("取消", id="timezone-cancel")

    def on_mount(self) -> None:
        self.query_one("#timezone-input", Input).focus()

    def _submit(self) -> None:
        import zoneinfo

        value = self.query_one("#timezone-input", Input).value.strip()
        try:
            timezone = zoneinfo.ZoneInfo(value)
        except (KeyError, ValueError):
            self.query_one("#timezone-error", Label).update("找不到此時區，請輸入有效的 IANA 時區名稱。")
            return
        self.dismiss(timezone.key)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "timezone-save":
            self._submit()
        else:
            self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
        elif event.key == "enter":
            self._submit()


class UpcomingEventsScreen(_FormulaDrillMixin, Screen):
    """重要日曆事件 Screen（持倉／SOX 財報與總經重大事件；不重複列出持有部位）。"""

    BINDINGS = [
        Binding("t", "adjust_timezone", "時區"),
        Binding("escape", "go_back", "返回"),
        Binding("q", "go_back", "返回", show=False),
    ]

    DEFAULT_CSS = """
    UpcomingEventsScreen {
        background: #0d1117;
        layout: vertical;
    }
    #events-header {
        height: auto;
        padding: 0 1;
        margin: 1 2 0 2;
    }
    #events-calendar-container {
        height: 1fr;
        padding: 0 2;
        layout: vertical;
    }
    #events-right-panel {
        height: 1fr;
        padding: 0;
        border: solid #21262d;
    }
    #events-right-panel:focus {
        border: tall $accent;
    }
    #events-months {
        height: auto;
    }
    #events-months Collapsible {
        height: auto;
        margin-bottom: 1;
        padding-left: 0;
    }
    #events-months Contents {
        padding: 0 1 0 1;
    }
    .month-split {
        layout: horizontal;
        height: auto;
        width: 100%;
    }
    .month-cal {
        width: 30;
        height: auto;
        padding: 0 1 0 0;
        border-right: tall #334155;
    }
    .month-detail {
        width: 1fr;
        height: auto;
        padding: 0 0 0 1;
    }
    #events-macro {
        height: auto;
        margin: 1 0 1 0;
        padding: 0 1;
        border: solid #21262d;
    }
    #events-macro.hidden { display: none; }
    """

    def __init__(self, user: str, positions: list[Position]) -> None:
        super().__init__()
        from .storage import load_user_preferences

        self.user = user
        self.positions = positions
        preferences = load_user_preferences(user)
        self.event_timezone = preferences.get("event_timezone") or "Asia/Taipei"
        self._header_status: str = ""
        self._macro_recs: list = []  # bug#00119: 結構化總經指標建議（可點選公式細節）
        self._calendar_month_views: list = []

    def compose(self) -> ComposeResult:
        yield Static("", id="events-header")
        with Vertical(id="events-calendar-container"):
            with ScrollableContainer(id="events-right-panel"):
                yield Vertical(id="events-months")
                yield Static("", id="events-macro", classes="hidden")
        yield Footer()

    def _update_header(self, status: str) -> None:
        self._header_status = status
        self._render_header()

    def _render_header(self) -> None:
        from rich.panel import Panel
        from .shared import format_timezone_label

        title_line = _chrome_line(
            "事件",
            self._header_status,
            f"[dim]{format_timezone_label(self.event_timezone)} · T 時區[/dim]",
        )
        self.query_one("#events-header", Static).update(
            Panel(title_line, border_style="dim", padding=(0, 1))
        )

    def _update_events_static(self) -> None:
        self._render_macro_recs()
        self._rebuild_month_collapsibles()

    @work(exclusive=True, group="calendar-months")
    async def _rebuild_month_collapsibles(self) -> None:
        months = self.query_one("#events-months", Vertical)
        await months.remove_children()
        views = list(self._calendar_month_views)
        if not views:
            await months.mount(
                Static("[dim]上個月起至未來 90 天內無重大事件與財報日期[/dim]")
            )
            return
        await months.mount(
            *[
                Collapsible(
                    Horizontal(
                        Static(grid, classes="month-cal"),
                        Static(detail, classes="month-detail"),
                        classes="month-split",
                    ),
                    title=heading,
                    collapsed=not expanded,
                )
                for heading, grid, detail, expanded in views
            ]
        )

    def _render_macro_recs(self) -> None:
        """把重點經濟指標的結論投影成單行＋公式連結；依據只在公式頁。"""
        w = self.query_one("#events-macro", Static)
        if not self._macro_recs:
            w.add_class("hidden")
            self._recs_by_id = {}
            return
        w.remove_class("hidden")
        w.border_title = "重點經濟指標（點公式細節看計算）"
        body, mapping = render_detail_recs(self._macro_recs, compact=True)
        self._recs_by_id = mapping
        caption = (
            "[dim]顏色：綠＝通膨／勞動降溫、偏寬鬆；不是「經濟變好」。"
            "CPI 事件列為總指數；FED 事件列為目標區間。[/dim]"
        )
        w.update(caption + "\n" + body)

    @work(thread=True)
    def run_macro_readings_fetch(self) -> None:
        """背景抓取各總經指標最新一期已公佈數值（FRED），完成後更新解析面板。
        缺 API key／資料時該指標不出現（不臆測）。"""
        from .quotes import fetch_latest_macro_readings
        from .shared import macro_recommendations

        recs = macro_recommendations(fetch_latest_macro_readings())
        self.app.call_from_thread(self._on_macro_readings, recs)

    def _on_macro_readings(self, recs: list) -> None:
        self._macro_recs = recs
        self._render_macro_recs()

    def on_mount(self) -> None:
        panel = self.query_one("#events-right-panel")
        panel.can_focus = True
        panel.focus()

        self._update_header("[dim]抓取行事曆…[/dim]")
        self.run_macro_readings_fetch()
        self.run_calendar_fetch()

    def action_go_back(self) -> None:
        self.dismiss()

    def action_adjust_timezone(self) -> None:
        self.app.push_screen(TimezoneInputModal(self.event_timezone), self._on_timezone_selected)

    def _on_timezone_selected(self, timezone_name: Optional[str]) -> None:
        if not timezone_name or timezone_name == self.event_timezone:
            return
        from .storage import load_user_preferences, save_user_preferences

        preferences = load_user_preferences(self.user)
        preferences["event_timezone"] = timezone_name
        save_user_preferences(preferences, self.user)
        self.event_timezone = timezone_name
        self._calendar_month_views = []
        self._update_events_static()
        self._update_header("[dim]重排時區…[/dim]")
        self.run_calendar_fetch()

    @work(thread=True, exclusive=True)
    def run_calendar_fetch(self) -> None:
        from datetime import datetime as dt_cls, time as time_cls, timedelta
        from .quotes import (
            _normalize_symbol_for_yf,
            compute_cpi_conclusion,
            compute_fed_decision_conclusion,
            compute_nfp_conclusion,
            compute_unemployment_conclusion,
            fetch_earnings_reactions_batch,
        )
        from .shared import (
            event_timezone,
            get_upcoming_macro_events,
        )
        from .storage import load_event_history, save_event_history

        # 1. Gather unique symbols
        portfolio_tickers = set()
        for p in self.positions:
            sym = p.underlying if p.instrument_type == "option" else p.symbol
            norm_sym = _normalize_symbol_for_yf(sym, "stock", p.currency)
            portfolio_tickers.add(norm_sym)

        unique_tickers = list(portfolio_tickers.union(SOX_TICKERS))

        ticker_to_data = fetch_earnings_calendar(unique_tickers, self.event_timezone)

        timezone = event_timezone(self.event_timezone)
        now = dt_cls.now(timezone)
        today = now.date()
        start_date = _event_history_start(today)
        cutoff = today + timedelta(days=90)

        events: list[_CalEvent] = []
        earnings_events: list[dict] = []
        current_history = []
        current_history_ids = set()

        def remember_event(
            sym: str,
            event_date,
            event_dt,
            period_str: Optional[str],
            is_user: bool,
            is_sox: bool,
        ) -> None:
            event_id = (
                f"{sym}|{int(event_dt.timestamp())}"
                if event_dt is not None
                else f"{sym}|date|{event_date.isoformat()}"
            )
            current_history_ids.add(event_id)
            current_history.append({
                "id": event_id,
                "symbol": sym,
                "timestamp": event_dt.isoformat() if event_dt is not None else None,
                "date": event_date.isoformat(),
                "period": period_str,
                "is_user": is_user,
                "is_sox": is_sox,
            })

        def pack_earnings(
            event_date,
            sym: str,
            occurred: bool,
            event_dt,
            period_str: Optional[str],
            is_user: bool,
            is_sox: bool,
            time_text: Optional[str],
        ) -> None:
            earnings_events.append({
                "date": event_date,
                "sym": sym,
                "occurred": occurred,
                "event_dt": event_dt,
                "period": period_str,
                "is_user": is_user,
                "is_sox": is_sox,
                "time_text": time_text,
            })

        # Add earnings dates
        for sym, (dates_list, info_date, time_str, period_str) in ticker_to_data.items():
            is_user = any(
                _normalize_symbol_for_yf(
                    p.underlying if p.instrument_type == "option" else p.symbol,
                    "stock",
                    p.currency,
                ) == sym
                for p in self.positions
            )
            is_sox = sym in SOX_TICKERS

            if info_date and start_date <= info_date <= cutoff:
                event_time = time_cls.fromisoformat(time_str) if time_str else None
                event_dt = dt_cls.combine(info_date, event_time, tzinfo=timezone) if event_time else None
                occurred = event_dt <= now if event_dt else info_date < today
                pack_earnings(
                    info_date, sym, occurred, event_dt, period_str, is_user, is_sox, time_str,
                )
                remember_event(sym, info_date, event_dt, period_str, is_user, is_sox)
            else:
                for d in dates_list:
                    if isinstance(d, dt_cls):
                        d = d.date()
                    if start_date <= d <= cutoff:
                        occurred = d < today
                        pack_earnings(
                            d, sym, occurred, None, period_str, is_user, is_sox, None,
                        )
                        remember_event(sym, d, None, period_str, is_user, is_sox)

        # Merge previously observed earnings. yfinance switches its calendar to
        # the next quarter after a release, so local retention is what keeps the
        # completed event visible.
        history_by_id = {
            item.get("id"): item
            for item in load_event_history(self.user)
            if isinstance(item, dict) and item.get("id")
        }
        for item in current_history:
            history_by_id[item["id"]] = item

        retained_history = []
        for event_id, item in history_by_id.items():
            try:
                timestamp = item.get("timestamp")
                if timestamp:
                    original_dt = dt_cls.fromisoformat(timestamp)
                    if original_dt.tzinfo is None:
                        original_dt = original_dt.replace(tzinfo=timezone)
                    local_dt = original_dt.astimezone(timezone)
                    event_date = local_dt.date()
                    occurred = local_dt <= now
                    time_text = local_dt.strftime("%H:%M")
                else:
                    event_date = dt_cls.fromisoformat(item["date"]).date()
                    local_dt = None
                    occurred = event_date < today
                    time_text = None
            except (TypeError, ValueError, KeyError):
                continue

            # Completed events are retained only from the first day of the
            # previous month. Future metadata stays bounded to one year.
            if _retain_event_history(event_date, today):
                retained_history.append(item)
            if event_id in current_history_ids or not (start_date <= event_date <= cutoff):
                continue

            sym = item.get("symbol", "")
            if not sym:
                continue
            pack_earnings(
                event_date,
                sym,
                occurred,
                local_dt,
                item.get("period"),
                bool(item.get("is_user")),
                bool(item.get("is_sox")),
                time_text,
            )

        save_event_history(retained_history, self.user)

        occurred_items = [
            (item["sym"], item["date"], item["event_dt"], item["period"])
            for item in earnings_events
            if item["occurred"]
        ]
        earnings_reactions = fetch_earnings_reactions_batch(occurred_items)
        for item in earnings_events:
            summary = ""
            if item["occurred"]:
                summary = _format_earnings_reaction(
                    earnings_reactions.get((item["sym"], item["date"]))
                )
            when = " ".join(
                part for part in (item["period"], item["time_text"]) if part
            )
            events.append(_CalEvent(
                date=item["date"],
                title=item["sym"],
                badge=_earnings_badge(item["is_user"], item["is_sox"]),
                when=when,
                completed=item["occurred"],
                summary=summary,
                event_type=_earnings_event_type(item["is_user"], item["is_sox"]),
            ))

        # Add macro events
        macro_list = get_upcoming_macro_events(
            days=90,
            start_days_ago=(today - start_date).days,
            timezone_name=self.event_timezone,
        )
        past_macro_types = {label for _, label, _ in macro_list}
        cpi_actuals = compute_cpi_conclusion() if "◆CPI" in past_macro_types else None
        nfp_actuals = compute_nfp_conclusion() if "★NFP" in past_macro_types else None
        unemployment_actuals = compute_unemployment_conclusion() if "★NFP" in past_macro_types else None
        macro_titles = {"▼FED": "FED", "★NFP": "NFP", "◆CPI": "CPI"}
        for ev_date, ev_label, time_str in macro_list:
            if start_date <= ev_date <= cutoff:
                event_dt = dt_cls.combine(ev_date, time_cls.fromisoformat(time_str), tzinfo=timezone)
                occurred = event_dt <= now
                summary = ""
                if occurred:
                    if ev_label == "◆CPI":
                        summary = _format_cpi_event_actuals(cpi_actuals)
                    elif ev_label == "★NFP":
                        summary = _format_nfp_event_actuals(nfp_actuals, unemployment_actuals)
                    else:
                        meeting_date_et = event_dt.astimezone(event_timezone("America/New_York")).date()
                        summary = _format_fed_event_actuals(
                            compute_fed_decision_conclusion(meeting_date_et)
                        )
                events.append(_CalEvent(
                    date=ev_date,
                    title=macro_titles.get(ev_label, ev_label),
                    when=time_str,
                    completed=occurred,
                    summary=summary,
                    event_type="MACRO",
                ))

        # Update UI back on the event loop
        self.app.call_from_thread(self._on_fetch_complete, events, today)

    def _on_fetch_complete(self, events: list[_CalEvent], today) -> None:
        self._update_header("[green]行事曆資料更新成功[/green]")

        if not events:
            self._calendar_month_views = []
            self._update_events_static()
            return

        events.sort(key=lambda item: item.date)

        by_month: dict = {}
        for event in events:
            by_month.setdefault((event.date.year, event.date.month), []).append(event)

        month_views = []
        for (y, m), ev_list in sorted(by_month.items()):
            is_current = (y, m) == (today.year, today.month)
            month_views.append(
                (
                    _month_heading(y, m, len(ev_list)),
                    _month_grid_panel(y, m, ev_list, today),
                    _month_detail_panel(ev_list, today),
                    is_current,
                )
            )

        self._calendar_month_views = month_views
        self._update_events_static()


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard Screen
# ─────────────────────────────────────────────────────────────────────────────

def _pos_key(p: Position) -> tuple[str, str, str, str]:
    """部位識別 key（與既有新增/刪除比對邏輯一致：券商+帳戶+代碼+類型）。"""
    return (p.broker.lower(), (p.account or "").lower(), p.symbol.upper(), p.instrument_type)


def _cash_key(cash: CashPosition) -> tuple[str, str, str, str]:
    return (
        cash.broker.lower(),
        (cash.account or "").lower(),
        f"CASH {cash.currency}",
        "cash",
    )


def _holding_key(holding: Holding) -> tuple[str, str, str, str]:
    return (
        _cash_key(holding)
        if isinstance(holding, CashPosition)
        else _pos_key(holding)
    )


def _drop_overlay_for_positions(user: str, positions: list[Position]) -> None:
    drop_quote_overlay_keys(user, [_pos_key(p) for p in positions])


def _active_params(user: str) -> dict:
    """目前生效的門檻參數——優先取 QuantTrade 匯出的 Champion 參數。

    2026-08-06 策略實驗室分割後，調參權責移到 QuantTrade：它每次 feedback cycle
    結束會把各 family 現任 Champion 的參數寫成 `{user}_champion_params.json`。
    主頁讀它，是為了讓**畫面顯示的結論仍然是 ledger 正在評估的那個結論**——分家
    之後若各讀各的，兩組門檻會各自演化，bug#00089 鎖的「結論＝被回測＝同一函式」
    當場就破。

    契約檔不存在時（QuantTrade 還沒跑過，或使用者只裝 AssetTrack）回退到 legacy
    校準狀態，行為與分割前相同。讀檔失敗一律回退預設，永不讓主頁渲染因此崩潰。
    """
    try:
        import json as _json

        from .storage import get_data_dir

        safe = (user or "default").replace("/", "_")
        contract = get_data_dir() / f"{safe}_champion_params.json"
        if contract.exists():
            params = (_json.loads(contract.read_text(encoding="utf-8")) or {}).get("params")
            if isinstance(params, dict) and params:
                return params
    except Exception:
        pass
    try:
        from .calibration_schedule import ensure_state
        return ensure_state(user).get("active_params", {})
    except Exception:
        try:
            from .calibration_schedule import default_params
            return default_params()
        except Exception:
            return {}


@dataclass
class _DashboardAnalysisInputs:
    active_params: dict
    etf_snapshots: dict[str, list]
    options_underlyings: list[str]
    options_snapshots: dict[str, list]
    sector_groups: dict
    sector_snapshots: dict[str, list]


def _load_dashboard_analysis_inputs(
    user: str,
    positions: list[Position],
) -> _DashboardAnalysisInputs:
    """Read each dashboard analysis source once for one render generation."""
    from .storage import load_sector_daily_snapshots, load_sector_groups

    active_params = _active_params(user)
    etf_snapshots = {
        symbol: load_etf_daily_snapshots(symbol)
        for symbol in active_etf_symbols()
    }
    options_underlyings, _, _ = _watchlist_underlyings(user, positions)
    options_snapshots = {
        symbol: load_options_daily_snapshots(symbol)
        for symbol in options_underlyings
    }
    sector_groups = load_sector_groups(user)
    sector_snapshots = {
        name: load_sector_daily_snapshots(name)
        for name in sector_groups
    }
    return _DashboardAnalysisInputs(
        active_params=active_params,
        etf_snapshots=etf_snapshots,
        options_underlyings=options_underlyings,
        options_snapshots=options_snapshots,
        sector_groups=sector_groups,
        sector_snapshots=sector_snapshots,
    )


class RecommendationDetailScreen(Screen):
    """公式細節頁（bug#00118）：把一則投資建議（Recommendation）的第三層 breakdown
    完整展開——結論／判斷依據於頂部，下方逐 section 顯示公式、帶入本標的數字、計算方式
    說明。純顯示、零計算（資料在建 rec 時已算好，維持『結論＝被回測＝同一函式』）。
    Esc / q 返回上一頁。由各 detail 畫面的 action_show_formula 推入。"""

    BINDINGS = [
        Binding("escape", "go_back", "返回"),
        Binding("q",      "go_back", "返回", show=False),
    ]

    DEFAULT_CSS = """
    RecommendationDetailScreen { background: #0d1117; layout: vertical; }
    #rd-head { height: auto; padding: 0 1; margin: 1 2 0 2; }
    #rd-body { height: 1fr; margin: 1 2; padding: 0 1; }
    """

    def __init__(self, rec: "Recommendation") -> None:
        super().__init__()
        self.rec = rec

    def compose(self) -> ComposeResult:
        yield Static("", id="rd-head")
        with ScrollableContainer(id="rd-body"):
            yield Static("", id="rd-content")
        yield Footer()

    def on_mount(self) -> None:
        from rich.panel import Panel as _Panel
        rec = self.rec
        head_lines = [rec.verdict]
        if (rec.basis or "").strip():
            head_lines.append(f"[dim]判斷依據：[/dim]{rec.basis}")
        self.query_one("#rd-head", Static).update(
            _Panel("\n".join(head_lines), title="[bold]公式與計算細節[/bold]",
                   border_style="dim", padding=(0, 1))
        )

        from rich.console import Group
        renderables: list = []
        sections = self.rec.detail_sections or []
        if not sections:
            renderables.append(Static("[dim]此建議無額外公式細節。[/dim]"))
        for i, sec in enumerate(sections, 1):
            body_lines = []
            if sec.get("formula"):
                body_lines.append(f"[bold]公式[/bold]\n{sec['formula']}")
            if sec.get("substitution"):
                body_lines.append(f"[bold]帶入此標的數字[/bold]\n{sec['substitution']}")
            if sec.get("explanation"):
                body_lines.append(f"[bold]計算方式說明[/bold]\n[dim]{sec['explanation']}[/dim]")
            renderables.append(_Panel(
                "\n\n".join(body_lines) or "[dim]—[/dim]",
                title=f"[bold]{i}. {sec.get('heading','')}[/bold]",
                border_style="#334155", padding=(0, 1),
            ))
        self.query_one("#rd-content", Static).update(Group(*renderables))

    def action_go_back(self) -> None:
        self.dismiss()


class DashboardScreen(_FormulaDrillMixin, Screen):
    """AssetTrack 主看板畫面。支援鍵盤快速鍵與 Holdings 捲動。

    Holdings 表格直接操作：`e` 編輯整筆、`x` 刪除（游標列或已多選列）、
    `space` 切換多選標記；`1` 直接開啟批次新增部位對話框。
    """

    BINDINGS = [
        Binding("1",   "add_position",         "新增"),
        Binding("2",   "refresh_now",          "重整"),
        Binding("3",   "logout",               "登出"),
        Binding("4",   "upcoming_events",      "事件"),
        Binding("5",   "save_snapshot",        "快照"),
        Binding("6",   "sector_analysis",      "類股"),
        Binding("7",   "options_watchlist",    "期權"),
        Binding("8",   "active_etfs",          "ETF"),
        Binding("9",   "performance_tracking", "對標"),
        Binding("i",   "deposit",              "入金", show=False),
        Binding("o",   "withdrawal",           "出金", show=False),
        Binding("r",   "refresh_now",          "重整",   show=False),
        Binding("q",   "logout",               "登出",   show=False),
        Binding("ctrl+c", "logout",            "強制登出", show=False),
    ]

    def __init__(self, user: str, positions: list[Position], cash_positions: list[CashPosition], rate: float) -> None:
        super().__init__()
        self._user: str              = user
        self._positions: list[Position] = positions
        self._cash_positions: list[CashPosition] = cash_positions
        self._rate: float            = rate
        self._loading: bool          = False
        self.row_data: list[Optional[Holding]] = []
        self._marked: set[tuple[str, str, str, str]] = set()  # space 多選標記（批次刪除用）
        self._upcoming_events: list[tuple] = []
        self._events_fetched: bool = False
        self._fetching_events: bool = False
        self._events_last_fetched_at: float = 0.0
        self._events_last_attempt_at: float = 0.0
        self._events_symbols: tuple[str, ...] = ()
        self._events_last_attempt_symbols: tuple[str, ...] = ()
        self._analysis_last_rendered_at: float = 0.0
        self._analysis_signature: Optional[tuple] = None
        self._rf_rate: float         = 0.04  # risk-free rate (^IRX), warmed in background
        self._underlying_prices: dict[str, float] = {}
        self._live_quotes_ready: bool = False
        self._overlay_quotes_active: bool = False

    # ── Layout ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("", id="tui-header")
        with Horizontal(id="main-layout"):
            with Vertical(id="content-area"):
                yield Static("", id="metrics-row")
                yield Static("", id="broker-dist")
                yield Static(
                    "[dim]持倉[/dim]  [dim]e 編輯  x 刪除  space 多選  1 新增[/dim]",
                    id="holdings-label",
                )
                with Horizontal(id="holdings-row"):
                    with ScrollableContainer(id="holdings-scroll"):
                        yield DataTable(id="holdings-table")
                    yield Static("", id="recent-events-panel")
                with Horizontal(id="recommendations-scroll"):
                    yield Static("", id="sector-consensus-panel")
                    yield Static("", id="options-flow-panel")
                    yield Static("", id="etf-conclusions-panel")
        yield Static("", id="status-bar")
        yield Footer()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        table = self.query_one("#holdings-table", DataTable)
        table.cursor_type = "cell"
        table.add_columns(
            "代碼", "種類", "數量", "成本", "現價", "市值", "今日", "未實現",
        )

        self._render_all()
        self.set_interval(1.0,  self._tick_header)
        self.set_interval(60.0, self._do_refresh_worker)
        self._do_refresh_worker(load_from_disk=False)
        self._fetch_upcoming_events_worker()
        self.query_one("#holdings-table").focus()

    def on_key(self, event) -> None:
        if event.key in ("e", "x", "space"):
            table = self.query_one("#holdings-table", DataTable)
            if self.focused is not table:
                return
            row = table.cursor_coordinate.row
            pos = self.row_data[row] if 0 <= row < len(self.row_data) else None
            event.prevent_default()
            event.stop()
            if event.key == "space":
                if pos is not None:
                    self._marked.symmetric_difference_update({_holding_key(pos)})
                    self._render_all()
            elif event.key == "e":
                if pos is not None:
                    self.app.push_screen(
                        AddPositionModal(pos),
                        lambda res: self._handle_edit_position_result(pos, res)
                    )
            elif event.key == "x":
                if self._marked:
                    targets = [
                        p for p in self.row_data
                        if p is not None and _holding_key(p) in self._marked
                    ]
                elif pos is not None:
                    targets = [pos]
                else:
                    targets = []
                if targets:
                    self.app.push_screen(
                        DeleteConfirmModal(
                            targets,
                            sell_to_cash=(
                                _tracking_state(self._user).enabled
                                and all(
                                    isinstance(item, Position)
                                    for item in targets
                                )
                            ),
                        ),
                        lambda ok: self._handle_batch_delete_confirm(targets, ok)
                    )

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        row_idx = event.coordinate.row
        col_idx = event.coordinate.column

        if row_idx < 0 or row_idx >= len(self.row_data):
            return

        pos = self.row_data[row_idx]
        if pos is None:
            return
        if isinstance(pos, CashPosition):
            if _tracking_state(self._user).enabled:
                self.app.notify(
                    "追蹤期間不能直接修改現金；請用 i／o 宣告出入金。",
                    severity="warning",
                )
                return
            self.app.push_screen(
                AddPositionModal(pos),
                lambda result: self._handle_edit_position_result(pos, result),
            )
            return

        # Columns: 代碼, 種類, 數量, 成本, 現價, 市值, 今日, 未實現
        editable_fields = {
            0: ("symbol", "代碼", None),
            1: ("instrument_type", "種類", ["stock", "etf", "option"]),
            2: ("quantity", "數量", None),
            3: ("avg_cost", "成本", None),
        }

        if col_idx not in editable_fields:
            modal = PositionActionsModal(pos)
            self.app.push_screen(modal, lambda action: self._handle_position_action(pos, action))
            return

        field_name, field_label, choices = editable_fields[col_idx]
        current_val = getattr(pos, field_name)
        if current_val is None:
            current_str = ""
        elif field_name == "quantity":
            current_str = str(abs(current_val))
        else:
            current_str = str(current_val)

        modal = FieldEditModal(f"修改 {field_label}", field_name, current_str, choices)
        self.app.push_screen(modal, lambda val: self._handle_field_edit(pos, field_name, val))

    def _handle_field_edit(self, pos: Position, field_name: str, new_val: Optional[str]) -> None:
        if new_val is None:
            return
        if _tracking_state(self._user).enabled and field_name in {
            "symbol",
            "instrument_type",
            "quantity",
            "avg_cost",
            "market",
        }:
            self.app.notify(
                "追蹤期間不能直接改寫部位價值；加碼請按 1，出售請按 x。",
                severity="warning",
            )
            return

        positions, cash_positions = load_manual_positions(user=self._user)
        target = next((p for p in positions if p.broker == pos.broker and (p.account or "") == (pos.account or "") and p.symbol == pos.symbol), None)
        if not target:
            return

        if field_name == "symbol":
            if new_val:
                if target.market == "TW" and not new_val.endswith(".TW") and not new_val.endswith(".TWO"):
                    new_val = new_val + ".TW"
                target.symbol = new_val
        elif field_name == "instrument_type":
            if new_val:
                target.instrument_type = new_val  # type: ignore
        elif field_name == "quantity":
            if new_val:
                try:
                    qty = float(new_val)
                    side_str = "long" if target.quantity >= 0 else "short"
                    target.quantity = qty if side_str == "long" else -qty
                except ValueError:
                    pass
        elif field_name == "avg_cost":
            try:
                target.avg_cost = float(new_val) if new_val else None
            except ValueError:
                pass
        elif field_name == "market":
            if new_val:
                target.market = new_val
                if new_val == "US":
                    target.exchange = "NASDAQ"
                elif new_val == "TW":
                    target.exchange = "TSE"
                elif new_val == "HK":
                    target.exchange = "HKEX"

        # bug#00046: invalidate cached quote fields so the edited quantity/cost/symbol
        # is reflected in a freshly-computed market_value instead of a stale cached one.
        target.market_price = None
        target.market_value = None
        target.prev_close = None

        try:
            idx = positions.index(target)
            validated = Position.model_validate(target.model_dump())
            validated.last_updated = datetime.utcnow()
            positions[idx] = validated
            target = validated
        except Exception:
            return

        dup = next((
            p for p in positions
            if p is not target
            and p.broker.lower() == target.broker.lower()
            and (p.account or "").lower() == (target.account or "").lower()
            and p.symbol.upper() == target.symbol.upper()
        ), None)
        if dup:
            old_qty = dup.quantity
            new_qty = old_qty + target.quantity
            if dup.avg_cost is not None and target.avg_cost is not None:
                if old_qty > 0 and target.quantity > 0:
                    new_cost = (old_qty * dup.avg_cost + target.quantity * target.avg_cost) / new_qty
                else:
                    new_cost = target.avg_cost
            else:
                new_cost = target.avg_cost or dup.avg_cost
            dup.quantity = new_qty
            dup.avg_cost = new_cost
            dup.market_price = None
            dup.market_value = None
            dup.prev_close = None
            dup.last_updated = datetime.utcnow()
            positions.remove(target)

        save_manual_positions(positions, cash_positions=cash_positions, user=self._user)
        _drop_overlay_for_positions(self._user, [target] + ([dup] if dup else []))
        self._do_refresh_worker()

    def _handle_position_action(self, pos: Position, action: Optional[str]) -> None:
        if not action:
            return

        if action == "notes":
            modal = FieldEditModal("修改備註 (Notes)", "notes", pos.notes or "", choices=None)
            self.app.push_screen(modal, lambda val: self._apply_metadata_edit(pos, "notes", val))
        elif action == "sector":
            choices = ["科技", "半導體", "金融", "醫療", "能源", "消費", "ETF", "無分類"]
            modal = FieldEditModal("修改持倉分類 (Sector)", "sector", pos.sector or "無分類", choices=choices)
            self.app.push_screen(modal, lambda val: self._apply_metadata_edit(pos, "sector", val))
        elif action == "currency":
            choices = ["USD", "TWD", "HKD", "EUR", "JPY"]
            modal = FieldEditModal("修改計價幣別 (Currency)", "currency", pos.currency, choices=choices)
            self.app.push_screen(modal, lambda val: self._apply_metadata_edit(pos, "currency", val))
        elif action == "cost_currency":
            choices = ["USD", "TWD", "HKD", "EUR", "JPY", "同計價幣別"]
            modal = FieldEditModal("修改成本幣別 (Cost Currency)", "cost_currency", pos.cost_currency or "同計價幣別", choices=choices)
            self.app.push_screen(modal, lambda val: self._apply_metadata_edit(pos, "cost_currency", val))
        elif action == "broker_account":
            brokers = ["firstrade", "ibkr", "manual", "custom"]
            modal = FieldEditModal("選擇新券商 (Broker)", "broker", pos.broker, choices=brokers)
            self.app.push_screen(modal, lambda b: self._handle_broker_edit(pos, b))
        elif action == "delete":
            modal = DeleteConfirmModal(
                pos,
                sell_to_cash=_tracking_state(self._user).enabled,
            )
            self.app.push_screen(modal, lambda confirmed: self._handle_delete_confirm(pos, confirmed))

    def _apply_metadata_edit(self, pos: Position, field_name: str, new_val: Optional[str]) -> None:
        if new_val is None:
            return
        if _tracking_state(self._user).enabled and field_name in {
            "currency",
            "cost_currency",
        }:
            self.app.notify(
                "追蹤期間不能直接改寫部位幣別或成本幣別。",
                severity="warning",
            )
            return

        positions, cash_positions = load_manual_positions(user=self._user)
        target = next((p for p in positions if p.broker == pos.broker and (p.account or "") == (pos.account or "") and p.symbol == pos.symbol), None)
        if not target:
            return

        if field_name == "notes":
            target.notes = new_val if new_val else None
        elif field_name == "sector":
            val = new_val.strip()
            target.sector = None if val in ["無分類", "CLEAR", ""] else val
        elif field_name == "currency":
            if new_val:
                target.currency = new_val
        elif field_name == "cost_currency":
            val = new_val.strip()
            target.cost_currency = None if val in ["同計價幣別", "CLEAR", ""] else val

        try:
            idx = positions.index(target)
            validated = Position.model_validate(target.model_dump())
            validated.last_updated = datetime.utcnow()
            positions[idx] = validated
        except Exception:
            return

        save_manual_positions(positions, cash_positions=cash_positions, user=self._user)
        _drop_overlay_for_positions(self._user, [validated])
        self._do_refresh_worker()

    def _handle_broker_edit(self, pos: Position, broker: Optional[str]) -> None:
        if not broker:
            return
        if broker == "custom":
            modal = FieldEditModal("輸入自訂券商名稱", "broker", pos.broker, choices=None)
            self.app.push_screen(modal, lambda b_name: self._handle_account_edit(pos, b_name))
        else:
            acc_defaults = {"firstrade": "FT", "ibkr": "IBKR", "manual": "None"}
            default_acc = acc_defaults.get(broker, "None")
            modal = FieldEditModal("輸入帳戶代號 (Account, Enter=預設)", "account", default_acc, choices=None)
            self.app.push_screen(modal, lambda acc: self._apply_broker_account_edit(pos, broker, acc))

    def _handle_account_edit(self, pos: Position, broker_name: Optional[str]) -> None:
        if not broker_name:
            return
        modal = FieldEditModal("輸入帳戶代號 (Account, 留空=無)", "account", "", choices=None)
        self.app.push_screen(modal, lambda acc: self._apply_broker_account_edit(pos, broker_name, acc))

    def _apply_broker_account_edit(self, pos: Position, broker: str, account: Optional[str]) -> None:
        acc_val = account.strip() if account else ""
        if acc_val.upper() in ["NONE", "CLEAR", ""]:
            acc_val = ""

        positions, cash_positions = load_manual_positions(user=self._user)
        target = next((p for p in positions if p.broker == pos.broker and (p.account or "") == (pos.account or "") and p.symbol == pos.symbol), None)
        if not target:
            return

        target.broker = broker.lower()
        target.account = acc_val.upper() if acc_val else None

        try:
            idx = positions.index(target)
            validated = Position.model_validate(target.model_dump())
            validated.last_updated = datetime.utcnow()
            positions[idx] = validated
            target = validated
        except Exception:
            return

        dup = next((
            p for p in positions
            if p is not target
            and p.broker.lower() == target.broker.lower()
            and (p.account or "").lower() == (target.account or "").lower()
            and p.symbol.upper() == target.symbol.upper()
        ), None)
        if dup:
            old_qty = dup.quantity
            new_qty = old_qty + target.quantity
            if dup.avg_cost is not None and target.avg_cost is not None:
                if old_qty > 0 and target.quantity > 0:
                    new_cost = (old_qty * dup.avg_cost + target.quantity * target.avg_cost) / new_qty
                else:
                    new_cost = target.avg_cost
            else:
                new_cost = target.avg_cost or dup.avg_cost
            dup.quantity = new_qty
            dup.avg_cost = new_cost
            dup.market_price = None
            dup.market_value = None
            dup.prev_close = None
            dup.last_updated = datetime.utcnow()
            positions.remove(target)

        save_manual_positions(positions, cash_positions=cash_positions, user=self._user)
        _drop_overlay_for_positions(self._user, [target] + ([dup] if dup else []))
        self._do_refresh_worker()

    def _handle_delete_confirm(self, pos: Position, confirmed: Optional[bool]) -> None:
        if not confirmed:
            return
        if _tracking_state(self._user).enabled:
            self._handle_batch_delete_confirm([pos], confirmed)
            return
        positions, cash_positions = load_manual_positions(user=self._user)
        target = next((p for p in positions if p.broker == pos.broker and (p.account or "") == (pos.account or "") and p.symbol == pos.symbol), None)
        if target:
            positions.remove(target)
            save_manual_positions(positions, cash_positions=cash_positions, user=self._user)
            _drop_overlay_for_positions(self._user, [target])
            self._do_refresh_worker()

    # ── Header tick (every 1s, lightweight) ──────────────────────────────────

    def _tick_header(self) -> None:
        now_str = datetime.now().strftime("%H:%M:%S")
        if self._loading:
            quote = "[dim]更新中[/dim]"
        elif getattr(self, "_overlay_quotes_active", False):
            quote = "[dim]上次價格[/dim]"
        else:
            quote = "[dim]報價即時[/dim]"
        self.query_one("#tui-header", Static).update(
            _chrome_line("AssetTrack", self._user, now_str, _session_pills(), quote)
        )
        try:
            activity = list(getattr(self.app, "_fetch_activity", {}).values())
        except RuntimeError:
            activity = []
        if activity:
            bar = "[dim]抓取：[/dim]" + "、".join(activity)
        else:
            bar = "[dim]閒置[/dim]"
        try:
            self.query_one("#status-bar", Static).update(bar)
        except Exception:
            pass

    # ── Full render ───────────────────────────────────────────────────────────

    def _positions_for_display(self) -> list[Position]:
        """First paint may overlay last live quotes; the worker still fetches fresh."""
        if getattr(self, "_live_quotes_ready", False):
            self._overlay_quotes_active = False
            return self._positions
        display, as_of = apply_quote_overlay(self._user, self._positions)
        self._overlay_quotes_active = as_of is not None
        return display

    def _render_all(self) -> None:
        """Render all dashboard widgets from current in-memory data."""
        table = self.query_one("#holdings-table", DataTable)
        
        # Save cursor coordinate and focus state
        old_coordinate = table.cursor_coordinate
        had_focus = (self.focused == table)

        if not self._positions and not self._cash_positions:
            self.query_one("#metrics-row",     Static).update(
                Panel("[dim]尚無持倉。按 1 新增。[/dim]", border_style="dim", padding=(0, 1))
            )
            table.clear(columns=False)
            table.add_row(
                "[dim]尚無持倉。按 1 新增。[/dim]",
                "", "", "", "", "", "", "",
            )
            self.row_data = [None]
            self.query_one("#broker-dist",     Static).update("")
            self.query_one("#recent-events-panel", Static).update(
                self._build_recent_events_panel()
            )
            self._refresh_analysis_panels()

            if had_focus:
                table.focus()
            return

        positions = self._positions_for_display()
        has_quotes = all(
            p.market_price is not None or p.market_value is not None
            for p in positions
        )

        self.query_one("#metrics-row", Static).update(
            _build_metrics_panel(
                positions,
                self._rate,
                self._cash_positions,
                stale_quotes=self._overlay_quotes_active,
                underlying_prices=self._underlying_prices,
                risk_free_rate=self._rf_rate,
            )
        )

        table.clear(columns=False)
        self.row_data = []

        grouped: dict[str, dict[str, list]] = {}
        for bk, broker_positions in group_positions_by_broker(
            positions, self._rate
        ):
            grouped.setdefault(bk, {"positions": [], "cash": []})[
                "positions"
            ].extend(broker_positions)
        for cash in self._cash_positions:
            bk = (
                f"{cash.broker} ({cash.account})"
                if cash.account
                else cash.broker
            )
            grouped.setdefault(bk, {"positions": [], "cash": []})[
                "cash"
            ].append(cash)
        sorted_brokers = sorted(
            grouped.items(),
            key=lambda item: sum(
                p.value if p.currency == "USD" else p.value / self._rate
                for p in item[1]["positions"]
            ) + sum(
                c.amount if c.currency == "USD" else c.amount / self._rate
                for c in item[1]["cash"]
            ),
            reverse=True,
        )

        for bk, broker_holdings in sorted_brokers:
            bk_pos = broker_holdings["positions"]
            bk_cash = broker_holdings["cash"]
            bk_total = sum(
                p.value if p.currency == "USD" else p.value / self._rate for p in bk_pos
            ) + sum(
                c.amount if c.currency == "USD" else c.amount / self._rate
                for c in bk_cash
            )
            
            bk_total_s = f"[dim]${bk_total:,.0f}[/dim]" if has_quotes else "—"
            table.add_row(
                f"[dim]{bk.upper()}[/dim]",
                "", "", "", "", "", "",
                bk_total_s,
            )
            self.row_data.append(None)

            for p in bk_pos:
                qty_s   = f"{p.quantity:,.2f}" if p.quantity % 1 != 0 else f"{int(p.quantity):,}"
                cost_s  = f"${p.avg_cost:,.2f}" if p.avg_cost is not None else "—"
                price_s = f"${p.market_price:,.2f}" if p.market_price is not None else "—"
                if self._overlay_quotes_active and p.market_price is not None:
                    price_s = f"[dim]{price_s}[/dim]"
                val_s   = f"${p.value:,.2f}" if (p.market_price is not None or p.market_value is not None) else "—"
                mark_s = "[bold]· [/bold]" if _holding_key(p) in self._marked else ""
                table.add_row(
                    f"{mark_s}  [bold]{p.symbol}[/bold]",
                    f"[dim]{_instrument_label(p.instrument_type)}[/dim]",
                    qty_s,
                    cost_s,
                    price_s,
                    f"[bold]{val_s}[/bold]" if val_s != "—" else val_s,
                    _day_cell(p.daily_change_pct, p.daily_change, p.currency),
                    _pnl_cell(p.unrealized_pnl, p.unrealized_pnl_pct),
                )
                self.row_data.append(p)

            for cash in bk_cash:
                amount_s = f"{cash.amount:,.2f}"
                usd_value = (
                    cash.amount
                    if cash.currency == "USD"
                    else cash.amount / self._rate
                )
                value_s = f"${usd_value:,.2f}"
                mark_s = (
                    "[bold]· [/bold]"
                    if _holding_key(cash) in self._marked
                    else ""
                )
                table.add_row(
                    f"{mark_s}  [bold]CASH {cash.currency}[/bold]",
                    "[dim]現金[/dim]",
                    amount_s,
                    "—",
                    "—",
                    f"[bold]{value_s}[/bold]",
                    "[dim]—[/dim]",
                    "[dim]—[/dim]",
                )
                self.row_data.append(cash)

        self.query_one("#broker-dist", Static).update(
            _build_broker_panel(
                positions,
                self._rate,
                self._cash_positions,
                loading=self._loading,
                underlying_prices=self._underlying_prices,
                risk_free_rate=self._rf_rate,
            )
        )
        self.query_one("#recent-events-panel", Static).update(
            self._build_recent_events_panel()
        )
        self._refresh_analysis_panels()

        # Restore coordinate and focus state
        if len(self.row_data) > 0:
            old_row, old_col = old_coordinate
            new_row = min(old_row, len(self.row_data) - 1)
            new_col = min(old_col, 7)
            table.cursor_coordinate = (max(0, new_row), max(0, new_col))
        if had_focus:
            table.focus()

    # ── Background refresh worker (thread) ───────────────────────────────────

    @work(thread=True)
    def _do_refresh_worker(self, load_from_disk: bool = True) -> None:
        """Background thread: fetch rate + positions + live quotes."""
        if self._loading:
            return  # skip if already refreshing
        self._loading = True
        self.app._set_fetch_active('quotes', '即時報價與匯率')
        try:
            self._rate      = fetch_usdtwd_rate()
            # Warm the ^IRX risk-free cache off the UI thread so watchlist Greeks
            # use the same real rate as the 期權觀察清單 page.
            from .quotes import fetch_risk_free_rate
            self._rf_rate = fetch_risk_free_rate(default=self._rf_rate)
            if load_from_disk:
                self._positions, self._cash_positions = load_manual_positions(user=self._user)
            if self._positions:
                self._positions = enrich_positions_with_quotes(self._positions)
                from .quotes import _normalize_symbol_for_yf
                normalized = {
                    p.underlying.upper(): _normalize_symbol_for_yf(
                        p.underlying, "stock", p.currency
                    )
                    for p in self._positions
                    if p.instrument_type == "option" and p.underlying
                }
                prices = fetch_prices_batch(sorted(set(normalized.values())))
                self._underlying_prices = {
                    underlying: prices[quote_symbol]
                    for underlying, quote_symbol in normalized.items()
                    if prices.get(quote_symbol) is not None
                }
                seen_beta: set[str] = set()
                for p in self._positions:
                    beta_key = (p.underlying or p.symbol).upper()
                    if beta_key in seen_beta:
                        continue
                    seen_beta.add(beta_key)
                    fetch_beta(p.symbol, p.instrument_type, p.underlying, p.currency)
                if any(p.market_price is not None for p in self._positions):
                    save_quote_overlay(self._user, self._positions)
            else:
                self._underlying_prices = {}
            if not self._positions or any(
                p.market_price is not None for p in self._positions
            ):
                self._live_quotes_ready = True
                self._overlay_quotes_active = False
            self._maybe_record_performance_valuation()
        except Exception:
            pass
        finally:
            self._loading = False
            self.app._clear_fetch_active('quotes')
            kickoff = getattr(self.app, "_kickoff_research_ingest_once", None)
            if callable(kickoff):
                try:
                    self.app.call_from_thread(kickoff)
                except Exception:
                    pass
        # Schedule UI update back on the event loop
        self.app.call_from_thread(self._render_all)
        if load_from_disk and self._events_refresh_due():
            self._fetch_upcoming_events_worker()

    def _event_symbols(self) -> tuple[str, ...]:
        """Return the position signature that determines earnings-calendar data."""
        return tuple(sorted({
            (p.underlying if p.instrument_type == "option" else p.symbol).upper()
            for p in self._positions
        }))

    def _events_refresh_due(self) -> bool:
        """Refresh slowly-changing calendar data without retrying every minute."""
        if self._fetching_events:
            return False
        now = time.monotonic()
        symbols = self._event_symbols()
        if symbols != self._events_last_attempt_symbols:
            return True
        if not self._events_fetched:
            return now - self._events_last_attempt_at >= _EVENTS_RETRY_INTERVAL_SECONDS
        return (
            symbols != self._events_symbols
            or now - self._events_last_fetched_at >= _EVENTS_REFRESH_INTERVAL_SECONDS
        )

    def _maybe_record_performance_valuation(self) -> None:
        """Create the opt-in baseline immediately and one valuation each Sunday."""
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Taipei"))
        tracker = PortfolioPerformanceTracker(
            user=self._user,
            data_dir=get_data_dir(),
            benchmark_prices=YFinanceBenchmarkPrices(),
        )
        if not tracker.valuation_due(now):
            return
        total_value = total_asset_value_usd(
            self._positions,
            self._cash_positions,
            self._rate,
        )
        if total_value is None or total_value <= 0:
            return
        tracker.record_valuation(
            total_value_usd=total_value,
            recorded_at=now,
        )

    @work(thread=True)
    def _fetch_upcoming_events_worker(self) -> None:
        if self._fetching_events:
            return
        self._fetching_events = True
        self._events_last_attempt_at = time.monotonic()
        self._events_last_attempt_symbols = self._event_symbols()
        self.app._set_fetch_active('events', '財報行事曆與總經數據')
        
        from datetime import datetime as dt_cls, timedelta
        from .quotes import _normalize_symbol_for_yf
        from .shared import get_upcoming_macro_events

        try:
            portfolio_tickers = set()
            for p in self._positions:
                sym = p.underlying if p.instrument_type == "option" else p.symbol
                norm_sym = _normalize_symbol_for_yf(sym, "stock", p.currency)
                portfolio_tickers.add(norm_sym)

            unique_tickers = list(portfolio_tickers.union(SOX_TICKERS))

            ticker_to_data = fetch_earnings_calendar(unique_tickers)

            today = datetime.now(timezone.utc).date()
            start_date = today  # 只顯示今天(含)以後的事件，過去事件不再列出
            cutoff = today + timedelta(days=90)

            events = []

            for sym, (dates_list, info_date, time_str, period_str) in ticker_to_data.items():
                is_user = any(
                    _normalize_symbol_for_yf(p.underlying if p.instrument_type == "option" else p.symbol, "stock", p.currency) == sym
                    for p in self._positions
                )
                is_sox = sym in SOX_TICKERS

                if is_user and is_sox:
                    label_base = f"🔔 [bold white]{sym}[/bold white] 財報公佈 (持倉/SOX 十大)"
                elif is_user:
                    label_base = f"🔔 [bold white]{sym}[/bold white] 財報公佈 (持倉)"
                else:
                    label_base = f"💻 {sym} 財報公佈 (SOX 十大)"

                if info_date and start_date <= info_date <= cutoff:
                    if period_str:
                        label = f"{label_base} ({period_str} {time_str})"
                    else:
                        label = f"{label_base} ({time_str})"
                    events.append((info_date, label))
                else:
                    for d in dates_list:
                        if isinstance(d, dt_cls):
                            d = d.date()
                        if start_date <= d <= cutoff:
                            events.append((d, label_base))

            from .shared import MACRO_EVENT_NAMES
            macro_list = get_upcoming_macro_events(days=90, start_days_ago=0)
            for ev_date, ev_label, time_str in macro_list:
                event_name = MACRO_EVENT_NAMES.get(ev_label, ev_label)
                events.append((ev_date, f"{event_name} ({time_str})"))

            events.sort(key=lambda x: x[0])
            self.app.call_from_thread(self._on_events_fetched, events)
        except Exception:
            pass
        finally:
            self._fetching_events = False
            self.app._clear_fetch_active('events')

    def _on_events_fetched(self, events: list[tuple]) -> None:
        self._upcoming_events = events
        self._events_fetched = True
        self._events_last_fetched_at = time.monotonic()
        self._events_symbols = self._events_last_attempt_symbols
        self._refresh_events_panel()

    def _refresh_events_panel(self) -> None:
        self.query_one("#recent-events-panel", Static).update(
            self._build_recent_events_panel()
        )

    def _analysis_input_signature(self) -> tuple:
        positions = tuple(sorted(
            (
                position.broker.casefold(),
                (position.account or "").casefold(),
                position.symbol.upper(),
                position.instrument_type,
                position.underlying or "",
                position.quantity,
            )
            for position in self._positions
        ))
        return positions, round(self._rf_rate, 6)

    def _refresh_analysis_panels(self, *, force: bool = False) -> None:
        """Refresh slow offline analysis only when its inputs may have changed."""
        now = time.monotonic()
        signature = self._analysis_input_signature()
        if (
            not force
            and signature == self._analysis_signature
            and now - self._analysis_last_rendered_at
            < _DASHBOARD_ANALYSIS_REFRESH_SECONDS
        ):
            return

        inputs = _load_dashboard_analysis_inputs(self._user, self._positions)
        self.query_one("#etf-conclusions-panel", Static).update(
            self._build_etf_conclusions_panel(inputs)
        )
        self.query_one("#options-flow-panel", Static).update(
            self._build_options_flow_panel(inputs)
        )
        self.query_one("#sector-consensus-panel", Static).update(
            self._build_sector_consensus_panel(inputs)
        )
        self._analysis_signature = signature
        self._analysis_last_rendered_at = now

    def _build_recent_events_panel(self) -> Panel:
        from rich.panel import Panel
        from datetime import timedelta
        
        today = datetime.now(timezone.utc).date()
        
        if not self._events_fetched:
            return Panel("[dim]同步行事曆…[/dim]", title="事件", border_style="dim", padding=(0, 1))
            
        if not self._upcoming_events:
            return Panel("[dim]30 天內無事[/dim]", title="事件", border_style="dim", padding=(0, 1))
            
        cutoff = today + timedelta(days=30)
        recent = []
        for d, label in self._upcoming_events:
            if today <= d <= cutoff:
                recent.append((d, label))
                
        if not recent:
            return Panel("[dim]30 天內無事[/dim]", title="事件", border_style="dim", padding=(0, 1))
            
        recent.sort(key=lambda x: x[0])
        
        lines = []
        for d, label in recent[:8]:
            days_away = (d - today).days
            days_str = "今天" if days_away == 0 else f"{days_away}天"
            date_str = d.strftime("%m-%d")
            simplified = _simplify_event_label(label)
            for mark in ("🔔 ", "💻 ", "▼ ", "★ ", "◆ "):
                simplified = simplified.replace(mark, "")
            badge = ""
            if "持倉" in label:
                badge = "  持倉"
            elif "SOX" in label:
                badge = "  SOX"
            lines.append(f"{date_str}  {simplified}{badge}  [dim]{days_str}[/dim]")
            
        if len(recent) > 8:
            lines.append(f"[dim]另 {len(recent) - 8} 件 · 4[/dim]")
            
        return Panel("\n".join(lines), title="事件", border_style="dim", padding=(0, 1))

    def _build_etf_conclusions_panel(
        self,
        inputs: Optional[_DashboardAnalysisInputs] = None,
    ) -> Panel:
        """bug#00061: 首頁「交易策略建議」卡片之一 —— 主動式ETF跨基金持股趨勢結論。
        100% 離線本機運算（讀取 etf_cache/history/*.jsonl 真實累積快照），無網路請求；
        與「主動式ETF排行」頁面的進階分析畫面共用同一份 generate_etf_conclusions()
        輸出，兩處文字保證一致。資料不足時誠實顯示收集進度，不生成假結論。
        """
        from rich.panel import Panel

        if not etf_watchlist_is_configured(self._user):
            return Panel(
                "[dim]未設清單 · 8[/dim]",
                title="8  ETF 觀察", border_style="dim", padding=(0, 1),
            )
        _ap = (
            inputs.active_params if inputs is not None else _active_params(self._user)
        ).get('etf', {})
        _ct = _ap.get('consensus_threshold', 0.5)
        if inputs is not None:
            snapshots_by_etf = inputs.etf_snapshots
        else:
            all_symbols = active_etf_symbols()
            snapshots_by_etf = {
                sym: load_etf_daily_snapshots(sym) for sym in all_symbols
            }
        report = compute_symbol_trends(
            snapshots_by_etf,
            window_days=ADVANCED_ANALYSIS_WINDOW_DAYS,
            consensus_threshold=_ct,
        )
        watchlist = load_etf_watchlist(self._user)
        rows = [
            row for row in watchlist_etf_activity(report, watchlist)
            if row.get("has_trade")
        ][:3]
        if not rows:
            body = "[dim]本視窗無買賣 · 8[/dim]"
        else:
            lines = []
            for row in rows:
                first, last = row.get("first_date"), row.get("last_date")
                period = f"{first}～{last}" if first and last else (first or last or "—")
                verb = "買入" if row.get("consensus") == "up" else (
                    "賣出" if row.get("consensus") == "down" else "有買賣"
                )
                lines.append(f"{row['symbol']}  {verb}\n[dim]{period}[/dim]")
            body = "\n".join(lines)

        return Panel(body, title="8  ETF 觀察", border_style="dim", padding=(0, 1))

    def _build_options_flow_panel(
        self,
        inputs: Optional[_DashboardAnalysisInputs] = None,
    ) -> Panel:
        """首頁期權卡：只描述已觀察到的市場樣態，不輸出多空投資建議。"""
        from rich.panel import Panel

        underlyings = (
            inputs.options_underlyings
            if inputs is not None
            else _watchlist_underlyings(self._user, self._positions)[0]
        )
        if not underlyings:
            return Panel(
                "[dim]尚無標的 · 7[/dim]",
                title="7  期權樣態", border_style="dim", padding=(0, 1),
            )

        snapshots_by_underlying = (
            inputs.options_snapshots
            if inputs is not None
            else {u: load_options_daily_snapshots(u) for u in underlyings}
        )
        observed = compute_observed_regime(snapshots_by_underlying)
        regime_label = {
            "down": "[red]偏空[/red]",
            "up": "[green]偏多[/green]",
            "mixed": "分化",
        }.get(observed["state"], "[dim]累積中[/dim]")
        iv_text = {
            "rising": "IV 升",
            "falling": "IV 降",
            "stable": "IV 平",
            "unknown": "IV —",
        }[observed["iv_state"]]
        ready = observed.get("ready_count") or 0
        total = len(underlyings)
        expensive = cheap = fair = unknown = 0
        for snaps in snapshots_by_underlying.values():
            report = richness_from_history(snaps or [])
            if not report.get("ready"):
                unknown += 1
            elif report["richness"] == "expensive":
                expensive += 1
            elif report["richness"] == "cheap":
                cheap += 1
            else:
                fair += 1
        if ready == 0:
            body = f"[dim]資料 {ready}/{total} · 7[/dim]"
        else:
            rich = f"貴 {expensive} · 便宜 {cheap} · 公允 {fair}"
            if unknown:
                rich += f" · 不足 {unknown}"
            body = (
                f"{regime_label} · {iv_text}\n"
                f"{rich}\n"
                f"[dim]非股價預測 · 7[/dim]"
            )
        return Panel(body, title="7  期權樣態", border_style="dim", padding=(0, 1))

    def _build_sector_consensus_panel(
        self,
        inputs: Optional[_DashboardAnalysisInputs] = None,
    ) -> Panel:
        """首頁「交易策略建議」第三張卡片：類股未來 10 個交易日方向預測。
        與「類股板塊分析」頁面共用 generate_sector_recommendations()／
        generate_sector_conclusions()，兩處結論文字一致。"""
        from rich.panel import Panel
        from .storage import (
            load_sector_groups, load_sector_daily_snapshots,
            load_sector_predictive_model,
        )
        from . import sector_analysis

        groups = (
            inputs.sector_groups
            if inputs is not None
            else load_sector_groups(self._user)
        )
        if not groups:
            return Panel(
                "[dim]尚無板塊 · 6[/dim]",
                title="6  類股 · 10 日", border_style="dim", padding=(0, 1),
            )

        # bug#00095 接線：套用已確認的校準參數（breadth_threshold / min_days）。
        _sap = (
            inputs.active_params if inputs is not None else _active_params(self._user)
        ).get('sector', {})
        _bth = _sap.get('breadth_threshold', 0.5)
        _md = _sap.get('min_days', 3)
        if inputs is not None:
            snapshots_by_group = inputs.sector_snapshots
        else:
            snapshots_by_group = {
                name: load_sector_daily_snapshots(name) for name in groups
            }
        flows = {
            name: sector_analysis.detect_broad_flow(
                snapshots_by_group[name],
                breadth_threshold=_bth,
                min_days=_md,
            )
            for name in groups
        }
        # bug#00093: 與結論卡共用同一套 walk-forward 回測，命中率就地顯示於每則類股共識。
        _sec_bt = sector_analysis.backtest_sector_flow(
            snapshots_by_group,
            breadth_threshold=_bth,
            min_days=_md,
        )
        _model = load_sector_predictive_model(self._user, groups) or {}
        confirmations = (
            (_model.get("sector_confirmation") or {}).get("groups") or {}
        )
        recs = sector_analysis.generate_sector_recommendations(
            flows, confirmations=confirmations, backtest=_sec_bt
        )

        if not recs:
            ready = sum(1 for f in flows.values() if f.get("ready"))
            body = f"[dim]資料 {ready}/{len(groups)} · 6[/dim]"
        else:
            lines = []
            for rec in recs[:2]:
                name = rec.rec_id.split(":", 1)[-1]
                if rec.direction == "多":
                    lines.append(f"[green]{name}  偏多[/green]")
                elif rec.direction == "空":
                    lines.append(f"[red]{name}  偏空[/red]")
                else:
                    lines.append(f"{name}  {rec.direction or '—'}")
            body = "\n".join(lines) + "\n[dim]6[/dim]"

        return Panel(body, title="6  類股 · 10 日", border_style="dim", padding=(0, 1))

    # ── Action handlers ───────────────────────────────────────────────────────

    def action_deposit(self) -> None:
        self._open_cash_flow("deposit")

    def action_withdrawal(self) -> None:
        self._open_cash_flow("withdrawal")

    def _open_cash_flow(self, direction: str) -> None:
        if not _tracking_state(self._user).enabled:
            self.app.notify(
                "績效追蹤尚未啟用；按 9 進入比較頁啟用。",
                severity="warning",
            )
            return
        self.app.push_screen(
            CashFlowModal(direction),
            self._handle_cash_flow_result,
        )

    def _handle_cash_flow_result(self, result: Optional[dict]) -> None:
        if result:
            self.app.notify("正在記錄出入金並同步 QQQ／VT…")
            self._save_declared_cash_flow(result)

    @work(thread=True)
    def _save_declared_cash_flow(self, declaration: dict) -> None:
        positions, cash_positions = load_manual_positions(self._user)
        try:
            positions, cash_positions = _process_declared_cash_flow(
                user=self._user,
                positions=positions,
                cash_positions=cash_positions,
                rate=self._rate,
                declaration=declaration,
            )
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify,
                f"出入金記錄失敗：{exc}",
                severity="error",
            )
            return
        self._positions = positions
        self._cash_positions = cash_positions
        label = "入金" if declaration["direction"] == "deposit" else "出金"
        self.app.call_from_thread(
            self.app.notify,
            f"{label}已記錄，benchmark 資金流已同步。",
        )
        self.app.call_from_thread(self._render_all)

    def action_add_position(self) -> None:
        """[1] 直接開啟批次新增部位對話框（編輯/刪除改由表格 e / x / space 直接操作）。"""
        self.app.push_screen(AddPositionModal(), self._handle_add_position_result)

    def _handle_edit_position_result(
        self,
        old_pos: Holding,
        result: Optional[list[Holding]],
    ) -> None:
        if result:
            updated_pos = result[0]
            if _tracking_state(self._user).enabled:
                if isinstance(old_pos, CashPosition):
                    self.app.notify(
                        "追蹤期間不能直接修改現金；請用 i／o 宣告出入金。",
                        severity="warning",
                    )
                    return
                if isinstance(updated_pos, Position):
                    economic_fields = (
                        "broker",
                        "account",
                        "symbol",
                        "instrument_type",
                        "quantity",
                        "avg_cost",
                        "currency",
                        "market",
                    )
                    if any(
                        getattr(old_pos, field) != getattr(updated_pos, field)
                        for field in economic_fields
                    ):
                        self.app.notify(
                            "追蹤期間不能直接改寫部位；加碼請按 1，出售請按 x。",
                            severity="warning",
                        )
                        return
            positions, cash_positions = load_manual_positions(self._user)
            if isinstance(old_pos, CashPosition) and isinstance(
                updated_pos, CashPosition
            ):
                for idx, cash in enumerate(cash_positions):
                    if _cash_key(cash) == _cash_key(old_pos):
                        cash_positions[idx] = updated_pos
                        break
            elif isinstance(old_pos, Position) and isinstance(
                updated_pos, Position
            ):
                for idx, p in enumerate(positions):
                    if _pos_key(p) == _pos_key(old_pos):
                        positions[idx] = updated_pos
                        break
            else:
                return
            save_manual_positions(positions, cash_positions=cash_positions, user=self._user)
            # 部位識別 key 可能已變更，移除舊標記避免殘留
            self._marked.discard(_holding_key(old_pos))
            self.app.notify("修改持倉成功！")
            self._positions = positions
            self._cash_positions = cash_positions
            self._do_refresh_worker()

    def _handle_batch_delete_confirm(
        self,
        targets: list[Holding],
        confirmed: bool | None,
    ) -> None:
        if not confirmed:
            return
        if _tracking_state(self._user).enabled:
            if any(isinstance(item, CashPosition) for item in targets):
                self.app.notify(
                    "追蹤期間不能刪除現金；請用 o 宣告出金。",
                    severity="error",
                )
                return
            positions, cash_positions = load_manual_positions(self._user)
            tracker = PortfolioPerformanceTracker(
                user=self._user,
                data_dir=get_data_dir(),
            )
            try:
                for target in targets:
                    stored = next(
                        (
                            item
                            for item in positions
                            if _pos_key(item) == _pos_key(target)
                        ),
                        None,
                    )
                    if stored is None:
                        continue
                    if stored.market_price is None:
                        stored.market_price = target.market_price
                    positions, cash_positions = tracker.apply_position_sale(
                        positions=positions,
                        cash_positions=cash_positions,
                        position=stored,
                        quantity=abs(stored.quantity),
                    )
            except ValueError as exc:
                self.app.notify(str(exc), severity="error")
                return
            save_manual_positions(
                positions,
                cash_positions=cash_positions,
                user=self._user,
            )
            self._marked.clear()
            self._positions = positions
            self._cash_positions = cash_positions
            self.app.notify(
                f"已出售 {len(targets)} 筆部位，價值已轉入帳戶現金。"
            )
            self._do_refresh_worker()
            return
        keys = {_holding_key(p) for p in targets}
        positions, cash_positions = load_manual_positions(self._user)
        new_positions = [
            p for p in positions if _holding_key(p) not in keys
        ]
        new_cash_positions = [
            cash for cash in cash_positions
            if _holding_key(cash) not in keys
        ]
        removed = (
            len(positions) + len(cash_positions)
            - len(new_positions) - len(new_cash_positions)
        )
        save_manual_positions(
            new_positions,
            cash_positions=new_cash_positions,
            user=self._user,
        )
        self._marked -= keys
        self.app.notify(f"已刪除 {removed} 筆部位！")
        self._positions = new_positions
        self._cash_positions = new_cash_positions
        drop_quote_overlay_keys(
            self._user,
            [key for key in keys if len(key) == 4],
        )
        self._do_refresh_worker()

    @staticmethod
    def _merge_position(positions: list[Position], pos: Position) -> None:
        """將單筆新部位合併進清單（同 key 加碼合併，否則附加）。就地修改 positions。"""
        for p in positions:
            if _pos_key(p) == _pos_key(pos):
                old_qty = p.quantity
                new_qty = old_qty + pos.quantity
                same_direction = (old_qty >= 0) == (pos.quantity >= 0)
                if same_direction:
                    # 同方向加碼：以「絕對數量」加權平均成本，多單與空單皆正確
                    # （舊版只在 new_qty > 0 時計算，導致空單加碼後成本永不更新）
                    if p.avg_cost is not None and pos.avg_cost is not None and new_qty != 0:
                        p.avg_cost = (
                            p.avg_cost * abs(old_qty) + pos.avg_cost * abs(pos.quantity)
                        ) / abs(new_qty)
                    else:
                        p.avg_cost = pos.avg_cost or p.avg_cost
                elif abs(pos.quantity) > abs(old_qty):
                    # 反向且已翻倉：剩餘部位屬新方向，成本改採新進場成本
                    p.avg_cost = pos.avg_cost if pos.avg_cost is not None else p.avg_cost
                # 反向但未翻倉（部分平倉）：保留原方向平均成本，不變動
                p.quantity = new_qty
                p.market_price = None
                p.market_value = None
                p.prev_close = None
                p.last_updated = datetime.utcnow()
                return
        positions.append(pos)

    def _handle_add_position_result(self, result: Optional[list[Holding]]) -> None:
        if result:
            positions, cash_positions = load_manual_positions(self._user)
            if _tracking_state(self._user).enabled:
                if any(isinstance(item, CashPosition) for item in result):
                    self.app.notify(
                        "追蹤期間不能直接新增現金；請用 i 宣告入金。",
                        severity="error",
                    )
                    return
                tracker = PortfolioPerformanceTracker(
                    user=self._user,
                    data_dir=get_data_dir(),
                )
                try:
                    for holding in result:
                        positions, cash_positions = tracker.apply_position_purchase(
                            positions=positions,
                            cash_positions=cash_positions,
                            purchase=holding,
                        )
                except ValueError as exc:
                    self.app.notify(str(exc), severity="error")
                    return
                save_manual_positions(
                    positions,
                    cash_positions=cash_positions,
                    user=self._user,
                )
                n = len(result)
                self.app.notify(
                    f"已買進 {n} 筆部位並扣除帳戶現金！"
                    if n > 1
                    else "已買進部位並扣除帳戶現金！"
                )
                self._positions = positions
                self._cash_positions = cash_positions
                self._do_refresh_worker()
                return
            for holding in result:
                if isinstance(holding, CashPosition):
                    merge_cash_position(cash_positions, holding)
                else:
                    self._merge_position(positions, holding)
            save_manual_positions(positions, cash_positions=cash_positions, user=self._user)
            n = len(result)
            self.app.notify(f"已儲存 {n} 筆持倉！" if n > 1 else "新增持倉成功！")
            self._positions = positions
            self._cash_positions = cash_positions
            _drop_overlay_for_positions(
                self._user,
                [item for item in result if isinstance(item, Position)],
            )
            self._do_refresh_worker()

    def action_refresh_now(self) -> None:
        """[2] 立即重整：背景更新報價。"""
        self._analysis_signature = None
        self._do_refresh_worker()

    def action_logout(self) -> None:
        """[3] 安全登出：Textual Modal 確認 → 返回 LoginScreen。"""
        self.app.push_screen(LogoutConfirmModal(), self._handle_logout_confirm)

    def _handle_logout_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            lock_vault()
            self.dismiss(True)

    def action_save_snapshot(self) -> None:
        """[5] 儲存快照：背景非阻塞執行，不 suspend。"""
        self.app.notify("⚙️ 正在儲存市值快照...")
        self.run_save_snapshot()

    @work(thread=True)
    def run_save_snapshot(self) -> None:
        """直接實作快照邏輯，不依賴 cli.py（避免 thread worker 中的 Confirm.ask 問題）。"""
        from .quotes import enrich_positions_with_quotes, current_portfolio_value
        from .storage import Storage
        from .models import PortfolioSnapshot
        from datetime import datetime as _dt
        try:
            positions, _ = load_manual_positions(user=self._user)
            enriched = enrich_positions_with_quotes(positions)
            total_val = current_portfolio_value(enriched)
            storage = Storage(user=self._user)
            by_broker: dict[str, float] = {}
            for p in enriched:
                by_broker[p.broker] = by_broker.get(p.broker, 0.0) + (p.market_value or 0.0)
            snap = PortfolioSnapshot(
                timestamp=_dt.utcnow(),
                total_value=total_val,
                by_broker=by_broker,
                positions=enriched,
                notes="tui_snapshot"
            )
            storage.save_snapshot(snap)
            self.app.call_from_thread(self.app.notify, "市值快照儲存成功！", title="快照")
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"儲存快照失敗: {e}", title="快照", severity="error")

    def action_upcoming_events(self) -> None:
        """[4] 近期重大事件：推入 UpcomingEventsScreen，不 suspend。"""
        self.app.push_screen(UpcomingEventsScreen(self._user, self._positions))

    def action_active_etfs(self) -> None:
        """[8] 主動式 ETF 動態：推入 ActiveETFsScreen（預設建議頁）。"""
        self.app.push_screen(ActiveETFsScreen(self._user, self._rate))

    def action_options_watchlist(self) -> None:
        """[7] 期權觀察清單：推入 OptionsWatchlistScreen，不 suspend。"""
        self.app.push_screen(OptionsWatchlistScreen(self._user, self._positions))

    def action_sector_analysis(self) -> None:
        """[6] 類股板塊分析：推入 SectorAnalysisScreen，不 suspend。"""
        self.app.push_screen(SectorAnalysisScreen(self._user))

    def action_performance_tracking(self) -> None:
        """[9] 開啟使用者投資組合與 QQQ／VT 的績效比較。"""
        self.app.push_screen(
            PerformanceTrackingScreen(
                self._user,
                self._positions,
                self._cash_positions,
                self._rate,
            )
        )

# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────


class CashFlowModal(ModalScreen[Optional[dict]]):
    """Declare money entering or leaving the complete tracked portfolio."""

    DEFAULT_CSS = """
    CashFlowModal {
        align: center middle;
    }
    #cash-flow-dialog {
        width: 62;
        height: auto;
        max-height: 95%;
        border: solid #21262d;
        background: #161b22;
        padding: 1 2;
    }
    #cash-flow-title {
        color: #58a6ff;
        text-style: bold;
        margin-bottom: 1;
    }
    .cash-flow-row {
        height: auto;
        margin-bottom: 1;
    }
    .cash-flow-label {
        width: 18;
        color: #8b949e;
    }
    .cash-flow-field {
        width: 38;
    }
    #cash-flow-error {
        color: #ff7b72;
        height: auto;
    }
    #cash-flow-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    #cash-flow-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, direction: str) -> None:
        super().__init__()
        if direction not in ("deposit", "withdrawal"):
            raise ValueError("direction must be deposit or withdrawal")
        self.direction = direction

    def compose(self) -> ComposeResult:
        is_deposit = self.direction == "deposit"
        categories = (
            [
                ("薪資／收入", "salary"),
                ("儲蓄轉入", "savings"),
                ("資產出售", "asset_sale"),
                ("贈與", "gift"),
                ("其他", "other"),
            ]
            if is_deposit
            else [
                ("購屋", "home_purchase"),
                ("購車", "vehicle_purchase"),
                ("生活支出", "living_expense"),
                ("稅款", "tax"),
                ("轉出", "transfer_out"),
                ("其他", "other"),
            ]
        )
        with Vertical(id="cash-flow-dialog"):
            yield Label(
                "宣告入金" if is_deposit else "宣告出金",
                id="cash-flow-title",
            )
            yield Label(
                "此紀錄會同步調整 QQQ／VT 影子部位，不計入投資績效。",
            )
            with Horizontal(classes="cash-flow-row"):
                yield Label("券商／帳戶 *", classes="cash-flow-label")
                yield Select(
                    [("manual", "manual"), ("FT", "FT"), ("IBKR", "IBKR")],
                    value="manual",
                    id="cash-flow-broker",
                    classes="cash-flow-field",
                )
            with Horizontal(classes="cash-flow-row"):
                yield Label("帳戶代號", classes="cash-flow-label")
                yield Input(id="cash-flow-account", classes="cash-flow-field")
            with Horizontal(classes="cash-flow-row"):
                yield Label("金額 *", classes="cash-flow-label")
                yield Input(
                    placeholder="正數金額",
                    id="cash-flow-amount",
                    classes="cash-flow-field",
                )
            with Horizontal(classes="cash-flow-row"):
                yield Label("幣別 *", classes="cash-flow-label")
                yield Select(
                    [("USD", "USD"), ("TWD", "TWD")],
                    value="USD",
                    id="cash-flow-currency",
                    classes="cash-flow-field",
                )
            with Horizontal(classes="cash-flow-row"):
                yield Label("用途／來源 *", classes="cash-flow-label")
                yield Select(
                    categories,
                    value=categories[0][1],
                    id="cash-flow-category",
                    classes="cash-flow-field",
                )
            with Horizontal(classes="cash-flow-row"):
                yield Label("管道 *", classes="cash-flow-label")
                yield Select(
                    [
                        ("銀行轉帳", "bank_transfer"),
                        ("券商轉帳", "broker_transfer"),
                        ("現金", "cash"),
                        ("其他", "other"),
                    ],
                    value="bank_transfer",
                    id="cash-flow-channel",
                    classes="cash-flow-field",
                )
            with Horizontal(classes="cash-flow-row"):
                yield Label("備註", classes="cash-flow-label")
                yield Input(id="cash-flow-notes", classes="cash-flow-field")
            yield Label("", id="cash-flow-error")
            with Horizontal(id="cash-flow-buttons"):
                yield Button(
                    "記錄入金" if is_deposit else "記錄出金",
                    variant="primary",
                    id="cash-flow-confirm",
                )
                yield Button("取消", id="cash-flow-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cash-flow-cancel":
            self.dismiss(None)
        elif event.button.id == "cash-flow-confirm":
            self._submit()

    def _submit(self) -> None:
        try:
            amount = float(self.query_one("#cash-flow-amount", Input).value)
        except ValueError:
            amount = 0
        if amount <= 0:
            self.query_one("#cash-flow-error", Label).update(
                "金額必須是大於零的數字。"
            )
            return
        broker = str(self.query_one("#cash-flow-broker", Select).value)
        currency = str(self.query_one("#cash-flow-currency", Select).value)
        category = str(self.query_one("#cash-flow-category", Select).value)
        channel = str(self.query_one("#cash-flow-channel", Select).value)
        account = self.query_one("#cash-flow-account", Input).value.strip()
        notes = self.query_one("#cash-flow-notes", Input).value.strip()
        self.dismiss(
            {
                "direction": self.direction,
                "broker": broker,
                "account": account or None,
                "amount": amount,
                "currency": currency,
                "category": category,
                "channel": channel,
                "notes": notes or None,
            }
        )

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class PerformanceTrackingCancelConfirmModal(ModalScreen[bool]):
    """Confirm that the current performance-tracking interval should stop."""

    DEFAULT_CSS = """
    PerformanceTrackingCancelConfirmModal {
        align: center middle;
    }
    #performance-cancel-dialog {
        width: 64;
        height: auto;
        border: thick $warning;
        background: $panel;
        padding: 1 2;
    }
    #performance-cancel-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    #performance-cancel-message {
        height: auto;
        margin-bottom: 1;
    }
    #performance-cancel-buttons {
        height: auto;
        align: right middle;
    }
    #performance-cancel-buttons Button {
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="performance-cancel-dialog"):
            yield Label(
                "取消績效追蹤",
                id="performance-cancel-title",
            )
            yield Label(
                "取消後會停止建立績效快照，並解除持股與現金的追蹤期間限制。\n\n"
                "既有估值及出入金紀錄會完整保留；日後重新啟用時，"
                "系統會建立新的追蹤區間並標示追蹤斷層。",
                id="performance-cancel-message",
            )
            with Horizontal(id="performance-cancel-buttons"):
                yield Button(
                    "確認取消追蹤",
                    variant="warning",
                    id="performance-cancel-confirm",
                )
                yield Button(
                    "繼續追蹤",
                    variant="primary",
                    id="performance-cancel-back",
                )

    def on_mount(self) -> None:
        self.query_one("#performance-cancel-back").focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "performance-cancel-confirm")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)
        elif event.key in ("left", "right"):
            confirm_btn = self.query_one("#performance-cancel-confirm")
            back_btn = self.query_one("#performance-cancel-back")
            if self.focused == confirm_btn:
                back_btn.focus()
            else:
                confirm_btn.focus()


class PerformanceTrackingScreen(Screen):
    """Cash-flow-adjusted comparison of the full portfolio with QQQ and VT."""

    BINDINGS = [
        Binding("t", "enable_tracking", "啟用追蹤"),
        Binding("d", "disable_tracking", "取消追蹤"),
        Binding("i", "deposit", "入金"),
        Binding("o", "withdrawal", "出金"),
        Binding("r", "refresh_report", "更新比較"),
        Binding("escape", "go_back", "返回"),
        Binding("q", "go_back", "返回", show=False),
    ]

    DEFAULT_CSS = """
    PerformanceTrackingScreen {
        background: #0d1117;
        layout: vertical;
    }
    #performance-copy {
        height: auto;
        margin: 1 2;
        padding: 1 2;
        border: solid #21262d;
        color: #f0f6fc;
    }
    #performance-status {
        height: auto;
        margin: 0 2 1 2;
        color: #8b949e;
    }
    #performance-table {
        height: 1fr;
        margin: 0 2;
        border: solid #21262d;
    }
    """

    def __init__(
        self,
        user: str,
        positions: list[Position],
        cash_positions: list[CashPosition],
        rate: float,
    ) -> None:
        super().__init__()
        self.user = user
        self.positions = positions
        self.cash_positions = cash_positions
        self.rate = rate

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold]對標[/bold]  [dim]完整資產 vs 同步出入金後的 QQQ／VT[/dim]",
            id="performance-copy",
        )
        yield Static("", id="performance-status")
        yield DataTable(id="performance-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#performance-table", DataTable)
        table.add_columns(
            "比較標的",
            "目前等值",
            "累積報酬",
            "與使用者差額",
            "使用者領先／落後",
            "資料日",
        )
        self._render_report()

    def _tracker(self, with_prices: bool = False) -> PortfolioPerformanceTracker:
        return PortfolioPerformanceTracker(
            user=self.user,
            data_dir=get_data_dir(),
            benchmark_prices=YFinanceBenchmarkPrices() if with_prices else None,
        )

    def _render_report(self) -> None:
        tracker = self._tracker()
        state = tracker.state()
        status = self.query_one("#performance-status", Static)
        table = self.query_one("#performance-table", DataTable)
        table.clear(columns=False)
        if not state.enabled:
            if state.enabled_at is not None:
                status.update(
                    "[yellow]績效追蹤已取消；既有紀錄仍保留，持股與現金可直接管理。"
                    "按 [bold]t[/bold] 可重新啟用，新的追蹤區間會標示追蹤斷層。[/yellow]"
                )
            else:
                status.update(
                    "[yellow]績效追蹤尚未啟用。按 [bold]t[/bold] 從目前完整資產建立基準；"
                    "中途啟用會如實標示追蹤斷層。[/yellow]"
                )
            return
        try:
            report = tracker.report()
        except ValueError:
            gap = "｜含追蹤斷層" if state.has_tracking_gap else ""
            status.update(
                f"[green]追蹤已啟用{gap}[/green]｜尚未建立第一筆估值基準，"
                "按 [bold]r[/bold] 更新比較。"
            )
            return

        gap = "｜含追蹤斷層" if state.has_tracking_gap else ""
        status.update(
            f"[green]追蹤中{gap}[/green]｜起始 {report.baseline_at.date()}｜"
            f"使用者現金流調整報酬 {report.portfolio_return_pct:+.2f}%"
        )
        table.add_row(
            "使用者完整資產",
            f"${report.portfolio_value_usd:,.2f}",
            f"{report.portfolio_return_pct:+.2f}%",
            "—",
            "—",
            report.current_at.date().isoformat(),
        )
        for comparison in report.comparisons:
            color = "green" if comparison.performance_gap_pct >= 0 else "red"
            verdict = (
                "擊敗" if comparison.performance_gap_pct >= 0 else "落後"
            )
            table.add_row(
                comparison.symbol,
                f"${comparison.benchmark_value_usd:,.2f}",
                f"{comparison.benchmark_return_pct:+.2f}%",
                f"${comparison.value_gap_usd:+,.2f}",
                f"[{color}]{verdict} {abs(comparison.performance_gap_pct):.2f}%"
                f"[/{color}]",
                comparison.market_date.isoformat(),
            )

    def action_enable_tracking(self) -> None:
        tracker = self._tracker()
        if tracker.state().enabled:
            self.app.notify("績效追蹤已啟用。")
            return
        tracker.enable(new_account=False)
        self.app.notify("已從現在開始追蹤；先前期間會標示為追蹤斷層。")
        self._render_report()
        self._record_current_valuation()

    def action_disable_tracking(self) -> None:
        if not self._tracker().state().enabled:
            self.app.notify("績效追蹤目前未啟用。", severity="warning")
            return
        self.app.push_screen(
            PerformanceTrackingCancelConfirmModal(),
            self._handle_disable_tracking_confirm,
        )

    def _handle_disable_tracking_confirm(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self._tracker().disable()
        self.app.notify("績效追蹤已取消；持股與現金管理限制已解除。")
        self._render_report()

    def action_refresh_report(self) -> None:
        self.app.notify("正在取得 QQQ／VT 收盤價並更新比較…")
        self._record_current_valuation()

    @work(thread=True)
    def _record_current_valuation(self) -> None:
        value = total_asset_value_usd(
            self.positions,
            self.cash_positions,
            self.rate,
        )
        if value is None or value <= 0:
            self.app.call_from_thread(
                self.app.notify,
                "完整資產尚未都有有效報價，無法建立績效快照。",
                severity="warning",
            )
            return
        try:
            self._tracker(with_prices=True).record_valuation(
                total_value_usd=value,
            )
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify,
                f"更新績效失敗：{exc}",
                severity="error",
            )
            return
        self.app.call_from_thread(self._render_report)

    def action_deposit(self) -> None:
        self._open_cash_flow("deposit")

    def action_withdrawal(self) -> None:
        self._open_cash_flow("withdrawal")

    def _open_cash_flow(self, direction: str) -> None:
        if not self._tracker().state().enabled:
            self.app.notify(
                "請先按 t 啟用績效追蹤。",
                severity="warning",
            )
            return
        self.app.push_screen(
            CashFlowModal(direction),
            self._handle_cash_flow_result,
        )

    def _handle_cash_flow_result(self, result: Optional[dict]) -> None:
        if result:
            self.app.notify("正在記錄出入金並同步 QQQ／VT…")
            self._save_declared_cash_flow(result)

    @work(thread=True)
    def _save_declared_cash_flow(self, declaration: dict) -> None:
        positions, cash_positions = load_manual_positions(self.user)
        try:
            positions, cash_positions = _process_declared_cash_flow(
                user=self.user,
                positions=positions,
                cash_positions=cash_positions,
                rate=self.rate,
                declaration=declaration,
            )
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify,
                f"出入金記錄失敗：{exc}",
                severity="error",
            )
            return
        self.positions = positions
        self.cash_positions = cash_positions
        self.app.call_from_thread(
            self.app.notify,
            "出入金已記錄，benchmark 資金流已同步。",
        )
        self.app.call_from_thread(self._render_report)

    def action_go_back(self) -> None:
        self.dismiss()


# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Active ETFs Screen
# ─────────────────────────────────────────────────────────────────────────────
# The ETF universe is discovered daily from Yahoo's ETF screener and persisted
# in data/active_etf_universe.json.  Membership requires AUM > USD 5B and an
# explicit actively-managed description; no ticker membership is embedded here.

# yfinance FundsData.asset_classes keys -> display label. Used to show a fund's
# full stock/bond/cash/preferred/convertible/other split in the holdings panel,
# so it isn't limited to just the top named stock-type positions.
_ASSET_CLASS_LABELS: dict[str, str] = {
    "stockPosition": "股票",
    "bondPosition": "債券",
    "cashPosition": "現金",
    "preferredPosition": "特別股",
    "convertiblePosition": "可轉債",
    "otherPosition": "其他（含衍生性金融商品等）",
}


def _fetch_and_cache_etf_symbols(stale_symbols: list[str]) -> dict:
    """Screen-agnostic core of the ETF holdings/performance/AUM background
    refresh — pure fetch-and-persist, no UI/Screen dependency.

    bug#00061 follow-up: extracted out of ActiveETFsScreen.run_background_fetch
    so AssetTrackApp can also call it directly on a periodic timer, regardless
    of which screen the user currently has open. Previously this logic only
    ran once when ActiveETFsScreen itself was mounted, so a user who stayed
    logged in on the Dashboard (or any other screen) through a day boundary
    would never get that day's real snapshot recorded until they actually
    navigated into 「主動式ETF排行」again.

    `stale_symbols` should already be filtered by the caller via
    storage.etf_symbol_cache_fresh() — kept as a separate call (not done
    inside this function) so a Screen caller can show an early "fetching N
    symbols" status before this potentially slow call runs; the App-level
    timer just filters silently and skips the call entirely when nothing is
    stale.

    Returns {"aums", "perf", "etf_cache", "perf_fail_count"} for a Screen
    caller to merge into its own in-memory state for immediate UI rendering.
    A caller with no UI (the App timer) can ignore the return value — the
    real data is already durably written to etf_cache/*.json and
    etf_cache/history/*.jsonl by the time this returns, and any screen that
    reads it afterward (or the Dashboard's existing 60s refresh) picks it up.
    """
    from concurrent.futures import ThreadPoolExecutor
    import yfinance as _yf
    from .storage import load_etf_symbol_cache, save_etf_symbol_cache
    from .quotes import (
        fetch_active_etf_performance, fetch_etf_holdings,
        fetch_prices_batch, estimate_shares,
    )
    from .ark_holdings import fetch_official_daily_holdings, is_official_daily_source

    if not stale_symbols:
        return {"aums": {}, "perf": {}, "etf_cache": {}, "perf_fail_count": 0}

    # A successful batch can still contain individual symbols with no price.
    # Retry only those symbols so one transient Yahoo omission does not make an
    # otherwise-valid cache look complete for the rest of the day.
    max_attempts = 3
    stale_perf: dict[str, dict] = {}
    perf_pending = list(stale_symbols)
    perf_attempts: dict[str, int] = {sym: 0 for sym in stale_symbols}
    for attempt in range(1, max_attempts + 1):
        if not perf_pending:
            break
        attempt_result = fetch_active_etf_performance(perf_pending)
        next_pending: list[str] = []
        for sym in perf_pending:
            perf_attempts[sym] = attempt
            item = attempt_result.get(sym) or {}
            prior = stale_perf.get(sym) or {}
            stale_perf[sym] = {
                key: value if value is not None else prior.get(key)
                for key, value in {
                    **prior,
                    **item,
                }.items()
            }
            if stale_perf[sym].get("price") is None:
                next_pending.append(sym)
        perf_pending = next_pending
        if perf_pending and attempt < max_attempts:
            time.sleep(0.5 * attempt)

    aums: dict[str, float] = {}
    perf: dict[str, dict] = {}
    etf_cache: dict[str, dict] = {}

    def _fetch_one_etf_details(
        sym: str,
    ) -> tuple[str, float | None, str | None, dict | None, int, list[str]]:
        import time as _time
        aum: float | None = None
        name: str | None = None
        holdings_res: dict | None = None
        problems: list[str] = []
        for attempt in range(1, max_attempts + 1):
            # Add a small delay between requests to prevent Yahoo rate limiting.
            _time.sleep(0.35 if attempt == 1 else 0.5 * (attempt - 1))
            try:
                info = _yf.Ticker(sym).info or {}
                aum_val = info.get("totalAssets") or info.get("marketCap")
                if aum_val:
                    aum = float(aum_val)
                name = (
                    info.get("longName")
                    or info.get("shortName")
                    or name
                    or sym
                )
            except Exception as exc:
                problems.append(f"AUM attempt {attempt}: {type(exc).__name__}")

            # bug#00123: prefer the publisher's own **daily** full-holdings file
            # when one exists (currently ARK). Yahoo's top-10 feed refreshes on
            # each fund's disclosure cadence and never discloses share counts,
            # so it cannot produce a day-over-day trading signal at all. A
            # failed official fetch returns None and we fall through to Yahoo —
            # never to a fabricated portfolio.
            try:
                if is_official_daily_source(sym):
                    official = fetch_official_daily_holdings(sym)
                    if official:
                        holdings_res = official
                        if official.get("aum"):
                            aum = official["aum"]
            except Exception as exc:
                problems.append(
                    f"official holdings attempt {attempt}: {type(exc).__name__}"
                )

            try:
                if not (holdings_res or {}).get("holdings"):
                    candidate = fetch_etf_holdings(sym, aum=aum)
                    if candidate:
                        holdings_res = candidate
                        name = candidate.get("name") or name or sym
            except Exception as exc:
                problems.append(
                    f"holdings attempt {attempt}: {type(exc).__name__}"
                )

            has_portfolio = bool(
                holdings_res
                and (
                    holdings_res.get("holdings")
                    or holdings_res.get("asset_classes")
                )
            )
            has_as_of = bool(
                holdings_res and holdings_res.get("as_of_date")
            )
            missing = []
            if aum is None:
                missing.append("AUM")
            if not has_portfolio:
                missing.append("holdings")
            if not has_as_of:
                missing.append("as_of_date")
            if not missing:
                return sym, aum, name, holdings_res, attempt, problems
            problems.append(
                f"attempt {attempt} missing {', '.join(missing)}"
            )

        return (
            sym,
            aum,
            name or sym,
            holdings_res,
            max_attempts,
            problems,
        )

    # Fetch in parallel with small concurrency to prevent rate limiting
    perf_fail_count = 0
    with ThreadPoolExecutor(max_workers=2) as ex:
        fetched = list(ex.map(_fetch_one_etf_details, stale_symbols))

    # bug#00061 follow-up: batch-fetch the *real* current price for every
    # distinct holding symbol seen across all ETFs refreshed this cycle, in one
    # pass — not per-ETF — since active ETFs' top-10 lists heavily overlap
    # (mega-caps repeat across many funds), keeping total request volume
    # bounded (same rate-limit lesson as bug#00058). This real price replaces
    # estimate_shares()'s old fixed-average-price ($100/$150) assumption, and
    # is what lets compute_symbol_trends() derive a real share-count delta
    # instead of relying on weight% alone (which can't tell a real purchase
    # from a stock simply rallying in price with zero trading).
    holding_symbols: set[str] = set()
    for _, _, _, holdings_res, _, _ in fetched:
        if holdings_res:
            for h in holdings_res.get("holdings", []):
                if h.get("symbol"):
                    holding_symbols.add(h["symbol"])
    price_map = fetch_prices_batch(list(holding_symbols)) if holding_symbols else {}

    for sym, aum, name, holdings_res, detail_attempts, detail_problems in fetched:
        cached = load_etf_symbol_cache(sym)
        prior_holdings = list(cached.get("holdings") or [])
        prior_asset_classes = dict(cached.get("asset_classes") or {})
        prior_holdings_date = cached.get("holdings_as_of_date")
        cached["name"] = name or cached.get("name") or sym
        if aum is not None:
            cached["aum"] = aum
            aums[sym] = aum
        else:
            cached["aum"] = cached.get("aum")

        # Update performance
        p_item = stale_perf.get(sym, {})
        if p_item.get("price") is None:
            perf_fail_count += 1
        for k in ("price", "change_pct", "return_ytd", "return_1y"):
            if p_item.get(k) is not None:
                cached[k] = p_item[k]

        p_constructed = {k: cached[k] for k in ("price", "change_pct", "return_ytd", "return_1y") if k in cached}
        if p_constructed:
            perf[sym] = p_constructed

        # Update holdings + full stock/bond/cash/other asset-class breakdown
        current_holdings_complete = bool(
            holdings_res
            and (
                holdings_res.get("holdings")
                or holdings_res.get("asset_classes")
            )
            and holdings_res.get("as_of_date")
        )
        if current_holdings_complete:
            holdings_list = holdings_res.get("holdings", [])
            for h in holdings_list:
                # bug#00123: only ever *fill in* estimates — never overwrite a
                # real disclosed price/share count with an estimate, and never
                # overwrite a good value with None when the batch quote fetch
                # was throttled (that is how a whole day's snapshot ended up
                # with 0/10 prices). Officially disclosed shares/values are the
                # strongest input the trend engine has; they must survive here.
                if h.get("price") is None:
                    real_price = price_map.get(h.get("symbol"))
                    if real_price is not None:
                        h["price"] = real_price
                if h.get("shares") is None:
                    h["shares"] = estimate_shares(
                        h.get("symbol", ""), h.get("weight", 0.0), aum, h.get("price"),
                    )
                if h.get("value") is None and h.get("shares") and h.get("price"):
                    h["value"] = float(h["shares"]) * float(h["price"])
            cached["holdings"] = holdings_list
            cached["asset_classes"] = holdings_res.get("asset_classes", {})
            cached["category"] = classify_holdings(
                cached["asset_classes"], holdings_list)
            cached["holdings_as_of_date"] = holdings_res.get("as_of_date", "")
            cached["source_type"] = "etf"
            cached["data_status"] = "ok"
            cached["status_message"] = "Yahoo 基金持股與資產配置"

            # 進階分析 (bug#00060): record today's *real* holdings as one
            # dated line in this symbol's history log. This is the only
            # source the trend/consensus report reads from — nothing here
            # is backfilled or estimated for days we didn't actually fetch.
            append_etf_daily_snapshot(
                sym,
                cached["holdings"],
                cached.get("aum"),
                asset_classes=cached.get("asset_classes"),
            )
        else:
            # Never replace a previously valid portfolio with an empty
            # transient response. Mark it retryable so freshness checks cause
            # the next background cycle to try again.
            cached["holdings"] = prior_holdings
            cached["asset_classes"] = prior_asset_classes
            cached["holdings_as_of_date"] = prior_holdings_date
            cached["source_type"] = "etf"
            cached["data_status"] = "retryable"
            cached["status_message"] = (
                "Yahoo 持股更新不完整；保留前次有效資料，稍後自動重試"
                if prior_holdings or prior_asset_classes
                else "Yahoo 持股更新不完整；稍後自動重試"
            )

        missing_fields = []
        if aum is None:
            missing_fields.append("AUM")
        if p_item.get("price") is None:
            missing_fields.append("price")
        if not current_holdings_complete:
            missing_fields.append("holdings")
        cached["fetch_attempts"] = {
            "performance": perf_attempts.get(sym, 0),
            "details": detail_attempts,
        }
        cached["last_fetch_error"] = (
            "; ".join(detail_problems[-3:]) if detail_problems else None
        )
        cached["missing_fields"] = missing_fields
        if missing_fields and cached.get("data_status") == "ok":
            cached["data_status"] = "retryable"
            cached["status_message"] = (
                "Yahoo 更新不完整（"
                + "、".join(missing_fields)
                + "）；稍後自動重試"
            )

        # Automated ETF trade history pipeline (bug#00101): derive & parse
        # trade history from daily snapshot diffs and official trade sources.
        from .etf_trades import update_etf_trade_history
        updated_history = update_etf_trade_history(sym)
        cached["history"] = updated_history

        # Save cache file
        save_etf_symbol_cache(sym, cached)
        etf_cache[sym] = cached

    return {"aums": aums, "perf": perf, "etf_cache": etf_cache, "perf_fail_count": perf_fail_count}


class EtfCacheClearModal(ModalScreen[bool]):
    """清除 ETF 快取前的確認。"""

    DEFAULT_CSS = """
    EtfCacheClearModal { align: center middle; }
    #etf-clear-dialog {
        width: 56;
        height: auto;
        border: thick $warning;
        background: $panel;
        padding: 1 2;
    }
    #etf-clear-buttons { height: auto; align: center middle; margin-top: 1; }
    #etf-clear-buttons Button { margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="etf-clear-dialog"):
            yield Static(
                "[bold]確定清除本機 ETF 快取並重新抓取？[/bold]\n"
                "[dim]會刪除 etf_cache 下的即時快取檔，歷史快照仍保留。[/dim]",
            )
            with Horizontal(id="etf-clear-buttons"):
                yield Button("確認清除", variant="warning", id="confirm")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel").focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)
        elif event.key in ("left", "right"):
            confirm_btn = self.query_one("#confirm")
            cancel_btn = self.query_one("#cancel")
            if self.focused == confirm_btn:
                cancel_btn.focus()
            else:
                confirm_btn.focus()


class EtfWatchlistEditor(ModalScreen[Optional[list]]):
    """第一次進入必須設定；之後按 w 可編輯。完成後才寫檔。"""

    DEFAULT_CSS = """
    EtfWatchlistEditor { align: center middle; }
    #ew-dialog {
        width: 64;
        height: auto;
        max-height: 28;
        border: thick $accent;
        background: $panel;
        padding: 1 2;
    }
    #ew-list { height: auto; max-height: 10; border: solid #30363d; margin: 1 0; }
    #ew-input { margin: 0 0 1 0; border: solid #30363d; }
    #ew-input:focus { border: solid $accent; }
    #ew-error { color: #ff7b72; height: auto; }
    #ew-buttons { height: auto; align: center middle; margin-top: 1; }
    #ew-buttons Button { margin: 0 1; }
    """

    def __init__(
        self,
        current: list[str],
        suggestions: list[str],
        required: bool = False,
    ) -> None:
        super().__init__()
        self.required = required
        self.suggestions = [s for s in suggestions if s]
        self._draft = list(dict.fromkeys(str(s).upper() for s in current if s))

    def compose(self) -> ComposeResult:
        title = "設定觀察清單（第一次使用必填）" if self.required else "編輯觀察清單"
        with Vertical(id="ew-dialog"):
            yield Static(f"[bold]{title}[/bold]")
            yield Static(
                "[dim]之後只顯示這些美股的大型 ETF 買賣與時間。台股代碼無法加入。[/dim]"
            )
            if self.suggestions:
                yield Static(
                    "[dim]可帶入持倉：[/dim] " + "、".join(self.suggestions)
                )
            yield OptionList(id="ew-list")
            yield Input(placeholder="輸入代碼，可用逗號：NVDA, AAPL", id="ew-input")
            yield Static("", id="ew-error")
            with Horizontal(id="ew-buttons"):
                yield Button("加入", variant="primary", id="add")
                if self.suggestions:
                    yield Button("帶入持倉", variant="default", id="seed")
                yield Button("移除選取", variant="default", id="remove")
                yield Button("完成", variant="success", id="done")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        self._refresh_list()
        self.query_one("#ew-input", Input).focus()

    def _refresh_list(self) -> None:
        listing = self.query_one("#ew-list", OptionList)
        listing.clear_options()
        if self._draft:
            listing.add_options([Option(symbol, id=symbol) for symbol in self._draft])
        else:
            listing.add_option(Option("（尚未加入任何標的）", id="__empty__"))

    def _error(self, text: str) -> None:
        self.query_one("#ew-error", Static).update(text)

    def _add_tokens(self, raw: str) -> None:
        added = 0
        rejected: list[str] = []
        for part in raw.replace("，", ",").split(","):
            symbol = normalize_etf_watchlist_symbol(part)
            if symbol is None:
                token = part.strip()
                if token:
                    rejected.append(token.upper())
                continue
            if symbol not in self._draft:
                self._draft.append(symbol)
                added += 1
        self._refresh_list()
        if rejected:
            self._error(f"未加入（非美股或格式不符）：{'、'.join(rejected)}")
        elif added:
            self._error("")
        else:
            self._error("沒有新的標的可加入")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add":
            field = self.query_one("#ew-input", Input)
            self._add_tokens(field.value)
            field.value = ""
        elif event.button.id == "seed":
            self._add_tokens(",".join(self.suggestions))
        elif event.button.id == "remove":
            listing = self.query_one("#ew-list", OptionList)
            highlighted = listing.highlighted
            if highlighted is None:
                self._error("請先在清單中選一檔再移除")
                return
            option = listing.get_option_at_index(highlighted)
            option_id = option.id if option is not None else None
            if option_id and option_id != "__empty__" and option_id in self._draft:
                self._draft.remove(option_id)
                self._refresh_list()
                self._error("")
        elif event.button.id == "done":
            if not self._draft:
                self._error("至少加入一檔美股標的")
                return
            self.dismiss(list(self._draft))
        else:
            self.dismiss(None)

    def on_input_submitted(self, _event) -> None:
        field = self.query_one("#ew-input", Input)
        self._add_tokens(field.value)
        field.value = ""

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


_ETF_HELP_TEXT = """[bold]ETF 觀察 — 畫面說明[/bold]

這頁只盯你[bold]觀察清單[/bold]上的股票：哪些大型主動式 ETF 在買或賣、以及可比較的日期區間。未列入清單的持股不顯示。

[bold yellow]── 建議頁（預設）──[/bold yellow]
• 每一列是觀察清單裡的一檔股票，標出買入／賣出與期間。
• 沒有確認增減持的標的會誠實寫「本視窗無確認增減持」，不會拿別檔股票來填版面。
• 資料新鮮度：幾檔 ETF 在本視窗真的有新的持股狀態。Yahoo 前十大多為月頻；未更新不是「今日無交易」。

[bold yellow]── 基金瀏覽／13F ──[/bold yellow]
• 持股與歷史買賣只保留觀察清單上的股票，並保留日期。
• 13F 是季末申報，滯後約一季，不是盤中成交。

[bold yellow]── 快速鍵 ──[/bold yellow]
[bold]w[/bold] 編輯觀察清單　[bold]j[/bold] 回到建議　[bold]a[/bold] 全市場研究表　[bold]h[/bold] 本說明　[bold]s[/bold] SEC 身分　[bold]c[/bold] 清除快取（需確認）　[bold]Esc[/bold] 返回

[bold yellow]── 紀律──[/bold yellow]
結論只讀本機真實快照，不回填、不臆測。買賣必須「真實股數變化」與「權重變化」同向。

[dim]按 Esc 或 q 返回。[/dim]
"""


class EtfHelpScreen(Screen):
    """主動式 ETF 動態說明。"""

    BINDINGS = [
        Binding("escape", "go_back", "返回"),
        Binding("q",      "go_back", "返回", show=False),
    ]

    DEFAULT_CSS = """
    EtfHelpScreen { background: #0d1117; layout: vertical; }
    #etf-help-body { height: 1fr; padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="etf-help-body"):
            yield Static(_ETF_HELP_TEXT)
        yield Footer()

    def on_mount(self) -> None:
        body = self.query_one("#etf-help-body")
        body.can_focus = True
        body.focus()

    def action_go_back(self) -> None:
        self.dismiss()


class ActiveETFsScreen(_FormulaDrillMixin, Screen):
    """主動式 ETF 動態：預設建議頁，基金瀏覽與 13F 為研究分頁。"""

    BINDINGS = [
        Binding("escape", "go_back", "返回"),
        Binding("j",      "show_advice", "建議"),
        Binding("w",      "edit_watchlist", "觀察清單"),
        Binding("a",      "advanced_analysis", "研究全表"),
        Binding("h",      "show_help", "說明"),
        Binding("c",      "clear_cache", "清除快取"),
        Binding("s",      "sec_identity", "SEC身分"),
        Binding("q",      "go_back", "返回", show=False),
    ]

    DEFAULT_CSS = """
    ActiveETFsScreen {
        background: #0d1117;
        layout: vertical;
    }
    #etf-header {
        height: auto;
        padding: 0 1;
        margin: 1 2 0 2;
    }
    #etf-main-tabs {
        height: 1fr;
        margin: 0 2 1 2;
    }
    #etf-advice-box {
        height: 1fr;
        border: tall #334155;
        background: #0d1117;
    }
    #etf-advice-box:focus-within { border: tall $accent; }
    #etf-analysis-content { height: auto; padding: 0 1; }

    #etf-body, #etf-13f-body {
        height: 1fr;
        layout: horizontal;
    }
    #etf-left-col, #etf-13f-left-col {
        width: 50%;
        height: 1fr;
        layout: vertical;
        margin-right: 1;
    }
    #etf-left-tabbed, #etf-13f-left-panel {
        height: 1fr;
        border: tall #334155;
    }
    #etf-left-tabbed:focus-within, #etf-13f-left-panel:focus-within { border: tall $accent; }
    #etf-us-table, #etf-13f-table {
        height: 1fr;
        border: none;
    }
    #etf-right-col, #etf-13f-right-col {
        width: 50%;
        height: 1fr;
        layout: vertical;
    }
    #etf-holdings-box, #etf-history-box,
    #etf-13f-holdings-box, #etf-13f-history-box {
        height: 1fr;
        layout: vertical;
    }
    #etf-holdings-box, #etf-13f-holdings-box { margin-bottom: 1; }
    #etf-holdings-title, #etf-history-title,
    #etf-13f-holdings-title, #etf-13f-history-title {
        height: 1;
        padding: 0 1;
        color: $accent;
        text-style: bold;
    }
    #etf-holdings-status, #etf-history-status,
    #etf-13f-holdings-status, #etf-13f-history-status {
        height: 1;
        padding: 0 1;
    }
    #etf-holdings-panel, #etf-history-panel,
    #etf-13f-holdings-panel, #etf-13f-history-panel {
        height: 1fr;
        border: tall #334155;
    }
    #etf-holdings-panel:focus-within, #etf-history-panel:focus-within,
    #etf-13f-holdings-panel:focus-within, #etf-13f-history-panel:focus-within {
        border: tall $accent;
    }
    #etf-holdings-table, #etf-history-table,
    #etf-13f-holdings-table, #etf-13f-history-table {
        height: 1fr;
        border: none;
    }
    """

    def __init__(self, user: str, rate: float) -> None:
        super().__init__()
        self.user = user
        self.rate = rate
        self.etf_cache: dict[str, dict] = {}
        self.performance_data: dict = {}
        self.realtime_aums: dict[str, float] = {}
        self.us_symbols: list[str] = []
        self.inst_symbols: list[str] = []
        self.universe_records: dict[str, dict] = {}
        self.selected_symbol: str | None = None
        self.selected_inst: str | None = None
        self._analysis_report: dict | None = None
        self._analysis_tilt: dict | None = None
        self._analysis_bt_consensus: dict | None = None
        self._analysis_bt_tilt: dict | None = None
        self._analysis_min_etfs: int = 4
        self._analysis_loaded: bool = False
        self._held: set[str] = set()
        self._tracked: set[str] = set()
        self._positions: list = []
        self._watchlist: list[str] = []
        self._watchlist_required = False

    # ── Compose ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("", id="etf-header")
        with TabbedContent(id="etf-main-tabs", initial="tab-etf-advice"):
            with TabPane("建議", id="tab-etf-advice"):
                with ScrollableContainer(id="etf-advice-box"):
                    yield Static("", id="etf-analysis-content")
            with TabPane("基金瀏覽", id="tab-etf-browse"):
                with Horizontal(id="etf-body"):
                    with Vertical(id="etf-left-col"):
                        with Container(id="etf-left-tabbed"):
                            yield DataTable(id="etf-us-table")
                    with Vertical(id="etf-right-col"):
                        with Vertical(id="etf-holdings-box"):
                            yield Static("當下持股細節", id="etf-holdings-title")
                            yield Static("", id="etf-holdings-status")
                            with Container(id="etf-holdings-panel"):
                                yield DataTable(id="etf-holdings-table")
                        with Vertical(id="etf-history-box"):
                            yield Static("歷史買賣紀錄", id="etf-history-title")
                            yield Static("", id="etf-history-status")
                            with Container(id="etf-history-panel"):
                                yield DataTable(id="etf-history-table")
            with TabPane("13F 機構", id="tab-etf-13f"):
                with Horizontal(id="etf-13f-body"):
                    with Vertical(id="etf-13f-left-col"):
                        with Container(id="etf-13f-left-panel"):
                            yield DataTable(id="etf-13f-table")
                    with Vertical(id="etf-13f-right-col"):
                        with Vertical(id="etf-13f-holdings-box"):
                            yield Static("當下申報持股", id="etf-13f-holdings-title")
                            yield Static("", id="etf-13f-holdings-status")
                            with Container(id="etf-13f-holdings-panel"):
                                yield DataTable(id="etf-13f-holdings-table")
                        with Vertical(id="etf-13f-history-box"):
                            yield Static("相鄰申報差分", id="etf-13f-history-title")
                            yield Static("", id="etf-13f-history-status")
                            with Container(id="etf-13f-history-panel"):
                                yield DataTable(id="etf-13f-history-table")
        yield Footer()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        from .storage import (
            load_etf_symbol_cache,
            cleanup_old_etf_caches,
        )

        # Setup tables
        us_t = self.query_one("#etf-us-table", DataTable)
        us_t.cursor_type = "row"
        us_t.add_columns("標的／機構", "分類", "AUM／13F市值", "YTD", "1Y", "最大持股")

        h_t = self.query_one("#etf-holdings-table", DataTable)
        h_t.cursor_type = "row"
        h_t.add_columns("Symbol", "名稱", "權重", "股數", "市值")

        tr_t = self.query_one("#etf-history-table", DataTable)
        tr_t.cursor_type = "row"
        tr_t.add_columns("申報／日期區間", "操作", "精確部位", "股數", "金額△／價格", "權重△")

        inst_t = self.query_one("#etf-13f-table", DataTable)
        inst_t.cursor_type = "row"
        inst_t.add_columns("標的／機構", "分類", "AUM／13F市值", "YTD", "1Y", "最大持股")

        ih_t = self.query_one("#etf-13f-holdings-table", DataTable)
        ih_t.cursor_type = "row"
        ih_t.add_columns("Symbol", "名稱", "權重", "股數", "市值")

        itr_t = self.query_one("#etf-13f-history-table", DataTable)
        itr_t.cursor_type = "row"
        itr_t.add_columns("申報／日期區間", "操作", "精確部位", "股數", "金額△／價格", "權重△")

        self._held, self._tracked = user_priority_symbols(self.user)
        try:
            self._positions, _ = load_manual_positions(user=self.user)
        except Exception:
            self._positions = []
        self._watchlist = load_etf_watchlist(self.user)
        if not etf_watchlist_is_configured(self.user):
            self._watchlist_required = True
            self.call_after_refresh(self._open_watchlist_editor, True)

        self._set_header("[dim]載入資料…[/dim]")
        self._set_mid_status("[dim]← 選取左欄 ETF 以查看持股[/dim]", pane="etf")
        self._set_right_status("[dim]← 選取左欄 ETF 以查看歷史[/dim]", pane="etf")
        self._set_mid_status("[dim]← 選取左欄機構以查看申報持股[/dim]", pane="13f")
        self._set_right_status("[dim]← 選取左欄機構以查看申報差分[/dim]", pane="13f")
        self.query_one("#etf-analysis-content", Static).update(
            "[dim]ETF 趨勢計算中…[/dim]"
        )
        advice = self.query_one("#etf-advice-box")
        advice.can_focus = True
        advice.focus()

        # Run per-ETF cache retention cleanup in background (non-blocking).
        # Retention window is the single source of truth in storage
        # (ANALYSIS_CACHE_RETENTION_DAYS = 730, decision D-04).
        cleanup_old_etf_caches()

        # Load whatever is already cached for immediate display
        # Dynamic AUM>5B active-ETF universe plus the four requested 13F filers.
        etf_records = load_active_etf_universe()
        institution_records = hedge_fund_records()
        self.universe_records = {
            item["id"]: item for item in etf_records + institution_records
        }
        all_symbols = [item["symbol"] for item in etf_records]

        # Trim each symbol's real daily-snapshot history log to the retention
        # window (two years, storage.ANALYSIS_CACHE_RETENTION_DAYS)
        # of real snapshots for the walk-forward backtest, still bounded.
        for sym in all_symbols:
            prune_etf_history(sym)

        for sym in all_symbols:
            cached = load_etf_symbol_cache(sym)
            if cached:
                self.etf_cache[sym] = cached
                if cached.get("aum") is not None:
                    self.realtime_aums[sym] = cached["aum"]
                p = {k: cached[k] for k in ("return_ytd", "return_1y", "price", "change_pct") if k in cached}
                if p:
                    self.performance_data[sym] = p
                # bug#00060: if this symbol's cache already holds real holdings
                # fetched today (or a prior real fetch) but its history log doesn't
                # have that date yet — e.g. right after this feature was added —
                # backfill exactly that one already-real snapshot under its own
                # actual holdings_as_of_date. Idempotent; never invents new dates.
                if cached.get("holdings") and cached.get("holdings_as_of_date"):
                    append_etf_daily_snapshot(
                        sym, cached["holdings"], cached.get("aum"),
                        snapshot_date=cached["holdings_as_of_date"],
                        asset_classes=cached.get("asset_classes"),
                    )
                # Derive and populate trade history for immediate rendering
                from .etf_trades import update_etf_trade_history
                cached["history"] = update_etf_trade_history(sym)
                self.etf_cache[sym] = cached

        for record in institution_records:
            entity_id = record["id"]
            cached = load_hedge_fund_cache(entity_id)
            if cached:
                self.etf_cache[entity_id] = cached
                if cached.get("aum") is not None:
                    self.realtime_aums[entity_id] = cached["aum"]

        # Render immediately with whatever cache we have
        self._render_ranking_tables()

        if self.us_symbols:
            self._refresh_detail_panels(self.us_symbols[0], pane="etf")
            try:
                from textual.coordinate import Coordinate
                us_t.cursor_coordinate = Coordinate(0, 0)
            except Exception:
                pass
        if self.inst_symbols:
            self._refresh_detail_panels(self.inst_symbols[0], pane="13f")

        # Launch background fetch (fetches what's missing or stale)
        self.run_background_fetch()

        # bug#00115：背景計算下方 detail 分析框（跨ETF共識＋每日主動選股多空＋回測），
        # 離線讀取已累積的真實快照。算好後 _on_analysis_ready 填入，不阻塞畫面。
        self.run_analysis_compute()

    # ── Detail analysis box (下方內嵌，不分頁) ─────────────────────────────────

    @work(thread=True)
    def run_analysis_compute(self) -> None:
        """離線計算下方 detail 分析框所需的三份輸出（跨ETF共識 report＋其回測、每日
        主動選股 tilt＋其回測），與首頁卡片、進階分析頁共用同一批 analysis 函式，維持
        「結論＝被回測＝同一函式」紀律。純本機、零網路；回測有資料簽章快取，重開頁面
        直接命中不重算。"""
        _ap = _active_params(self.user).get('etf', {})
        _ct = _ap.get('consensus_threshold', 0.5)
        _me = _ap.get('min_etfs_evaluated', 4)
        snaps = {sym: load_etf_daily_snapshots(sym) for sym in active_etf_symbols()}
        report = compute_symbol_trends(
            snaps, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ct)
        bt_consensus = backtest_etf_consensus(
            snaps, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS,
            consensus_threshold=_ct, min_etfs_evaluated=_me)
        tilt = compute_etf_selection_tilt(report)
        bt_tilt = backtest_etf_selection_tilt(
            snaps, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ct)
        try:
            positions, _ = load_manual_positions(user=self.user)
        except Exception:
            positions = []
        held, tracked = user_priority_symbols(self.user)
        self.app.call_from_thread(
            self._on_analysis_ready, report, bt_consensus, tilt, bt_tilt, _me,
            positions, held, tracked,
        )

    def _on_analysis_ready(
        self, report, bt_consensus, tilt, bt_tilt, min_etfs, positions, held, tracked,
    ) -> None:
        self._analysis_report = report
        self._analysis_bt_consensus = bt_consensus
        self._analysis_tilt = tilt
        self._analysis_bt_tilt = bt_tilt
        self._analysis_min_etfs = min_etfs
        self._positions = positions or []
        self._held = set(held or [])
        self._tracked = set(tracked or [])
        self._analysis_loaded = True
        self._render_analysis()

    def _render_analysis(self, etf_symbol: "str | None" = None) -> None:
        """建議頁：傾向、新鮮度、與你相關、其他共識。研究分頁的單檔明細不在這裡。"""
        try:
            target = self.query_one("#etf-analysis-content", Static)
        except Exception:
            return
        if not self._analysis_loaded or self._analysis_report is None:
            target.update("[dim]ETF 趨勢計算中…[/dim]")
            return
        markup, mapping = render_etf_advice_view(
            self._analysis_report,
            self._analysis_tilt or {},
            positions=self._positions,
            watchlist=self._watchlist,
            held=self._held,
            tracked=self._tracked,
            backtest=self._analysis_bt_consensus,
            tilt_backtest=self._analysis_bt_tilt,
            min_etfs_evaluated=self._analysis_min_etfs,
        )
        self._recs_by_id = mapping
        target.update(markup)

    def _set_header(self, status: str) -> None:
        from rich.panel import Panel as _Panel
        self.query_one("#etf-header", Static).update(
            _chrome_header("ETF 觀察", status)
        )

    def _pane_ids(self, pane: str = "etf") -> dict[str, str]:
        if pane == "13f":
            return {
                "holdings_title": "#etf-13f-holdings-title",
                "holdings_status": "#etf-13f-holdings-status",
                "holdings_table": "#etf-13f-holdings-table",
                "history_title": "#etf-13f-history-title",
                "history_status": "#etf-13f-history-status",
                "history_table": "#etf-13f-history-table",
            }
        return {
            "holdings_title": "#etf-holdings-title",
            "holdings_status": "#etf-holdings-status",
            "holdings_table": "#etf-holdings-table",
            "history_title": "#etf-history-title",
            "history_status": "#etf-history-status",
            "history_table": "#etf-history-table",
        }

    def _set_mid_title(self, text: str, pane: str = "etf") -> None:
        self.query_one(self._pane_ids(pane)["holdings_title"], Static).update(text)

    def _set_right_title(self, text: str, pane: str = "etf") -> None:
        self.query_one(self._pane_ids(pane)["history_title"], Static).update(text)

    def _set_mid_status(self, text: str, pane: str = "etf") -> None:
        self.query_one(self._pane_ids(pane)["holdings_status"], Static).update(text)

    def _set_right_status(self, text: str, pane: str = "etf") -> None:
        self.query_one(self._pane_ids(pane)["history_status"], Static).update(text)

    def action_sec_identity(self) -> None:
        """Open the account-scoped SEC identity/privacy controls."""
        self.app.push_screen(
            SECIdentityModal(self.user),
            self._on_sec_identity_changed,
        )

    def _on_sec_identity_changed(self, changed: bool) -> None:
        if not changed:
            return
        summary = masked_sec_identity(self.user)
        self._set_header(
            (
                f"[green]SEC 身分已儲存：{summary}；正在重新核對 13F[/green]"
                if summary
                else "[yellow]SEC 身分已刪除；13F 自動更新已停止[/yellow]"
            )
        )
        self.run_background_fetch()

    # ── Keyboard Navigation ───────────────────────────────────────────────────

    def on_key(self, event) -> None:
        from textual.widgets import Tabs
        
        if event.key == "right":
            focused = self.focused
            if isinstance(focused, DataTable):
                nxt = {
                    "etf-us-table": "#etf-holdings-table",
                    "etf-holdings-table": "#etf-history-table",
                    "etf-13f-table": "#etf-13f-holdings-table",
                    "etf-13f-holdings-table": "#etf-13f-history-table",
                }.get(focused.id)
                if nxt:
                    self.query_one(nxt, DataTable).focus()
                    event.prevent_default()
                    event.stop()
        elif event.key == "left":
            focused = self.focused
            if isinstance(focused, DataTable):
                nxt = {
                    "etf-holdings-table": "#etf-us-table",
                    "etf-history-table": "#etf-holdings-table",
                    "etf-13f-holdings-table": "#etf-13f-table",
                    "etf-13f-history-table": "#etf-13f-holdings-table",
                }.get(focused.id)
                if nxt:
                    self.query_one(nxt, DataTable).focus()
                    event.prevent_default()
                    event.stop()
        elif event.key == "up":
            focused = self.focused
            if isinstance(focused, DataTable) and focused.id in ("etf-us-table", "etf-13f-table"):
                if focused.cursor_row == 0:
                    try:
                        self.query_one(Tabs).focus()
                        event.prevent_default()
                        event.stop()
                    except Exception:
                        pass
        elif event.key == "down":
            focused = self.focused
            if isinstance(focused, Tabs):
                try:
                    tabs = self.query_one("#etf-main-tabs", TabbedContent)
                    target = (
                        "#etf-13f-table" if tabs.active == "tab-etf-13f"
                        else "#etf-us-table" if tabs.active == "tab-etf-browse"
                        else "#etf-advice-box"
                    )
                    widget = self.query_one(target)
                    if hasattr(widget, "focus"):
                        widget.focus()
                    event.prevent_default()
                    event.stop()
                except Exception:
                    pass

    # ── Background worker ──────────────────────────────────────────────────────

    @work(thread=True)
    def run_background_fetch(self) -> None:
        """Parallel fetch of AUM, performance, and holdings for all ETFs. Thin
        screen-side wrapper around the shared, screen-agnostic
        _fetch_and_cache_etf_symbols() — see that function's docstring
        (bug#00061 follow-up) for why the actual fetch logic lives there
        instead of here: AssetTrackApp also calls it directly on a periodic
        timer so real snapshots keep accumulating even while the user stays
        on a different screen across a day boundary."""
        from .storage import etf_symbol_cache_fresh

        universe_result = ensure_active_etf_universe()
        all_symbols = [
            item["symbol"] for item in universe_result.get("records", [])
        ]

        # ── 1. Identify stale symbols ─────────────────────────────────────────
        stale_symbols = [sym for sym in all_symbols if not etf_symbol_cache_fresh(sym)]

        self.app.call_from_thread(
            self._set_header,
            (
                f"更新 {len(stale_symbols)} 檔 ETF · 核對 13F…"
                if stale_symbols else "ETF 已是今日 · 核對 13F…"
            ),
        )

        result = _fetch_and_cache_etf_symbols(stale_symbols)
        institutions = ensure_hedge_fund_filings(self.user)

        aums = dict(self.realtime_aums)
        aums.update(result["aums"])
        perf = dict(self.performance_data)
        perf.update(result["perf"])
        etf_cache = dict(self.etf_cache)
        etf_cache.update(result["etf_cache"])
        etf_cache.update(institutions)
        for entity_id, cached in institutions.items():
            if cached.get("aum") is not None:
                aums[entity_id] = cached["aum"]

        self.app.call_from_thread(
            self._on_fetch_complete, aums, perf, etf_cache,
            result["perf_fail_count"], len(stale_symbols),
            universe_result, institutions,
        )

    def _on_fetch_complete(
        self,
        aums: dict[str, float],
        perf: dict[str, dict],
        etf_cache: dict[str, dict],
        perf_fail_count: int = 0,
        perf_attempted_count: int = 0,
        universe_result: dict | None = None,
        institutions: dict | None = None,
    ) -> None:
        self.realtime_aums = aums
        self.performance_data = perf
        self.etf_cache = etf_cache
        etf_records = (
            (universe_result or {}).get("records")
            or load_active_etf_universe()
        )
        institution_records = hedge_fund_records()
        self.universe_records = {
            item["id"]: item for item in etf_records + institution_records
        }
        if perf_fail_count > 0:
            # bug#00058: surface partial performance-fetch failures instead of
            # silently showing "—" with no explanation of why.
            self._set_header(
                f"[yellow]即時數據載入完成，但 {perf_fail_count}/{perf_attempted_count} 檔 ETF 績效抓取失敗"
                f"（將於下次刷新自動重試）[/yellow]"
            )
        else:
            stale_13f = sum(
                1 for item in (institutions or {}).values()
                if item.get("data_status") != "ok"
            )
            universe_note = (
                "；ETF universe 更新失敗，使用前次真實快取"
                if (universe_result or {}).get("status") in ("stale", "error")
                else ""
            )
            institution_note = (
                f"；{stale_13f} 家 13F 更新失敗，保留前次申報"
                if stale_13f else "；4 家 13F 已核對"
            )
            self._set_header(
                f"[green]即時數據載入完成[/green]"
                f"[dim]{universe_note}{institution_note}[/dim]"
            )
        self._render_ranking_tables()
        sym = self.selected_symbol or (self.us_symbols[0] if self.us_symbols else None)
        if sym:
            self._refresh_detail_panels(sym, pane="etf")
        inst = self.selected_inst or (self.inst_symbols[0] if self.inst_symbols else None)
        if inst:
            self._refresh_detail_panels(inst, pane="13f")

    # ── Render ranking tables (left col) ──────────────────────────────────────

    def _record_ids(self, source_type: str) -> list[str]:
        return [
            item_id for item_id, record in self.universe_records.items()
            if record.get("source_type", "etf") == source_type
        ]

    def _render_ranking_tables(self) -> None:
        new_us: list[str] = []
        self._render_one_tab("#etf-us-table", self._record_ids("etf"), new_us)
        self.us_symbols = new_us
        new_inst: list[str] = []
        self._render_one_tab("#etf-13f-table", self._record_ids("13f"), new_inst)
        self.inst_symbols = new_inst

    def _render_one_tab(
        self,
        selector: str,
        universe: list[str],
        out_symbols: list[str],
    ) -> None:
        table = self.query_one(selector, DataTable)
        table.clear(columns=False)

        # Group by actual holdings composition, then sort each group by AUM /
        # reported 13F market value descending.
        ordered = sorted(
            universe,
            key=lambda item_id: (
                self.etf_cache.get(item_id, {}).get("category")
                or self.universe_records.get(item_id, {}).get("category", "未分類"),
                -float(self.realtime_aums.get(item_id) or 0.0),
                item_id,
            ),
        )

        for symbol in ordered:
            record = self.universe_records.get(symbol, {})
            source_type = record.get("source_type", "etf")
            is_tw = symbol.endswith(".TW") or symbol.endswith(".TWO")
            aum_s = self._fmt_aum(self.realtime_aums.get(symbol), is_tw=is_tw)
            p = self.performance_data.get(symbol, {})
            holdings = self.etf_cache.get(symbol, {}).get("holdings", [])
            category = (
                self.etf_cache.get(symbol, {}).get("category")
                or record.get("category")
                or classify_holdings(
                    self.etf_cache.get(symbol, {}).get("asset_classes"), holdings)
            )
            if source_type == "13f":
                name = record.get("name") or self.etf_cache.get(symbol, {}).get("name") or symbol
                sym_display = f"[bold yellow]◆ {name}[/bold yellow]"
            else:
                sym_display = f"[bold white]{symbol}[/bold white]"

            if holdings:
                top_h = max(holdings, key=lambda h: h.get("weight", 0.0))
                w = top_h.get("weight", 0.0)
                top_name = (
                    top_h.get("issuer") or top_h.get("name")
                    if source_type == "13f" else top_h.get("symbol")
                )
                top_h_s = f"[dim]{top_name or '—'} ({w:.1f}%)[/dim]"
            else:
                top_h_s = "[dim]—[/dim]"

            table.add_row(
                sym_display,
                f"[cyan]{category}[/cyan]",
                f"[dim]{aum_s}[/dim]",
                "[dim]季報[/dim]" if source_type == "13f" else self._fmt_pct(p.get("return_ytd")),
                "[dim]季報[/dim]" if source_type == "13f" else self._fmt_pct(p.get("return_1y")),
                top_h_s,
            )
            out_symbols.append(symbol)

    # ── Detail panels (middle + right cols) ───────────────────────────────────

    def _refresh_detail_panels(self, symbol: str, pane: str = "etf") -> None:
        if pane == "13f":
            self.selected_inst = symbol
        else:
            self.selected_symbol = symbol
        cached = self.etf_cache.get(symbol, {})
        fund_name = cached.get("name") or symbol
        source_type = cached.get("source_type", "etf")
        label = fund_name if source_type == "13f" else symbol
        holdings_label = "當下申報持股" if pane == "13f" else "當下持股細節"
        history_label = "相鄰申報差分" if pane == "13f" else "歷史買賣紀錄"
        self._set_mid_title(
            f"[bold]{label}[/bold]  [dim]{fund_name}[/dim]  {holdings_label}",
            pane=pane,
        )
        self._set_right_title(f"[bold]{label}[/bold]  {history_label}", pane=pane)
        self._set_mid_status(f"[dim]載入 {symbol} 持股…[/dim]", pane=pane)
        self._set_right_status(f"[dim]載入 {symbol} 歷史…[/dim]", pane=pane)
        self._render_holdings(symbol, pane=pane)
        self._render_history(symbol, pane=pane)

    def _render_holdings(self, symbol: str, pane: str = "etf") -> None:
        table = self.query_one(self._pane_ids(pane)["holdings_table"], DataTable)
        table.clear(columns=False)

        info = self.etf_cache.get(symbol, {})
        holdings = info.get("holdings", [])
        asset_classes = info.get("asset_classes") or {}
        as_of = info.get("holdings_as_of_date", "")
        source_type = info.get("source_type", "etf")
        watch = {item.upper() for item in self._watchlist}
        name_index = None
        if watch:
            name_index = build_ticker_name_index({
                key: [{"date": "x", "holdings": cached.get("holdings") or []}]
                for key, cached in self.etf_cache.items()
                if cached.get("source_type", "etf") != "13f"
            })
            holdings = [
                row for row in holdings
                if holding_on_watchlist(row, watch, name_index)
            ]
            asset_classes = {}

        if not holdings and not asset_classes:
            if watch:
                self._set_mid_status(
                    f"[dim]{symbol} 沒有觀察清單上的持股。按 w 編輯清單。[/dim]",
                    pane=pane,
                )
                return
            diagnostic = info.get("last_fetch_error")
            self._set_mid_status(
                f"[dim]{symbol} 持股資料更新中；狀態："
                f"{info.get('status_message') or '資料來源尚未回傳'}"
                f"{f'（{diagnostic}）' if diagnostic else ''}[/dim]",
                pane=pane,
            )
            return

        if source_type == "13f":
            filing_date = info.get("filing_date") or "—"
            option_count = sum(
                1 for item in holdings if item.get("instrument_type") == "option")
            if info.get("data_status") == "ok":
                source_badge = (
                    f"[green]SEC 13F 報告期: {as_of or '—'}；"
                    f"申報日: {filing_date}[/green]"
                )
            else:
                source_badge = (
                    f"[yellow]⚠ SEC 更新未完成，顯示前次申報 "
                    f"{as_of or '—'}；{info.get('status_message') or '稍後重試'}"
                    "[/yellow]"
                )
            date_badge = (
                source_badge
                + f"  [yellow]季報、非即時；{option_count} 筆 Put/Call 不含履約價與到期日，"
                "不納入期權時間區間推論[/yellow]"
            )
        else:
            date_badge = (
                f"[green]Yahoo 持股快照: {as_of}[/green]"
                if as_of else "[dim]Yahoo 持股快照日期: 未知[/dim]"
            )
        if watch:
            date_badge += f"  [dim]只顯示觀察清單 {len(holdings)} 檔[/dim]"
        self._set_mid_status(date_badge, pane=pane)

        aum = info.get("aum")
        is_tw = symbol.endswith(".TW") or symbol.endswith(".TWO")

        # Section 1: whole-fund stock/bond/cash/preferred/convertible/other split.
        # `holdings` below is only Yahoo's curated top-N *named* positions (mostly
        # equities); a fund with a meaningful cash/bond/options-overlay sleeve would
        # otherwise look 100% stock-only. This surfaces the real full composition.
        if asset_classes:
            table.add_row("[bold]▾ 資產配置[/bold]", "[dim]整體基金比例[/dim]", "", "", "")
            for key, label in _ASSET_CLASS_LABELS.items():
                w = asset_classes.get(key)
                if w is None:
                    continue
                mv_s = "—"
                if aum and w:
                    mv_s = self._fmt_aum(aum * (w / 100.0), is_tw=is_tw)
                table.add_row(
                    f"[bold yellow]{label}[/bold yellow]",
                    "[dim]資產類別佔比[/dim]",
                    f"{w:.2f}%",
                    "—",
                    mv_s,
                )

        # Section 2: named top holdings (individual positions within the fund)
        if holdings:
            if asset_classes:
                table.add_row("[bold]▾ 前十大持股[/bold]", "[dim]個股持有明細[/dim]", "", "", "")
            visible_holdings = holdings[:100] if source_type == "13f" else holdings
            for h in visible_holdings:
                w = h.get("weight")
                s = h.get("shares")
                mv_s = "—"
                if h.get("value") is not None:
                    mv_s = self._fmt_aum(float(h["value"]))
                elif aum and w:
                    mv_s = self._fmt_aum(aum * (w / 100.0), is_tw=is_tw)
                position_code = holding_display_symbol(h, name_index)
                position_name = h.get("name", "—")
                if h.get("instrument_type") == "option":
                    position_name = (
                        f"{h.get('option_type') or 'OPTION'} {h.get('issuer') or position_name} "
                        "（13F未揭露到期日/履約價）"
                    )
                table.add_row(
                    f"[bold white]{position_code}[/bold white]",
                    f"[dim]{position_name}[/dim]",
                    f"{w:.2f}%" if w is not None else "—",
                    f"{int(s):,}" if s is not None else "—",
                    mv_s,
                )
            if source_type == "13f" and len(holdings) > len(visible_holdings):
                self._set_mid_status(
                    f"{date_badge}  [dim]畫面顯示市值前 {len(visible_holdings)}/{len(holdings)} 筆；"
                    "完整申報仍保存在快取與分析資料中[/dim]",
                    pane=pane,
                )

    def _render_history(self, symbol: str, pane: str = "etf") -> None:
        table = self.query_one(self._pane_ids(pane)["history_table"], DataTable)
        table.clear(columns=False)

        history = self.etf_cache.get(symbol, {}).get("history", [])
        watch = {item.upper() for item in self._watchlist}
        name_index = None
        if watch:
            name_index = build_ticker_name_index({
                key: [{"date": "x", "holdings": cached.get("holdings") or []}]
                for key, cached in self.etf_cache.items()
                if cached.get("source_type", "etf") != "13f"
            })
            history = [
                row for row in history
                if holding_on_watchlist(row, watch, name_index)
            ]
        if history:
            source_type = self.etf_cache.get(symbol, {}).get("source_type", "etf")
            self._set_right_status(
                f"[green]{symbol} 歷史部位變化 ({len(history)} 筆)[/green]"
                + (
                    " [yellow]由相鄰 13F 報告期差分；不是季內成交紀錄[/yellow]"
                    if source_type == "13f" else
                    " [dim]由相鄰真實持股快照差分[/dim]"
                ),
                pane=pane,
            )
            for h in history[:250]:
                action = h.get("action", "—")
                a_col = "green" if action == "BUY" else "red"
                wc = h.get("weight_change")
                wc_s = "—"
                if wc is not None:
                    col = "green" if wc >= 0 else "red"
                    sign = "+" if wc >= 0 else ""
                    wc_s = f"[{col}]{sign}{wc:.2f}%[/{col}]"
                price = h.get("price")
                value_change = h.get("value_change")
                shares = h.get("shares")
                period = (
                    f"{h.get('period_start')}→{h.get('period_end')}"
                    if h.get("period_start") else h.get("date", "—")
                )
                ticker = holding_display_symbol(h, name_index)
                name = h.get("name") or h.get("issuer") or ""
                if name and ticker not in (name, "—"):
                    position_label = f"{ticker} {name}"
                else:
                    position_label = ticker if ticker != "—" else (name or "—")
                value_or_price = (
                    self._fmt_aum(abs(float(value_change)))
                    if value_change is not None else
                    f"${price:,.2f}" if price is not None else "—"
                )
                table.add_row(
                    period,
                    f"[{a_col}]{action}[/{a_col}]",
                    f"[bold white]{position_label}[/bold white]",
                    f"{int(shares):,}" if shares is not None else "—",
                    value_or_price,
                    wc_s,
                )
        else:
            self._set_right_status(
                (
                    f"[dim]{symbol} 沒有觀察清單標的的買賣紀錄。按 w 編輯清單。[/dim]"
                    if watch else
                    f"[dim]{symbol} 尚無可比較的歷史部位（至少需要兩個真實快照／兩季 13F）。"
                    "系統會在背景更新後自動產生差分。[/dim]"
                ),
                pane=pane,
            )

    # ── Unified row navigation ─────────────────────────────────────────────────

    def _handle_row(self, table_id: str, row_idx: int) -> None:
        if table_id == "etf-us-table" and 0 <= row_idx < len(self.us_symbols):
            self._refresh_detail_panels(self.us_symbols[row_idx], pane="etf")
        elif table_id == "etf-13f-table" and 0 <= row_idx < len(self.inst_symbols):
            self._refresh_detail_panels(self.inst_symbols[row_idx], pane="13f")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._handle_row(event.data_table.id, event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._handle_row(event.data_table.id, event.cursor_row)

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        self._handle_row(event.data_table.id, event.coordinate.row)

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        self._handle_row(event.data_table.id, event.coordinate.row)

    # ── Static formatting ──────────────────────────────────────────────────────

    @staticmethod
    def _fmt_aum(val: float | None, is_tw: bool = False) -> str:
        if val is None:
            return "—"
        prefix = "NT$" if is_tw else "$"
        if val >= 1e12:
            return f"{prefix}{val / 1e12:.2f}T"
        if val >= 1e9:
            return f"{prefix}{val / 1e9:.1f}B"
        if val >= 1e6:
            return f"{prefix}{val / 1e6:.1f}M"
        return f"{prefix}{val:,.0f}"

    @staticmethod
    def _fmt_pct(val: float | None) -> str:
        if val is None:
            return "—"
        color = "green" if val >= 0 else "red"
        sign  = "+" if val >= 0 else ""
        return f"[{color}]{sign}{val:.2f}%[/{color}]"

    def action_go_back(self) -> None:
        self.dismiss()

    def action_clear_cache(self) -> None:
        """清除快取前先確認，避免誤按清空即時快取。"""
        self.app.push_screen(EtfCacheClearModal(), self._on_clear_cache_confirmed)

    def _on_clear_cache_confirmed(self, confirmed: bool) -> None:
        if not confirmed:
            return
        from .storage import get_etf_cache_dir
        cache_dir = get_etf_cache_dir()
        if cache_dir.exists():
            for f in cache_dir.glob("*.json"):
                try:
                    f.unlink()
                except Exception:
                    pass
        self.etf_cache.clear()
        self.realtime_aums.clear()
        self.performance_data.clear()
        self.us_symbols.clear()
        self.inst_symbols.clear()
        self.selected_symbol = None
        self.selected_inst = None

        self.query_one("#etf-us-table", DataTable).clear(columns=False)
        self.query_one("#etf-holdings-table", DataTable).clear(columns=False)
        self.query_one("#etf-history-table", DataTable).clear(columns=False)
        self.query_one("#etf-13f-table", DataTable).clear(columns=False)
        self.query_one("#etf-13f-holdings-table", DataTable).clear(columns=False)
        self.query_one("#etf-13f-history-table", DataTable).clear(columns=False)

        self._set_header("[dim]快取已清 · 重抓中…[/dim]")
        self._set_mid_status("[dim]← 快取已清除，等待重新載入[/dim]", pane="etf")
        self._set_right_status("[dim]← 快取已清除，等待重新載入[/dim]", pane="etf")
        self._set_mid_status("[dim]← 快取已清除，等待重新載入[/dim]", pane="13f")
        self._set_right_status("[dim]← 快取已清除，等待重新載入[/dim]", pane="13f")
        self.run_background_fetch()

    def action_show_advice(self) -> None:
        """[j] 回到建議頁。"""
        tabs = self.query_one("#etf-main-tabs", TabbedContent)
        tabs.active = "tab-etf-advice"
        box = self.query_one("#etf-advice-box")
        box.can_focus = True
        box.focus()

    def action_show_help(self) -> None:
        self.app.push_screen(EtfHelpScreen())

    def action_edit_watchlist(self) -> None:
        self._open_watchlist_editor(required=False)

    def _open_watchlist_editor(self, required: bool = False) -> None:
        self._watchlist_required = required
        self.app.push_screen(
            EtfWatchlistEditor(
                current=self._watchlist,
                suggestions=suggested_etf_watchlist(self._positions),
                required=required,
            ),
            self._on_watchlist_edited,
        )

    def _on_watchlist_edited(self, tickers: Optional[list]) -> None:
        if tickers is None:
            if self._watchlist_required and not etf_watchlist_is_configured(self.user):
                self.dismiss()
            return
        save_etf_watchlist(self.user, tickers)
        self._watchlist = list(tickers)
        self._watchlist_required = False
        if self._analysis_loaded:
            self._render_analysis()
        if self.selected_symbol:
            self._refresh_detail_panels(self.selected_symbol, pane="etf")
        if self.selected_inst:
            self._refresh_detail_panels(self.selected_inst, pane="13f")
        self._set_header(
            f"[green]觀察清單 {len(self._watchlist)} 檔：{'、'.join(self._watchlist)}[/green]"
        )

    def action_advanced_analysis(self) -> None:
        """[a] 全市場研究表：持有／追蹤置頂的部位明細，結論在建議頁。"""
        self.app.push_screen(AdvancedAnalysisScreen(self.user))


# ─────────────────────────────────────────────────────────────────────────────
# Advanced Analysis Screen (進階分析)
# ─────────────────────────────────────────────────────────────────────────────
# bug#00060 / bug#00104 / bug#00122: 100% 離線運算 —— 只讀取 storage.py
# 背景刷新所保存的真實 per-ETF 快照（etf_cache/history/*.jsonl）。14 天視窗內
# 必須有至少 2 個內容不同的持股狀態才納入計算；不同日期抓到完全相同的 Yahoo
# top-holdings 只算一個狀態，避免把資料源未更新誤認為基金有新的交易資訊。

ADVANCED_ANALYSIS_WINDOW_DAYS = 14

# bug#00123: how many non-priority 13F rows to render. Four filers disclose
# ~16k distinct securities; the table exists to be read, so everything the user
# actually holds or tracks is always shown and the remainder is capped by scale.
INSTITUTION_OTHER_ROWS_CAP = 40


def user_priority_symbols(user: str) -> tuple[set[str], set[str]]:
    """(持有部位代碼, 追蹤類股成分代碼) for display prioritisation.

    Tracked symbols exclude ones already held so the two tiers stay disjoint.
    Options contribute their underlying — the user holding a TSLA call means
    TSLA is a symbol they care about.
    """
    from .storage import load_manual_positions, load_sector_groups
    held: set[str] = set()
    try:
        positions, _ = load_manual_positions(user=user)
    except Exception:
        positions = []
    for position in positions:
        symbol = (
            position.underlying
            if getattr(position, "instrument_type", None) == "option" and position.underlying
            else position.symbol
        )
        if symbol:
            held.add(str(symbol).upper())

    tracked: set[str] = set()
    try:
        groups = load_sector_groups(user)
    except Exception:
        groups = {}
    for members in (groups or {}).values():
        for member in members or []:
            if member:
                tracked.add(str(member).upper())
    return held, tracked - held


def position_display_sort_key(
    key: str,
    info: dict,
    held: set[str],
    tracked: set[str],
    name_index: Optional[dict] = None,
) -> tuple:
    """Sort key implementing the user's requested ordering (bug#00123).

    Tier 0 = a position the user holds, tier 1 = a member of a sector group they
    track, tier 2 = everything else. Inside a tier the previous ranking is kept:
    positions carrying a real directional signal first, then by absolute net
    exposure change, then alphabetically — so a tier never buries its own most
    material row.
    """
    from .analysis import resolve_position_ticker
    position = dict(info.get("position") or {})
    position.setdefault("symbol", key)
    ticker = resolve_position_ticker({**position, "symbol": key}, name_index or {})
    if ticker and ticker in held:
        tier = 0
    elif ticker and ticker in tracked:
        tier = 1
    else:
        tier = 2
    has_signal = info.get("consensus") in ("up", "down")
    # Rank by the same number the row actually displays, so the ordering never
    # looks arbitrary next to the money column.
    scale = info.get("confirmed_net_trade_value")
    if scale is None:
        scale = info.get("confirmed_net_value_delta")
    if scale is None:
        scale = info.get("net_value_delta")
    return (
        tier,
        not has_signal,
        -abs(scale or 0.0),
        (ticker or position.get("issuer") or position.get("name") or key or "").upper(),
    )


class AdvancedAnalysisScreen(_FormulaDrillMixin, Screen):
    """跨主動式ETF持股趨勢共識報告 —— 純本機離線運算，無網路請求。"""

    def __init__(self, user: str = "default") -> None:
        super().__init__()
        self.user = user

    BINDINGS = [
        Binding("escape", "go_back", "返回"),
        Binding("q",      "go_back", "返回", show=False),
    ]

    DEFAULT_CSS = """
    AdvancedAnalysisScreen {
        background: #0d1117;
        layout: vertical;
    }
    #aa-header {
        height: auto;
        padding: 0 1;
        margin: 1 2 0 2;
    }
    #aa-conclusions {
        height: auto;
        padding: 0 1;
        margin: 1 2 0 2;
        border: solid #21262d;
    }
    #aa-body {
        height: 1fr;
        margin: 1 2;
        border: tall #334155;
    }
    #aa-body:focus-within { border: tall $accent; }
    #aa-table { height: 1fr; border: none; }
    #aa-empty { padding: 2 3; height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="aa-header")
        yield Static("", id="aa-conclusions")
        with Container(id="aa-body"):
            yield DataTable(id="aa-table")
            yield Static("", id="aa-empty")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#aa-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "精確部位", "資料來源", "資料期別／交易期間", "配置權重",
            "增減持方", "買入總額", "賣出總額", "淨部位變化", "多空判斷",
        )
        table.display = False
        self.query_one("#aa-empty", Static).display = False

        self._run_analysis()

    def _run_analysis(self) -> None:
        # 投資建議一律以美股為主（bug#00091）：跨ETF趨勢共識只納入美股主動式 ETF。
        # bug#00095 接線：套用已確認校準參數。
        _ap = _active_params(self.user).get('etf', {})
        _ct = _ap.get('consensus_threshold', 0.5)
        _me = _ap.get('min_etfs_evaluated', 4)
        all_symbols = active_etf_symbols()
        snapshots_by_etf = {sym: load_etf_daily_snapshots(sym) for sym in all_symbols}

        report = compute_symbol_trends(snapshots_by_etf, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ct)
        # bug#00092: walk-forward 回測（與結論卡共用同一套邏輯）。
        _bt = backtest_etf_consensus(snapshots_by_etf, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ct, min_etfs_evaluated=_me)
        # The recommendation engine uses only qualified directional signals,
        # while the detail table retains every observed position. A flat row is
        # valuable period-over-period state, not a recommendation.
        # bug#00123: 使用者要求的分層排序 —— 持有部位 → 追蹤類股 → 其他（字母）。
        # 13F 部位以 CUSIP/發行人名稱識別，沒有 ticker，因此用本機 ETF 快照（同時含
        # ticker 與公司名）離線建立名稱索引來解析；解析不出來就維持發行人名稱、歸第三層，
        # 不臆造代碼。
        self._held, self._tracked = user_priority_symbols(self.user)
        self._name_index = build_ticker_name_index(snapshots_by_etf)
        position_rows = sorted(
            (report.get("symbols") or {}).items(),
            key=lambda item: position_display_sort_key(
                item[0], item[1], self._held, self._tracked, self._name_index,
            ),
        )
        tilt = compute_etf_selection_tilt(report)

        # bug#00123（A）：SEC 13F 逐季申報是本機唯一含「真實申報股數與市值」的來源，
        # 因此獨立以季度環比計算，補上 Yahoo 每日 top-10 無法提供的方向訊號。
        inst_report = self._institution_report()

        freshness = report.get("source_freshness") or {}
        coverage_line = (
            f"[bold]進階分析[/bold]  [dim]│[/dim]  "
            f"視窗 {report['window_days']} 天　"
            f"期間可比較：{report['etfs_comparable_count']}/{report['etfs_total_count']} 檔　"
            f"方向訊號就緒：{report['etfs_ready_count']}/{report['etfs_total_count']} 檔"
            f"（≥2 個不同持股狀態）　更新於 {report['as_of']}"
        )
        # bug#00123（C）：來源停滯是事實，必須說出來，而不是讓整張表印成「持平／$0」。
        if freshness.get("sources_unchanged"):
            since = freshness.get("oldest_state_since") or "—"
            days = freshness.get("max_unchanged_days")
            span = f"（已 {days} 天）" if days else ""
            coverage_line += (
                f"\n[yellow]來源持股揭露停滯："
                f"{freshness['sources_unchanged']}/{freshness['sources_total']} 檔 ETF "
                f"自 {since} 起揭露內容完全未變{span}。[/yellow]"
                f"\n[dim]Yahoo 依各基金揭露頻率更新前十大持股（多為月頻），且不揭露股數；"
                f"權重沒有變動時無法推論任何買賣。這是資料來源限制，不是「本期沒有交易」。"
                f"下方 13F 區塊改用逐季真實申報股數，另 ARK 系列已改抓官方每日完整持股。[/dim]"
            )
        # bug#00124（使用者要求 1）：13F 的資料年代必須寫清楚 —— 它描述的是哪一季、
        # 什麼時候才公開、距今多久、下一次會在什麼時候更新。四個都不能互相推導。
        self._inst_provenance = inst_report.get("provenance") or {}
        if inst_report.get("report_dates"):
            prov = self._inst_provenance
            dates = inst_report["report_dates"]
            published = [
                f"{p['report_date']} 公開於 {p['filing_date']}"
                + ("（法定期限，實際申報日未記錄）" if p.get("filing_date_estimated") else "")
                for p in prov.get("periods") or []
            ]
            coverage_line += (
                f"\n[cyan]🏛 SEC 13F 季度環比：{dates[0]} → {dates[-1]}　"
                f"申報機構 {inst_report['etfs_comparable_count']}/{inst_report['etfs_total_count']} 家"
                f"　（依真實申報股數，變動 ≥ "
                f"{inst_report.get('rel_share_threshold', 0.05) * 100:.0f}% 才計為增減持）[/cyan]"
                f"\n[dim]　　資料期別：{'；'.join(published) or '—'}[/dim]"
                f"\n[yellow]　　⚠ 最新一期為 {prov.get('report_date_to')} 的季末持股，"
                f"距今 {prov.get('data_age_days')} 天；13F 法定申報期限為季末後 "
                f"{prov.get('filing_lag_days')} 天，下一期（{prov.get('next_report_date')}）"
                f"最晚 {prov.get('next_filing_due')} 公開。[/yellow]"
                f"\n[dim]　　13F 只揭露季末快照、不揭露成交日 —— 表中的買賣發生在 "
                f"{prov.get('trade_window_from')} 至 {prov.get('trade_window_to')} 之間的"
                f"某個未知時點，只能作為中期部位訊號，不適合當短天期進出依據。"
                f"　保留 {inst_report.get('history_quarters')} 期（"
                f"{'、'.join(inst_report.get('retained_report_dates') or [])}）僅供計算連續同向季數，"
                f"比較口徑固定為最新兩期。[/dim]"
            )
        from rich.panel import Panel as _Panel
        self.query_one("#aa-header", Static).update(
            _Panel(coverage_line, border_style="dim", padding=(0, 1))
        )

        from .calibration import calibration_status_label
        _bt_status = calibration_status_label(_bt)
        w = self.query_one("#aa-conclusions", Static)
        self._recs_by_id = {}
        conclusion_body = (
            f"[dim]回測校準狀態：{_bt_status}。方向結論與「與你相關」篩選在按 Esc 返回後的建議頁；"
            "本頁是全市場研究表。[/dim]"
            "\n[dim]表格中的「買入／賣出」只計入已確認方向事件；ETF 區塊的"
            "「淨部位變化」為期末部位市值減期初部位市值，保留期間總體變化，"
            "但可能包含價格與基金 AUM 影響，不直接等同交易金額；"
            "13F 區塊則顯示已確認增減持的淨額（依真實申報股數），"
            "以免與右側方向欄互相矛盾。[/dim]"
            "\n[dim]「配置權重」＝該部位市值 ÷「實際持有它的基金／申報人」合計資產，"
            "期初→期末兩個真值與 pp 變化；分母不含沒有持有它的來源。"
            "金額會隨股價波動，配置權重才是經理人真正做的選擇。[/dim]"
            "\n[dim]★ 為你的持有部位、◆ 為你追蹤類股的成分股，一律排在最前面。"
            "13F 期權部位以「增持／減持」表示申報部位變化 —— 13F 不揭露買方或賣方，"
            "增加 PUT 可能是買進避險也可能是賣出收權利金，因此不推論標的多空。[/dim]"
        )
        w.border_title = "研究表說明"
        w.update(conclusion_body)

        table = self.query_one("#aa-table", DataTable)
        empty = self.query_one("#aa-empty", Static)

        institution_rows = self._institution_rows(inst_report)

        if not position_rows and not institution_rows:
            table.display = False
            empty.display = True
            if report["etfs_ready_count"] == 0:
                empty.update(
                    "[yellow]目前尚未累積滿 2 個不同的真實持股狀態，"
                    "無法計算增減持趨勢。[/yellow]\n\n"
                    "[dim]相同持股與權重的重複每日觀測只算一個狀態，"
                    "不會因日期不同而假裝資料已就緒。系統會持續等待資料來源"
                    "實際更新持股內容；不回填或捏造交易。[/dim]"
                )
            else:
                empty.update(
                    "[yellow]目前沒有可比較的精確部位。[/yellow]\n\n"
                    "[dim]系統已有不同持股狀態，但前後狀態沒有共同或新增／"
                    "移除的具名部位可供條列。[/dim]"
                )
            return

        table.display = True
        empty.display = False
        table.clear(columns=False)

        def _money(value) -> str:
            if value is None:
                return "—"
            return ActiveETFsScreen._fmt_aum(float(value))

        def _signed_money(value) -> str:
            if value is None:
                return "—"
            amount = float(value)
            sign = "+" if amount > 0 else "−" if amount < 0 else ""
            return f"{sign}{_money(abs(amount))}"

        aggregate = tilt.get("aggregate") or {}
        flow_totals = report.get("flow_totals") or {}
        stance_label = {
            "long": "[bold green]看漲[/bold green]",
            "short": "[bold red]看跌[/bold red]",
            "neutral": "[yellow]中性[/yellow]",
            "insufficient": "[dim]資料不足[/dim]",
        }.get(aggregate.get("stance"), "[dim]資料不足[/dim]")
        all_stale = bool(freshness.get("all_sources_unchanged"))
        table.add_row(
            "[bold]全部追蹤 ETF（總體統計）[/bold]",
            "[dim]ETF 共識[/dim]",
            f"近 {report['window_days']} 天",
            "—",
            "—" if all_stale else (
                f"[green]{aggregate.get('etfs_long', 0)}↑[/green] "
                f"[red]{aggregate.get('etfs_short', 0)}↓[/red]"
            ),
            "—" if all_stale else
            f"{_money(flow_totals.get('buy_value'))} ({flow_totals.get('positions_bought', 0)} 筆)",
            "—" if all_stale else
            f"{_money(flow_totals.get('sell_value'))} ({flow_totals.get('positions_sold', 0)} 筆)",
            _signed_money(flow_totals.get("net_value_delta")),
            "[yellow]來源未更新[/yellow]" if all_stale else stance_label,
        )

        for sym, info in position_rows:
            self._add_position_row(table, sym, info, "ETF 共識", _money, _signed_money)

        # ── SEC 13F 機構季度增減持（bug#00123 A）────────────────────────────
        if institution_rows:
            inst_flow = inst_report.get("flow_totals") or {}
            table.add_row(
                "[bold magenta]SEC 13F 機構（季度環比總計）[/bold magenta]",
                "[dim]13F 申報[/dim]",
                (
                    f"{inst_report['report_dates'][0]}→{inst_report['report_dates'][-1]}"
                    f"\n[dim]公開於 {(self._inst_provenance.get('periods') or [{}])[-1].get('filing_date', '—')}"
                    f"　距今 {self._inst_provenance.get('data_age_days')} 天[/dim]"
                    if inst_report.get("report_dates") else "—"
                ),
                "—",
                f"[green]{inst_flow.get('positions_bought', 0)}↑[/green] "
                f"[red]{inst_flow.get('positions_sold', 0)}↓[/red]",
                _money(inst_flow.get("buy_value")),
                _money(inst_flow.get("sell_value")),
                _signed_money(
                    (inst_flow.get("buy_value") or 0.0)
                    - (inst_flow.get("sell_value") or 0.0)
                ),
                "[dim]真實申報股數[/dim]",
            )
            for sym, info in institution_rows:
                self._add_position_row(
                    table, sym, info, "13F 申報", _money, _signed_money,
                )

    # ── helpers ─────────────────────────────────────────────────────────────

    def _institution_report(self) -> dict:
        """Quarter-over-quarter 13F consensus, or an empty report on any failure.

        Kept non-fatal on purpose: the ETF section must still render if the 13F
        history log is missing or unreadable."""
        from .storage import taiwan_now
        try:
            entity_ids = [record["id"] for record in hedge_fund_records()]
            snapshots = {
                entity: load_etf_daily_snapshots(entity) for entity in entity_ids
            }
            return compute_institution_trends(
                snapshots, today=taiwan_now().strftime("%Y-%m-%d"),
            )
        except Exception:
            return {}

    def _institution_rows(self, inst_report: dict) -> list[tuple]:
        """Institutional rows worth rendering, in the user's priority order.

        Four filers disclose ~16k securities, so the table would be unreadable
        unfiltered. Every position the user holds or tracks is always kept; the
        rest must carry a *cross-filer* directional consensus (a single manager
        rebalancing one line is not a signal) and is capped by scale.
        """
        symbols = (inst_report or {}).get("symbols") or {}
        if not symbols:
            return []

        priority: list[tuple] = []
        others: list[tuple] = []
        for sym, info in symbols.items():
            key = position_display_sort_key(
                sym, info, self._held, self._tracked, self._name_index,
            )
            if key[0] < 2:
                priority.append((key, sym, info))
            elif (
                info.get("consensus") in ("up", "down")
                and info.get("etfs_evaluated", 0) >= 2
            ):
                others.append((key, sym, info))

        priority.sort(key=lambda row: row[0])
        others.sort(key=lambda row: row[0])
        return [
            (sym, info)
            for _, sym, info in priority + others[:INSTITUTION_OTHER_ROWS_CAP]
        ]

    def _add_position_row(self, table, sym, info, source_label, _money, _signed_money) -> None:
        consensus = info.get("consensus")
        holder_word = "家" if source_label == "13F 申報" else "檔"
        position = info.get("position") or {}
        is_option = position.get("instrument_type") == "option"
        # bug#00123: an option line must NOT be labelled 看漲/看跌. Form 13F
        # discloses the contract class and size but never whether the manager
        # bought or wrote it, so "more PUTs" is not evidence of a bearish view
        # (it is equally consistent with selling puts, a bullish trade) and
        # "more CALLs" is not evidence of a bullish one. Report the position
        # change that was actually disclosed — 增持/減持 — and leave the
        # underlying direction unclaimed.
        up_label = "增持 ▲" if is_option else "看漲 ▲"
        down_label = "減持 ▼" if is_option else "看跌 ▼"
        if consensus == "up":
            dir_s = f"[bold green]{up_label} {info['consensus_pct']:.0f}%[/bold green]"
        elif consensus == "down":
            dir_s = f"[bold red]{down_label} {info['consensus_pct']:.0f}%[/bold red]"
        elif info.get("status") == "source_unchanged":
            # bug#00123（C）：這不是「比較後沒有變化」，是來源根本沒有發布新的持股狀態。
            # 兩者混用同一個「持平」標籤，會讓使用者把資料缺口讀成分析結論。
            dir_s = "[yellow]來源未更新[/yellow]"
        elif consensus == "flat":
            dir_s = f"[dim]持平（{len(info.get('etfs_flat') or [])} {holder_word}）[/dim]"
        else:
            dir_s = "[dim]分歧[/dim]"

        # 多數性（家數）與規模性（金額）可以指向相反方向 —— 3 家小額加碼、1 家大額
        # 減碼時，家數共識是「多數看多」而金額淨額是負的。兩個數字並排卻不說明，
        # 使用者只會覺得畫面自相矛盾，因此明確標註。
        net_for_flag = (
            info.get("confirmed_net_trade_value")
            if source_label == "13F 申報"
            else info.get("confirmed_net_value_delta")
        )
        if consensus in ("up", "down") and net_for_flag not in (None, 0):
            if (consensus == "up") != (net_for_flag > 0):
                dir_s += "[yellow] ⚠ 金額背離[/yellow]"

        # bug#00124：連續同向季數 —— 「連 3 季加碼」與「這季才加碼」是完全不同強度
        # 的訊號，只有保留的歷史期別分得出來。
        evaluated_quarters = info.get("transitions_evaluated")
        if consensus in ("up", "down") and evaluated_quarters:
            dir_s += (
                f"[dim]（{info.get('same_direction_quarters', 0)}/"
                f"{evaluated_quarters} 季同向）[/dim]"
            )

        position_label = " ".join(
            str(position.get("name") or position.get("issuer") or sym).split()
        )
        ticker = resolve_position_ticker(
            {**position, "symbol": sym}, getattr(self, "_name_index", {}),
        )
        if is_option:
            option_type = position.get("option_type") or "OPTION"
            # 13F labels already start with PUT/CALL (see
            # institutional.parse_13f_information_table); prefixing again
            # produced "PUT PUT TESLA".
            if not position_label.upper().startswith(option_type.upper()):
                position_label = f"{option_type} {position_label}"
            expiry = position.get("expiration")
            strike = position.get("strike")
            if expiry and strike is not None:
                position_label = f"{position_label} {expiry} ${strike}"
            else:
                position_label = (
                    f"{position_label}（到期日/履約價未揭露，"
                    "且 13F 不揭露買方或賣方，不推論標的方向）"
                )
        if ticker and ticker not in position_label:
            position_label = f"{ticker} · {position_label}"

        if ticker and ticker in self._held:
            marker, style = "★ ", "bold yellow"
        elif ticker and ticker in self._tracked:
            marker, style = "◆ ", "bold cyan"
        else:
            marker, style = "", "bold white"

        # bug#00123（C）：來源沒有發布第二個持股狀態時，「買入 $0／賣出 $0」同樣是把
        # 資料缺口寫成量測結果。這種列一律以「—」表示無法計算。
        stale = info.get("status") == "source_unchanged"
        # 13F rows use transacted amounts and their net, so the money columns
        # can never contradict the direction column beside them; ETF rows keep
        # the raw period exposure delta, which is their only remaining signal
        # when no directional event qualified (bug#00122 user decision).
        if source_label == "13F 申報":
            buy_value = info.get("buy_trade_value")
            sell_value = info.get("sell_trade_value")
            net_value = info.get("confirmed_net_trade_value")
        else:
            buy_value = info.get("buy_value")
            sell_value = info.get("sell_value")
            net_value = info.get("net_value_delta")
        buy_cell = "—" if stale else _money(buy_value)
        sell_cell = "—" if stale else _money(sell_value)

        # bug#00124（使用者要求 2）：allocation —— 這檔部位佔納入比較的基金／申報人
        # 合計資產的比重。金額會隨股價波動，配置權重才是經理人真正做的選擇。
        start_pct = info.get("allocation_start_pct")
        end_pct = info.get("allocation_end_pct")
        delta_pp = info.get("allocation_delta_pp")
        if end_pct is None:
            alloc_cell = "—"
        elif stale or start_pct is None or delta_pp is None:
            # 來源未更新時只印單一權重，不畫箭頭 —— 沒有發生過的變化不該長得像變化。
            alloc_cell = f"[dim]{end_pct:.2f}%[/dim]"
        else:
            tone = "green" if delta_pp > 0 else "red" if delta_pp < 0 else "dim"
            alloc_cell = (
                f"{start_pct:.2f}% → {end_pct:.2f}% "
                f"[{tone}]{delta_pp:+.2f}pp[/{tone}]"
            )

        # bug#00124（使用者要求 1、2）：13F 只揭露季末快照，從不揭露成交日。因此
        # 這一欄對 13F 是「交易發生的區間」＋資料公開日，不是交易日。
        if source_label == "13F 申報":
            period = (
                f"{info.get('first_date') or '—'}→{info.get('last_date') or '—'}"
            )
            filed = (getattr(self, "_inst_provenance", {}) or {}).get("periods") or []
            published = filed[-1].get("filing_date") if filed else None
            estimated = filed[-1].get("filing_date_estimated") if filed else False
            date_range = (
                f"{period}\n[dim]期間內交易（未揭露成交日）"
                + (f"　公開於 {published}{'（法定期限）' if estimated else ''}" if published else "")
                + "[/dim]"
            )
        else:
            date_range = f"{info.get('first_date') or '—'}→{info.get('last_date') or '—'}"

        table.add_row(
            f"[{style}]{marker}{position_label}[/{style}]",
            f"[dim]{source_label}[/dim]",
            date_range,
            alloc_cell,
            "—" if stale else (
                f"[green]{len(info['etfs_up'])}↑[/green] "
                f"[red]{len(info['etfs_down'])}↓[/red]"
            ),
            buy_cell,
            sell_cell,
            _signed_money(net_value),
            dir_s,
        )

    def action_go_back(self) -> None:
        self.dismiss()


# ─────────────────────────────────────────────────────────────────────────────
# Options Watchlist Screen (期權觀察清單)
# ─────────────────────────────────────────────────────────────────────────────
# bug#00061 / bug#00066: 期權觀察清單。標的來源 = 持倉自動帶入 ∪ 使用者自訂新增
# （storage.load/save_options_watchlist），每日真實累積價內外 ≤60 天到期合約的快照
# （quotes.fetch_options_snapshot）供「各標的期權分析」離線運算。預期波動與持倉淨 Greeks
# 仍在本頁顯示；方向投資建議已自 TUI 移除。
# 不顯示逐口合約價表、也不為顯示而逐口計算希臘字母。100% 離線運算，資料不足時
# 誠實顯示收集進度，絕不回填或捏造。


def _underlyings_from_positions(positions: list[Position]) -> list[str]:
    """Real underlyings only — from the user's own tracked positions.
    Options use their `underlying`; stocks/ETFs use their own `symbol`."""
    syms: set[str] = set()
    for p in positions:
        if p.instrument_type == "option" and p.underlying:
            syms.add(p.underlying.upper())
        elif p.instrument_type in ("stock", "etf"):
            syms.add(p.symbol.upper())
    return sorted(syms)


def _watchlist_underlyings(user: str, positions: list[Position]) -> "tuple[list[str], set[str], set[str]]":
    """bug#00066: 期權觀察清單 = 持倉自動帶入的標的 ∪ 使用者額外新增的標的。

    回傳 (all_sorted, position_set, extra_set)。持倉標的永遠顯示且不可刪除；使用者
    額外新增（storage.load_options_watchlist）且不與持倉重複者才可刪除。
    """
    from .storage import load_options_watchlist
    from .shared import is_taiwan_position
    # bug#00091：投資建議一律以美股為主——期權觀察清單排除台股（持倉照常追蹤，僅不進觀察清單/建議）。
    us_positions = [p for p in positions if not is_taiwan_position(p)]
    pos_set = set(_underlyings_from_positions(us_positions))
    def _is_tw_sym(sym: str) -> bool:
        sym = (sym or "").upper()
        return sym.endswith(".TW") or sym.endswith(".TWO")
    extra_set = {s for s in load_options_watchlist(user) if not _is_tw_sym(s)} - pos_set
    return sorted(pos_set | extra_set), pos_set, extra_set


def _fetch_and_cache_options_underlyings(stale: list[str]) -> None:
    """Screen-agnostic core of the options-chain snapshot background refresh —
    pure fetch-and-persist, no UI/Screen dependency.

    bug#00061 follow-up: extracted out of OptionsWatchlistScreen.run_background_
    fetch for the same reason as _fetch_and_cache_etf_symbols — AssetTrackApp
    calls this directly on a periodic timer so real options snapshots keep
    accumulating even while the user stays on a different screen across a day
    boundary, instead of only refreshing when 「期權觀察清單」 is actually opened.

    `stale` should already be filtered by the caller via
    storage.options_symbol_fresh(). No return value — the real data is
    durably written to options_cache/history/*.jsonl by the time this
    returns; a Screen caller re-reads it via load_options_daily_snapshots.
    """
    from concurrent.futures import ThreadPoolExecutor
    import time as _time
    from .quotes import fetch_options_snapshot, fetch_next_earnings_dates
    from .storage import append_options_daily_snapshot

    if not stale:
        return

    # bug#00068: record each stale underlying's next-earnings date alongside its
    # snapshot so divergence analysis can flag earnings-driven IV moves later.
    earnings = fetch_next_earnings_dates(stale)

    def _fetch_one(u: str):
        _time.sleep(0.35)  # pacing to avoid Yahoo rate limiting, same pattern as ETF fetch
        try:
            return u, fetch_options_snapshot(u)
        except Exception:
            return u, {"spot_price": None, "contracts": []}

    with ThreadPoolExecutor(max_workers=2) as ex:
        for u, res in ex.map(_fetch_one, stale):
            if res.get("contracts"):
                append_options_daily_snapshot(
                    u, res["contracts"], res.get("spot_price"),
                    snapshot_date=res.get("session_date"),
                    earnings_date=earnings.get(u.upper()),
                )
                prune_options_history(u, max_age_days=RICHNESS_HISTORY_DAYS)


# ─────────────────────────────────────────────────────────────────────────────
# Sector / thematic-group analysis (類股板塊分析 sector_analysis)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_and_cache_sector_groups(user: str) -> dict:
    """Fetch real market data for every sector group's members (batched over the
    union of symbols), append today's real breadth snapshot per group, and return
    {group_name: sector_analysis.summarize_group(...)} for display.

    Screen-agnostic (SectorAnalysisScreen.run_background_fetch and the App's
    periodic _background_data_refresh both call it) — same split-out pattern as
    _fetch_and_cache_etf_symbols. No-op-safe; never fabricates data."""
    from .storage import (
        load_sector_groups, append_sector_daily_snapshot, prune_sector_history,
        save_sector_summaries_cache, sector_predictive_cache_needs_refresh,
        save_sector_predictive_cache, mark_sector_predictive_attempt,
        append_symbol_daily_adjusted_closes, us_session_complete,
        us_session_date,
    )
    from . import sector_analysis, sector_predictive
    from .quotes import (
        fetch_sector_members_data, fetch_fx_rates, fetch_sector_prediction_bars,
    )

    groups = load_sector_groups(user)
    if not groups:
        return {}
    union = sorted({s for members in groups.values() for s in members})
    data = fetch_sector_members_data(union)

    # bug#00085：市值以本地幣別計價，先取真實匯率換算 USD 再加權，否則 KRW 之類的
    # 高面額幣別會憑數值大小佔走 99% 權重。取不到匯率時 cap_weights() 會自動退回
    # 等權並標示，不會硬加不同幣別。
    currencies = {
        (d.get("currency") or sector_analysis.infer_currency(sym))
        for sym, d in data.items()
    }
    fx = fetch_fx_rates(sorted(c for c in currencies if c))

    summaries: dict[str, dict] = {}
    for name, members in groups.items():
        prune_sector_history(name)
        summary = sector_analysis.summarize_group(data, members, fx=fx)
        summaries[name] = summary
        # append_sector_daily_snapshot() takes the full summarize_group() result and
        # archives the complete per-member fields (使用者要求保留180日). Passing the
        # old slim list here raised AttributeError ('list' has no .get) and crashed
        # every live fetch — the snapshot API now expects the summary dict.
        append_sector_daily_snapshot(name, summary)

    # Persist the live summaries cache (bug#00080) so re-entering the screen shows
    # data instantly. Only when at least one member has a real price, so a throttled/
    # failed fetch is retried rather than locking in blanks (cf. bug#00058/00083).
    got_real = any(
        m.get("price") is not None
        for s in summaries.values() for m in s.get("members", [])
    )
    if got_real:
        save_sector_summaries_cache(user, summaries)

    # 個股 1–3 日條件機率需要多年日線才有足夠樣本。依美股交易日＋板塊設定簽章
    # 每日最多重建一次；下載或建模失敗時保留上一版，不用空模型覆蓋真實歷史結果。
    if sector_predictive_cache_needs_refresh(user, groups):
        try:
            mark_sector_predictive_attempt(user, groups)
            history_symbols = list(dict.fromkeys([*union, "QQQ"]))
            history = fetch_sector_prediction_bars(history_symbols, years=5)
            truth_session = us_session_date()
            truth_session_complete = us_session_complete()
            completed_history = {
                symbol: [
                    bar
                    for bar in bars
                    if str(bar.get("date") or "") <= truth_session
                    and (
                        truth_session_complete
                        or str(bar.get("date") or "") < truth_session
                    )
                ]
                for symbol, bars in history.items()
            }
            for symbol, bars in completed_history.items():
                if symbol not in union:
                    continue
                append_symbol_daily_adjusted_closes(
                    symbol,
                    (
                        (bar["date"], bar["close"])
                        for bar in bars
                        if bar.get("date") and bar.get("close") is not None
                    ),
                    source="yfinance-auto-adjust",
                )
            model = sector_predictive.build_prediction_model(
                groups,
                completed_history,
            )
            save_sector_predictive_cache(user, groups, model)
        except Exception:
            pass
    return summaries


class SectorGroupModal(ModalScreen[Optional[dict]]):
    """新增 / 編輯 / 刪除板塊群組 (item#3)。回傳：
      {"action":"save","name":str,"members":[symbol,...]} 儲存（新增或編輯，含改名）；
      {"action":"delete"} 刪除此板塊（僅編輯模式）；None 取消。
    成分股以空白或逗號分隔輸入，一律轉大寫並去重（保留輸入順序）。"""
    DEFAULT_CSS = """
    SectorGroupModal { align: center middle; }
    #sg-dialog { width: 64; height: auto; border: solid #21262d; background: #161b22; padding: 1 2; }
    #sg-title { text-style: bold; color: #58a6ff; margin-bottom: 1; }
    #sg-name, #sg-members { margin-bottom: 1; border: solid #30363d; background: #0d1117; }
    #sg-name:focus, #sg-members:focus { border: solid #58a6ff; }
    #sg-error { color: #ff7b72; height: auto; margin-bottom: 1; }
    #sg-buttons { height: auto; align: right middle; }
    #sg-buttons Button { margin-left: 1; }
    """

    def __init__(self, mode: str = "add", name: str = "", members: Optional[list[str]] = None) -> None:
        super().__init__()
        self.mode = mode
        self._name = name
        self._members = members or []

    def compose(self) -> ComposeResult:
        title = "[bold]新增板塊[/bold]" if self.mode == "add" else "[bold]編輯板塊[/bold]"
        with Vertical(id="sg-dialog"):
            yield Label(title, id="sg-title")
            yield Label("板塊名稱：", classes="form-label")
            yield Input(value=self._name, placeholder="例如 CPU 處理器", id="sg-name")
            yield Label("成分股（空白或逗號分隔）：", classes="form-label")
            yield Input(
                value=" ".join(self._members),
                placeholder="例如 INTC AMD NVDA ARM QCOM",
                id="sg-members",
            )
            yield Label("", id="sg-error")
            with Horizontal(id="sg-buttons"):
                yield Button("儲存", variant="primary", id="save")
                if self.mode == "edit":
                    yield Button("刪除板塊", variant="error", id="delete")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#sg-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._submit()
        elif event.button.id == "delete":
            self.dismiss({"action": "delete"})
        else:
            self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)

    @staticmethod
    def _parse_members(raw: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for tok in raw.replace(",", " ").split():
            s = tok.strip().upper()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def _submit(self) -> None:
        name = self.query_one("#sg-name", Input).value.strip()
        members = self._parse_members(self.query_one("#sg-members", Input).value)
        if not name:
            self.query_one("#sg-error", Label).update("請輸入板塊名稱")
            return
        if not members:
            self.query_one("#sg-error", Label).update("請至少輸入一檔成分股")
            return
        self.dismiss({"action": "save", "name": name, "members": members})


class SectorAnalysisScreen(_FormulaDrillMixin, Screen):
    r"""類股板塊分析 (sector_analysis) — 左板塊 / 右成分股 兩欄版面 (item#8)。

    Layout::

        ┌──────────────────────┬──────────────────────┐
        │  左欄 50% 板塊項目    │  右欄 50% 板塊成分股 │
        │  總市值/加權漲跌/廣度 │  現價/收盤/漲跌%/量額 │
        └──────────────────────┴──────────────────────┘

    左欄依「當日市值加權漲跌%」由高到低排序（上漲最高置頂、下跌最多置底，item#1），
    並顯示每日累計廣度共識（item#4）。右欄為選定板塊的成分股即時明細（item#8）。
    """

    BINDINGS = [
        Binding("r", "refresh_now",  "重新整理"),
        Binding("a", "add_group",    "新增板塊"),
        Binding("e", "edit_group",   "編輯板塊"),
        Binding("d", "delete_group", "刪除板塊"),
        Binding("escape", "go_back", "返回"),
        Binding("q",      "go_back", "返回", show=False),
    ]

    DEFAULT_CSS = """
    SectorAnalysisScreen {
        background: #0d1117;
        layout: vertical;
    }
    #sec-header { height: auto; padding: 0 1; margin: 1 2 0 2; }
    #sec-body { height: 1fr; layout: horizontal; margin: 1 2; }
    #sec-left-col { width: 50%; height: 1fr; layout: vertical; margin-right: 1; }
    #sec-left-title { height: 1; padding: 0 1; color: $accent; text-style: bold; }
    #sec-groups-panel { height: 1fr; border: tall #334155; }
    #sec-groups-panel:focus-within { border: tall $accent; }
    #sec-groups-table { height: 1fr; border: none; }
    #sec-right-col { width: 50%; height: 1fr; layout: vertical; }
    #sec-members-title { height: 1; padding: 0 1; color: $accent; text-style: bold; }
    #sec-members-status { height: 1; padding: 0 1; }
    #sec-members-panel { height: 1fr; border: tall #334155; }
    #sec-members-panel:focus-within { border: tall $accent; }
    #sec-members-table { height: 1fr; border: none; }
    /* bug#00088：上下改為 1fr : 1fr 平均分配（原本 body=1fr、建議區 max-height:12
       造成頭重腳輕），並改用 ScrollableContainer 讓過長的建議可上下捲動。 */
    #sec-conclusions-panel {
        height: 1fr;
        margin: 0 2 1 2;
        border: solid #21262d;
        overflow-y: auto;
        overflow-x: hidden;
    }
    #sec-conclusions-panel:focus-within { border: tall $accent; }
    #sec-conclusions { height: auto; padding: 0 1; }
    """

    def __init__(self, user: str) -> None:
        super().__init__()
        self.user = user
        self.summaries: dict[str, dict] = {}
        self.flows: dict[str, dict] = {}
        self.group_order: list[str] = []
        self.selected_group: str | None = None
        self._updated_at: Optional[datetime] = None  # 快取/抓取時間戳記

    # ── Compose ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("", id="sec-header")
        with Horizontal(id="sec-body"):
            with Vertical(id="sec-left-col"):
                yield Static("板塊項目（依當日市值加權漲跌% 排序）", id="sec-left-title")
                with Container(id="sec-groups-panel"):
                    yield DataTable(id="sec-groups-table")
            with Vertical(id="sec-right-col"):
                yield Static("板塊成分股", id="sec-members-title")
                yield Static("", id="sec-members-status")
                with Container(id="sec-members-panel"):
                    yield DataTable(id="sec-members-table")
        with ScrollableContainer(id="sec-conclusions-panel"):
            yield Static("", id="sec-conclusions")
        yield Footer()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        from .storage import load_sector_groups

        g_t = self.query_one("#sec-groups-table", DataTable)
        g_t.cursor_type = "row"
        g_t.add_columns("板塊", "總市值", "當日%", "廣度", "週%", "月%", "共識")

        m_t = self.query_one("#sec-members-table", DataTable)
        m_t.cursor_type = "row"
        # item#1 佔比（市值權重）、item#3 當日/當週/當月漲跌% 皆納入成分股明細。
        m_t.add_columns(
            "Symbol", "現價", "收盤價", "佔比", "當日%", "當週%", "當月%", "成交量", "成交額"
        )

        # Seed from the live summaries cache so re-entering the screen shows the last
        # data instantly — no blank reload (bug#00080：不要每次進入模塊都重新載入).
        from .storage import load_sector_summaries_cache
        self.group_order = list(load_sector_groups(self.user).keys())
        cache = load_sector_summaries_cache(self.user)
        self.summaries = cache.get("summaries") or {}
        try:
            self._updated_at = datetime.fromisoformat(cache.get("last_refreshed") or "")
        except (ValueError, TypeError):
            self._updated_at = None
        self._recompute_flows()
        self._set_header("[dim]載入板塊…[/dim]" if not self.summaries else "[dim]快取 · 檢查更新[/dim]")
        self._set_member_status("[dim]← 選取左欄板塊以查看成分股[/dim]")
        self._render_groups()
        g_t.focus()

        # 開盤中每 60 秒檢查一次；run_background_fetch 內部依市場時段決定是否真的重抓。
        self.set_interval(60, self.run_background_fetch)
        self.run_background_fetch()

    # ── Status helpers ─────────────────────────────────────────────────────────

    def _set_header(self, status: str) -> None:
        from rich.panel import Panel as _Panel
        self.query_one("#sec-header", Static).update(
            _chrome_header("類股", status)
        )

    def _set_member_status(self, text: str) -> None:
        self.query_one("#sec-members-status", Static).update(text)

    def _set_member_title(self, text: str) -> None:
        self.query_one("#sec-members-title", Static).update(text)

    # ── Formatting ──────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_big(v: float | None, unit: str = "$") -> str:
        if v is None:
            return "[dim]—[/dim]"
        av = abs(v)
        if av >= 1e12:
            return f"{unit}{v / 1e12:.2f}T"
        if av >= 1e9:
            return f"{unit}{v / 1e9:.2f}B"
        if av >= 1e6:
            return f"{unit}{v / 1e6:.1f}M"
        if av >= 1e3:
            return f"{unit}{v / 1e3:.0f}K"
        return f"{unit}{v:,.0f}"

    @staticmethod
    def _fmt_pct(v: float | None) -> str:
        if v is None:
            return "[dim]—[/dim]"
        color = "green" if v > 0 else "red" if v < 0 else "white"
        return f"[{color}]{v:+.2f}%[/{color}]"

    @staticmethod
    def _fmt_vol(v: float | None) -> str:
        if v is None:
            return "[dim]—[/dim]"
        if v >= 1e6:
            return f"{v / 1e6:.1f}M"
        if v >= 1e3:
            return f"{v / 1e3:.0f}K"
        return f"{v:,.0f}"

    def _fmt_consensus(self, group: str) -> str:
        f = self.flows.get(group)
        if not f or not f.get("ready"):
            return "[dim]收集中[/dim]"
        if f["direction"] == "up":
            return f"[green]偏多 {f['up_days']}/{f['days_evaluated']}[/green]"
        if f["direction"] == "down":
            return f"[red]📉 普遍跌 {f['down_days']}/{f['days_evaluated']}[/red]"
        return "[dim]—[/dim]"

    # ── Breadth flows (offline) ─────────────────────────────────────────────────

    def _recompute_flows(self) -> None:
        from .storage import (
            load_sector_daily_snapshots, load_sector_groups,
            load_sector_predictive_model,
        )
        from . import sector_analysis
        # bug#00095 接線：套用已確認校準參數。
        _sap = _active_params(self.user).get('sector', {})
        _bth = _sap.get('breadth_threshold', 0.5)
        _md = _sap.get('min_days', 3)
        snapshots_by_group = {name: load_sector_daily_snapshots(name) for name in self.group_order}
        self.flows = {
            name: sector_analysis.detect_broad_flow(snapshots_by_group[name], breadth_threshold=_bth, min_days=_md)
            for name in self.group_order
        }
        # bug#00093: walk-forward 回測（與 Dashboard 卡片共用同一套邏輯）。
        self._sector_backtest = sector_analysis.backtest_sector_flow(snapshots_by_group, breadth_threshold=_bth, min_days=_md)
        _groups = load_sector_groups(self.user)
        self._sector_predictive_model = load_sector_predictive_model(
            self.user, _groups
        )
        self._sector_confirmations = (
            ((self._sector_predictive_model or {}).get("sector_confirmation") or {}).get("groups")
            or {}
        )

    # ── Render ──────────────────────────────────────────────────────────────────

    def _render_groups(self) -> None:
        table = self.query_one("#sec-groups-table", DataTable)
        table.clear(columns=False)

        # item#1: 依當日市值加權漲跌% 由高到低排序（上漲最高置頂、下跌最多置底）。
        ordered = sorted(
            self.group_order,
            key=lambda g: (
                self.summaries.get(g, {}).get("capw_day") is not None,
                self.summaries.get(g, {}).get("capw_day") or 0.0,
            ),
            reverse=True,
        )
        self.group_order = ordered

        for name in ordered:
            s = self.summaries.get(name, {})
            n_up = s.get("n_up", 0)
            n_down = s.get("n_down", 0)
            breadth_s = f"[green]▲{n_up}[/green] [red]▼{n_down}[/red]" if s else "[dim]—[/dim]"
            table.add_row(
                f"[bold white]{name}[/bold white]",
                self._fmt_big(s.get("total_marketcap")),
                self._fmt_pct(s.get("capw_day")),
                breadth_s,
                self._fmt_pct(s.get("capw_week")),
                self._fmt_pct(s.get("capw_month")),
                self._fmt_consensus(name),
            )

        if ordered:
            sel = self.selected_group if self.selected_group in ordered else ordered[0]
            self._render_members(sel)
        else:
            self.selected_group = None
            self.query_one("#sec-members-table", DataTable).clear(columns=False)
            self._set_member_title("板塊成分股")
            self._set_member_status("[dim]尚無任何板塊，按 [bold]a[/bold] 新增[/dim]")

        self._render_conclusions()

    def _render_conclusions(self) -> None:
        """底部類股方向預測（未來 10 個交易日）。與 Dashboard 卡片共用
        generate_sector_recommendations()，兩處結論一致。"""
        from . import sector_analysis

        _sec_bt = getattr(self, "_sector_backtest", None)
        confirmations = getattr(self, "_sector_confirmations", {}) or {}
        recs = sector_analysis.generate_sector_recommendations(
            self.flows, confirmations=confirmations, backtest=_sec_bt
        )
        all_recs = recs
        w = self.query_one("#sec-conclusions", Static)
        # bug#00088：邊框移到可捲動的外層容器，border_title 需掛在有邊框的元件上。
        self.query_one("#sec-conclusions-panel").border_title = (
            "📝 類股方向預測（未來 10 個交易日）"
        )

        if all_recs:
            _model = getattr(self, "_sector_predictive_model", None) or {}
            confirmation_as_of = (
                (_model.get("sector_confirmation") or {}).get("as_of") or "—"
            )
            body, mapping = render_detail_recs(
                all_recs,
                header=f"[dim]複合訊號歷史資料日：{confirmation_as_of}[/dim]",
            )
            self._recs_by_id = mapping
        else:
            ready = sum(1 for f in self.flows.values() if f.get("ready"))
            total = len(self.flows)
            body = (
                f"[dim]{ready}/{total} 個板塊已有 breadth 資料；目前沒有未來 10 個交易日"
                f"的上漲或下跌預測，因此系統棄權。[/dim]"
            )
            self._recs_by_id = {}
        w.update(body)

    def _market_stress_lines(self) -> list[str]:
        """市場級訊號（bug#00085）：跨板塊廣度 + 單日嚴重度，離線由已累積的真實快照
        計算。無訊號或資料不足時回空 list（不顯示、不臆測）。"""
        from . import sector_analysis
        from .storage import load_sector_daily_snapshots, load_sector_groups
        try:
            snaps = {g: load_sector_daily_snapshots(g) for g in load_sector_groups(self.user)}
            snaps = {g: s for g, s in snaps.items() if s}
            if not snaps:
                return []
            comp = sector_analysis.compute_composite_index(snaps)
            stress = sector_analysis.detect_market_stress(snaps, benchmark_by_date=comp)
            sev = {}
            for g, s in snaps.items():
                ev = sector_analysis.detect_severity_event(s)
                if ev.get("direction") in ("up", "down") and ev.get("ready"):
                    sev[g] = ev
            # bug#00087：先寫出「目前整體狀態」的初步結論，再列市場/單日訊號。
            # 先前只逐板塊條列，使用者看不到「類股層面已達持續下跌共識」這個結論。
            lines = sector_analysis.consensus_lines(
                self.flows, stress, getattr(self, "_sector_backtest", None)
            )
            return lines + sector_analysis.generate_market_conclusions(stress, sev)
        except Exception:
            return []

    def _render_members(self, group: str) -> None:
        self.selected_group = group
        self._set_member_title(f"板塊成分股 — {group}")
        table = self.query_one("#sec-members-table", DataTable)
        table.clear(columns=False)

        s = self.summaries.get(group)
        if not s or not s.get("members"):
            self._set_member_status("[dim]資料收集中...[/dim]")
            return
        self._set_member_status(
            f"[dim]{s.get('n_rated', 0)} 檔有報價　市值加權當日 [/dim]" + self._fmt_pct(s.get("capw_day"))
        )
        for m in s["members"]:
            w = m.get("weight")
            weight_s = f"[cyan]{w:.1f}%[/cyan]" if w is not None else "[dim]—[/dim]"
            table.add_row(
                f"[bold white]{m.get('symbol')}[/bold white]",
                self._fmt_big(m.get("price"), unit="") if m.get("price") is not None else "[dim]—[/dim]",
                self._fmt_big(m.get("prev_close"), unit="") if m.get("prev_close") is not None else "[dim]—[/dim]",
                weight_s,
                self._fmt_pct(m.get("day_pct")),
                self._fmt_pct(m.get("week_pct")),
                self._fmt_pct(m.get("month_pct")),
                self._fmt_vol(m.get("volume")),
                self._fmt_big(m.get("turnover")),
            )

    # ── Selection ────────────────────────────────────────────────────────────────

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "sec-groups-table":
            return
        idx = event.cursor_row
        if 0 <= idx < len(self.group_order):
            self._render_members(self.group_order[idx])

    # ── Background worker ─────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True)
    def run_background_fetch(self, force: bool = False) -> None:
        """Refresh sector data only when the cache is stale for the current market
        session (sector_cache_needs_refresh): US market open → every 60s; closed →
        once per session. Otherwise reuse the cached summaries with no network call
        (bug#00080). `force=True`（手動 r 鍵，bug#00083）略過新鮮度判定一律重抓——用於
        快取在節流時抓到不完整資料、又逢休市無法自動重抓的情況。"""
        from .storage import sector_cache_needs_refresh, load_sector_summaries_cache

        if not force and not sector_cache_needs_refresh(self.user):
            cached = load_sector_summaries_cache(self.user)
            summaries = cached.get("summaries") or {}
            if summaries:
                self.app.call_from_thread(
                    self._on_fetch_complete,
                    summaries,
                    True,
                    cached.get("last_refreshed") or "",
                )
                return

        self.app.call_from_thread(self._set_header, "[dim]更新板塊…[/dim]")
        summaries = _fetch_and_cache_sector_groups(self.user)
        self.app.call_from_thread(self._on_fetch_complete, summaries, False)

    def _on_fetch_complete(
        self,
        summaries: dict,
        from_cache: bool = False,
        cached_at: str = "",
    ) -> None:
        # 載入新價前保留上一筆最後資料：抓取結果為空（無群組/失敗）時不清空畫面。
        summaries_changed = bool(summaries and summaries != self.summaries)
        if summaries:
            self.summaries = summaries
        if not from_cache or summaries_changed:
            self._recompute_flows()
        if from_cache:
            try:
                self._updated_at = datetime.fromisoformat(cached_at)
            except (ValueError, TypeError):
                self._updated_at = None
            ts = self._updated_at.strftime("%Y-%m-%d %H:%M") if self._updated_at else "—"
            self._set_header(f"[green]已載入快取[/green] [dim]（{ts} 更新）[/dim]")
        else:
            from .storage import taiwan_now
            self._updated_at = taiwan_now()
            ts = self._updated_at.strftime("%Y-%m-%d %H:%M")
            self._set_header(f"[green]板塊即時數據載入完成[/green] [dim]（{ts}）[/dim]")
        self._render_groups()

    # ── Actions ──────────────────────────────────────────────────────────────────

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_refresh_now(self) -> None:
        """手動重新整理 (bug#00083)：略過快取新鮮度判定，強制重新抓取——用於快取被節流
        時抓到不完整資料、市場休市又無法自動重抓的情況。"""
        self._set_header("[dim]重新整理…[/dim]")
        self.run_background_fetch(force=True)

    # ── Group create / edit / delete (item#3) ────────────────────────────────────

    def action_add_group(self) -> None:
        self.app.push_screen(SectorGroupModal(mode="add"), self._on_group_modal_result)

    def action_edit_group(self) -> None:
        if not self.selected_group:
            self.app.notify("請先於左欄選取要編輯的板塊", severity="warning")
            return
        from .storage import load_sector_groups
        members = load_sector_groups(self.user).get(self.selected_group, [])
        original = self.selected_group
        self.app.push_screen(
            SectorGroupModal(mode="edit", name=original, members=members),
            lambda r: self._on_group_modal_result(r, original=original),
        )

    def action_delete_group(self) -> None:
        if not self.selected_group:
            self.app.notify("請先於左欄選取要刪除的板塊", severity="warning")
            return
        from .storage import load_sector_groups
        members = load_sector_groups(self.user).get(self.selected_group, [])
        original = self.selected_group
        # Reuse the edit modal's 刪除 button rather than a separate confirm dialog.
        self.app.push_screen(
            SectorGroupModal(mode="edit", name=original, members=members),
            lambda r: self._on_group_modal_result(r, original=original),
        )

    def _on_group_modal_result(self, result: Optional[dict], original: Optional[str] = None) -> None:
        if not result:
            return
        from .storage import load_sector_groups, save_sector_groups
        groups = load_sector_groups(self.user)

        if result.get("action") == "delete":
            if original:
                groups.pop(original, None)
                save_sector_groups(self.user, groups)
                self.app.notify(f"已刪除板塊「{original}」")
                if self.selected_group == original:
                    self.selected_group = None
            self._reload_after_edit()
            return

        name = result["name"]
        members = result["members"]
        if original and original != name:
            groups.pop(original, None)  # rename: drop old key
        groups[name] = members
        save_sector_groups(self.user, groups)
        self.selected_group = name
        self.app.notify(f"已{'更新' if original else '新增'}板塊「{name}」")
        self._reload_after_edit()

    def _reload_after_edit(self) -> None:
        """After a save/delete: reload group order from disk, re-render, and refetch
        live data (a new/renamed group has no fresh snapshot yet, so the background
        fetch will pick it up)."""
        from .storage import load_sector_groups
        self.group_order = list(load_sector_groups(self.user).keys())
        self._recompute_flows()
        self._render_groups()
        if self.group_order:
            self._set_header("[dim]重抓板塊…[/dim]")
            self.run_background_fetch()
        else:
            self._set_header("[dim]尚無任何板塊，按 [bold]a[/bold] 新增[/dim]")


class AddTickerModal(ModalScreen[Optional[str]]):
    """輸入要加入期權觀察清單的標的代碼 (bug#00066)。"""
    DEFAULT_CSS = """
    AddTickerModal { align: center middle; }
    #at-dialog { width: 52; height: auto; border: solid #21262d; background: #161b22; padding: 1 2; }
    #at-title { text-style: bold; color: #58a6ff; margin-bottom: 1; }
    #at-input { margin-bottom: 1; border: solid #30363d; background: #0d1117; }
    #at-input:focus { border: solid #58a6ff; }
    #at-error { color: #ff7b72; height: auto; margin-bottom: 1; }
    #at-buttons { height: auto; align: right middle; }
    #at-buttons Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="at-dialog"):
            yield Label("[bold]新增觀察標的[/bold]", id="at-title")
            yield Input(placeholder="輸入標的代碼，例如 AAPL 或 2330.TW", id="at-input")
            yield Label("", id="at-error")
            with Horizontal(id="at-buttons"):
                yield Button("加入", variant="primary", id="confirm")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#at-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, _event) -> None:
        self._submit()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)

    def _submit(self) -> None:
        val = self.query_one("#at-input", Input).value.strip().upper()
        if not val:
            self.query_one("#at-error", Label).update("請輸入標的代碼")
            return
        self.dismiss(val)


class RemoveTickerModal(ModalScreen[Optional[str]]):
    """自使用者額外新增的標的中選擇一個移除（持倉自動帶入的標的不可移除）。"""
    DEFAULT_CSS = """
    RemoveTickerModal { align: center middle; }
    #rt-dialog { width: 52; height: auto; border: solid #21262d; background: #161b22; padding: 1 2; }
    #rt-title { text-style: bold; color: red; margin-bottom: 1; }
    #rt-list { height: auto; max-height: 16; border: solid $accent; }
    """

    def __init__(self, removable: list[str]) -> None:
        super().__init__()
        self.removable = removable

    def compose(self) -> ComposeResult:
        opts = [Option(t, id=t) for t in self.removable]
        opts.append(Option("取消", id="__cancel__"))
        with Vertical(id="rt-dialog"):
            yield Label("[bold]移除觀察標的（僅限自訂新增）[/bold]", id="rt-title")
            yield OptionList(*opts, id="rt-list")

    def on_mount(self) -> None:
        self.query_one("#rt-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        val = event.option.id
        self.dismiss(None if val == "__cancel__" else val)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class OptionsWatchlistScreen(Screen):
    """期權觀察清單 —— 標的自管理 + 各標的預期波動與持倉淨 Greeks。"""

    RICHNESS_COLUMN_INDEX = 4  # 波動貴賤：↑↓ 選列、Enter 看 ATM IV−RV 走勢

    BINDINGS = [
        Binding("a",      "add_ticker",    "新增標的"),
        Binding("d",      "remove_ticker", "刪除標的"),
        Binding("h",      "help",          "說明"),
        Binding("c",      "clear_cache",   "重抓今日"),
        Binding("escape", "go_back",       "返回"),
        Binding("q",      "go_back",       "返回", show=False),
    ]

    DEFAULT_CSS = """
    OptionsWatchlistScreen {
        background: #0d1117;
        layout: vertical;
    }
    #ow-header {
        height: auto;
        padding: 0 1;
        margin: 1 2 0 2;
    }
    #ow-top {
        /* 畫面只留「各標的期權分析」：可捲動容器吃滿 header 以外空間。 */
        height: 1fr;
    }
    #ow-top:focus-within { border-left: tall $accent; }
    #ow-portfolio {
        height: auto;
        min-height: 12;
        padding: 0 1;
        margin: 1 2 0 2;
    }
    #ow-portfolio-foot {
        height: auto;
        padding: 0 1 1 1;
        margin: 0 2 0 2;
    }
    """

    def __init__(self, user: str, positions: list[Position]) -> None:
        super().__init__()
        self.user = user
        self.positions = positions
        self.underlyings, _, self.extra_set = _watchlist_underlyings(user, positions)
        self.expected_move: dict = {}  # {underlying: compute_expected_move(...)}
        self.richness: dict = {}  # {underlying: assess_option_richness(...)}
        self.spot_by_underlying: dict = {}  # underlying -> spot, for portfolio Greeks
        self.closes_by_underlying: dict = {}  # underlying -> trailing closes for RV
        self.dated_closes_by_underlying: dict = {}  # underlying -> [(YYYY-MM-DD, close)]
        self.earnings_note_by_underlying: dict = {}  # underlying -> "財報剩Nd" | None
        self._table_underlyings: list[str] = []
        self.r: float = 0.04  # risk-free rate; refreshed from ^IRX in background

    def compose(self) -> ComposeResult:
        yield Static("", id="ow-header")
        with ScrollableContainer(id="ow-top"):
            yield DataTable(id="ow-portfolio")
            yield Static("", id="ow-portfolio-foot")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#ow-portfolio", DataTable)
        table.cursor_type = "cell"
        table.zebra_stripes = True
        table.add_columns(
            "標的",
            "預期波動 ±1σ",
            "ATM IV",
            "RV",
            "波動貴賤",
            "Call溢價",
            "Put溢價",
            "跨式溢價",
            "持倉Δ$",
            "Θ/日",
            "Vega",
        )
        for u in self.underlyings:
            prune_options_history(u, max_age_days=RICHNESS_HISTORY_DAYS)

        self._set_header(f"[dim]載入 {len(self.underlyings)} 檔…[/dim]")
        self._run_analysis()
        table.focus()
        self.run_background_fetch()

    def _set_header(self, status: str) -> None:
        from rich.panel import Panel as _Panel
        self.query_one("#ow-header", Static).update(
            _chrome_header(
                "期權",
                "[dim]↑↓ 貴賤  Enter 走勢  a 新增  d 刪除  h 說明  c 重抓[/dim]",
                status,
            )
        )

    def _render_portfolio(self) -> None:
        """bug#00073: 各標的期權分析總表 —— **以觀察清單標的為列**（新增標的會即時多一列），
        每列顯示由價平跨式估算的『預期波動區間』、ATM IV 相對已實現波動的貴賤，
        以及若你持有該標的選擇權時的『淨 Greeks』(僅選擇權、逐標的、bug#00071/72)。現股不納入 Greeks。"""
        from textual.coordinate import Coordinate
        from rich.text import Text as _Text

        table = self.query_one("#ow-portfolio", DataTable)
        foot = self.query_one("#ow-portfolio-foot", Static)
        old_row = table.cursor_coordinate.row if table.row_count else 0

        if not self.underlyings:
            table.clear(columns=False)
            self._table_underlyings = []
            foot.update("[dim]清單為空，按 a 新增標的[/dim]")
            return

        pg = compute_portfolio_greeks(self.positions, self.spot_by_underlying, r=self.r, options_only=True)
        by_u = pg["by_underlying"]
        total = pg["total"]

        def _money(v: float, signed: bool = True) -> str:
            c = "green" if v >= 0 else "red"
            sign = "+" if (signed and v >= 0) else ""
            return f"[{c}]{sign}${v:,.0f}[/{c}]"

        def _rich_label(kind: str) -> str:
            return {
                "expensive": "[red]偏貴[/red]",
                "cheap": "[cyan]偏便宜[/cyan]",
                "fair": "合理",
            }.get(kind, "—")

        def _edge_cell(side: Optional[dict]) -> str:
            if not side or side.get("edge") is None:
                return "[dim]—[/dim]"
            edge = side["edge"]
            warn = " [yellow]⚠[/yellow]" if side.get("low_confidence") else ""
            return f"{_rich_label(side['label'])} {edge:+.2f}{warn}"

        table.clear(columns=False)
        self._table_underlyings = []
        for u in self.underlyings:
            em = self.expected_move.get(u)
            if em and em.get("sigma_abs") is not None:
                warn = " [yellow]⚠[/yellow]" if em.get("low_confidence") else ""
                em_s = (f"[white]±${em['sigma_abs']:.2f}[/white] "
                        f"[dim](±{em['sigma_pct']:.1f}%,{em['dte']}d)[/dim]{warn}")
                iv_s = f"{em['atm_iv'] * 100:.0f}%{warn}" if em.get("atm_iv") else "—"
            else:
                em_s = "[dim]資料收集中[/dim]"
                iv_s = "—"

            rich = self.richness.get(u) or {}
            if rich.get("ready") and rich.get("realized_vol") is not None:
                rv_s = f"{rich['realized_vol'] * 100:.0f}%"
                spread = rich.get("vol_spread")
                spread_s = f" {spread * 100:+.0f}pp" if spread is not None else ""
                vol_s = f"{_rich_label(rich['richness'])}{spread_s}"
                earn_note = self.earnings_note_by_underlying.get(u)
                if earn_note:
                    vol_s += f" [yellow]{earn_note}[/yellow]"
                call_s = _edge_cell(rich.get("call"))
                put_s = _edge_cell(rich.get("put"))
                st = rich.get("straddle_edge")
                st_s = f"{st:+.2f}" if st is not None else "[dim]—[/dim]"
                if rich.get("low_confidence"):
                    vol_s += " [yellow]⚠[/yellow]"
            else:
                rv_s = vol_s = call_s = put_s = st_s = "[dim]樣本不足[/dim]"
                earn_note = self.earnings_note_by_underlying.get(u)
                if earn_note:
                    vol_s = f"[dim]樣本不足[/dim] [yellow]{earn_note}[/yellow]"

            g = by_u.get(u)
            if g and g["priced"] > 0:
                d_s = _money(g["delta_dollars"], signed=False)
                t_s = _money(g["theta_day"])
                v_s = f"${g['vega_1pt']:,.0f}"
                held = " [magenta]◆[/magenta]"
            else:
                d_s = t_s = v_s = "[dim]—[/dim]"
                held = ""
            table.add_row(
                f"[bold white]{u}[/bold white]{held}",
                em_s, iv_s, rv_s, vol_s, call_s, put_s, st_s, d_s, t_s, v_s,
            )
            self._table_underlyings.append(u)

        notes = [
            "[dim]波動貴賤欄 ↑↓ 選擇、Enter 看近90天每日 ATM IV − RV（RV＝當日往前20個交易日；更早快照會刪除）。"
            "波動貴賤＝ATM IV 對近20日已實現波動（≥+3pp 偏貴、≤−3pp 偏便宜）。"
            "財報剩 N 天僅在剩餘 <10 天時顯示。"
            "Call/Put／跨式溢價＝市價中間價 − 以已實現波動代入 BS 的理論價；正＝權利金偏貴。"
            "這不是股價漲跌預測。[yellow]⚠[/yellow]＝低可信度；"
            "◆＝持有該標的選擇權（Δ$/Θ/Vega 淨值，現股不計）[/dim]"
        ]
        if total["priced"] > 0:
            notes.insert(
                0,
                f"持倉選擇權合計  Δ$ {_money(total['delta_dollars'], signed=False)}  "
                f"Θ/日 {_money(total['theta_day'])}  "
                f"Vega ${total['vega_1pt']:,.0f}",
            )
        if total["unpriced"]:
            notes.append(
                f"[dim][yellow]*[/yellow] {len(total['unpriced'])} 筆選擇權無法定價（缺現價/無法反解 IV），未計入合計[/dim]"
            )
        foot.update(_Text.from_markup("\n".join(notes)))

        if table.row_count:
            table.cursor_coordinate = Coordinate(
                min(max(old_row, 0), table.row_count - 1),
                self.RICHNESS_COLUMN_INDEX,
            )
            if self.app.screen is self:
                table.focus()

    # ── Background worker ──────────────────────────────────────────────────────

    @work(thread=True)
    def run_background_fetch(self) -> None:
        """背景抓取：先取無風險利率（^IRX），再對尚未有今日快照的標的抓期權鏈。
        期權鏈抓取沿用共用的 _fetch_and_cache_options_underlyings()。"""
        from .storage import options_symbol_fresh
        from .quotes import fetch_risk_free_rate

        previous_rate = self.r
        self.r = fetch_risk_free_rate(default=self.r)
        self._refresh_underlying_spots()
        self._refresh_underlying_closes()
        rate_changed = abs(self.r - previous_rate) > 1e-9

        stale = [u for u in self.underlyings if not options_symbol_fresh(u)]
        if not stale:
            self.app.call_from_thread(self._on_fetch_complete, rate_changed)
            return

        self.app.call_from_thread(self._set_header, f"[dim]更新 {len(stale)} 檔期權…[/dim]")
        _fetch_and_cache_options_underlyings(stale)
        self.app.call_from_thread(self._on_fetch_complete, True)

    def _refresh_underlying_spots(self) -> None:
        """抓取持倉中選擇權標的的現價（供投資組合淨 Greeks 反解 IV 用）。在背景執行緒呼叫。"""
        from .quotes import fetch_prices_batch, _normalize_symbol_for_yf
        norm_by_u = {
            p.underlying.upper(): _normalize_symbol_for_yf(p.underlying, "stock", p.currency)
            for p in self.positions
            if p.instrument_type == "option" and p.underlying
        }
        if not norm_by_u:
            return
        try:
            prices = fetch_prices_batch(sorted(set(norm_by_u.values())))
            self.spot_by_underlying = {
                u: prices.get(n) for u, n in norm_by_u.items() if prices.get(n) is not None
            }
        except Exception:
            pass

    def _refresh_underlying_closes(self) -> None:
        """Trailing daily closes for realized vol. Runs on the background thread."""
        from datetime import date, datetime, timedelta
        from .quotes import fetch_benchmark_history, _normalize_symbol_for_yf

        end = date.today()
        start = end - timedelta(days=RICHNESS_HISTORY_DAYS + 40)
        start_dt = datetime(start.year, start.month, start.day)
        end_dt = datetime(end.year, end.month, end.day)
        closes: dict = {}
        dated: dict = {}
        for underlying in self.underlyings:
            yf_symbol = _normalize_symbol_for_yf(underlying, "stock", "USD")
            rows = fetch_benchmark_history(yf_symbol, start_dt, end_dt)
            if rows:
                dated[underlying] = [(day.isoformat(), price) for day, price in rows]
                closes[underlying] = [price for _, price in rows]
        if closes:
            self.closes_by_underlying = {**self.closes_by_underlying, **closes}
            self.dated_closes_by_underlying = {**self.dated_closes_by_underlying, **dated}

    def _on_fetch_complete(self, analysis_changed: bool = True) -> None:
        if analysis_changed:
            self._run_analysis()
        else:
            # The mount path already rendered the unchanged snapshot analysis.
            # Spot prices can still have changed, so only refresh the lightweight
            # portfolio Greeks panel.
            self._render_portfolio()

    # ── Analysis + render ────────────────────────────────────────────────────

    def _run_analysis(self) -> None:
        snapshots_by_underlying = {u: load_options_daily_snapshots(u) for u in self.underlyings}
        self.expected_move = {
            u: compute_expected_move(snapshots_by_underlying[u][-1] if snapshots_by_underlying[u] else None)
            for u in self.underlyings
        }
        self.richness = {
            u: richness_from_history(
                snapshots_by_underlying[u],
                r=self.r,
                closes=self.closes_by_underlying.get(u),
            )
            for u in self.underlyings
        }
        from .storage import taiwan_now
        today = taiwan_now().strftime("%Y-%m-%d")
        notes = {}
        for u in self.underlyings:
            snaps = snapshots_by_underlying[u]
            earn = snaps[-1].get("earnings_date") if snaps else None
            notes[u] = earnings_remaining_note(days_to_earnings(today, earn))
        self.earnings_note_by_underlying = notes
        self._render_portfolio()

        ready = sum(1 for snaps in snapshots_by_underlying.values() if snaps)
        last_dates = [
            snaps[-1].get("date")
            for snaps in snapshots_by_underlying.values()
            if snaps and snaps[-1].get("date")
        ]
        as_of = max(last_dates) if last_dates else "—"
        self._set_header(
            f"清單 {len(self.underlyings)} 檔　已有快照 {ready}/{len(self.underlyings)}　"
            f"無風險利率 {self.r * 100:.2f}%　更新 {as_of}"
        )

    # ── Add / remove tickers ───────────────────────────────────────────────────

    def action_add_ticker(self) -> None:
        self.app.push_screen(AddTickerModal(), self._handle_add_ticker)

    def _handle_add_ticker(self, ticker: Optional[str]) -> None:
        if not ticker:
            return
        ticker = ticker.strip().upper()
        if ticker in self.underlyings:
            self.app.notify(f"{ticker} 已在清單中")
            return
        from .storage import load_options_watchlist, save_options_watchlist
        save_options_watchlist(self.user, load_options_watchlist(self.user) + [ticker])
        self.underlyings, _, self.extra_set = _watchlist_underlyings(self.user, self.positions)
        prune_options_history(ticker, max_age_days=RICHNESS_HISTORY_DAYS)
        self.app.notify(f"已加入 {ticker}，背景抓取期權資料中...")
        # 立即重跑分析：上方「各標的期權分析」表會馬上多一列（新標的先顯示「資料收集中」，
        # 待背景抓完再自動補上預期波動/IV/Greeks）(bug#00073)
        self._run_analysis()
        self.run_background_fetch()

    def action_remove_ticker(self) -> None:
        removable = sorted(self.extra_set)
        if not removable:
            self.app.notify("無可移除標的（持倉自動帶入的標的不可移除）", severity="warning")
            return
        self.app.push_screen(RemoveTickerModal(removable), self._handle_remove_ticker)

    def _handle_remove_ticker(self, ticker: Optional[str]) -> None:
        if not ticker:
            return
        from .storage import load_options_watchlist, save_options_watchlist
        save_options_watchlist(self.user, [t for t in load_options_watchlist(self.user) if t != ticker])
        self.underlyings, _, self.extra_set = _watchlist_underlyings(self.user, self.positions)
        self.app.notify(f"已移除 {ticker}")
        self._run_analysis()

    def action_go_back(self) -> None:
        self.dismiss()

    def action_help(self) -> None:
        """[h] 開啟本頁各項數值的詳細說明頁（bug#00076）。"""
        self.app.push_screen(OptionsHelpScreen())

    def _fetch_dated_closes_now(self, underlying: str) -> list[tuple[str, float]]:
        """Stock daily closes covering the 90-day window plus a 20-session RV lookback."""
        from datetime import date, datetime, timedelta
        from .quotes import fetch_benchmark_history, _normalize_symbol_for_yf

        have = list(self.dated_closes_by_underlying.get(underlying) or [])
        if len(have) >= RV_WINDOW + 40:
            return have
        end = date.today()
        start = end - timedelta(days=RICHNESS_HISTORY_DAYS + 45)
        rows = fetch_benchmark_history(
            _normalize_symbol_for_yf(underlying, "stock", "USD"),
            datetime(start.year, start.month, start.day),
            datetime(end.year, end.month, end.day),
        )
        dated = [(day.isoformat(), price) for day, price in rows]
        if dated:
            self.dated_closes_by_underlying[underlying] = dated
            self.closes_by_underlying[underlying] = [price for _, price in dated]
        return dated or have

    def _contract_iv_map(self, underlying: str, snaps: list, dated: list) -> dict:
        latest = snaps[-1] if snaps else None
        atm = _select_atm_pair(latest)
        if not atm:
            return {}
        from datetime import date, datetime, timedelta
        from .quotes import fetch_option_daily_closes

        end = date.today()
        start = end - timedelta(days=RICHNESS_HISTORY_DAYS + 5)
        start_dt = datetime(start.year, start.month, start.day)
        end_dt = datetime(end.year, end.month, end.day)
        call_h = fetch_option_daily_closes(str(atm.get("call_symbol") or ""), start_dt, end_dt)
        put_h = fetch_option_daily_closes(str(atm.get("put_symbol") or ""), start_dt, end_dt)
        if not call_h and not put_h:
            return {}
        return invert_contract_iv_series(
            dated,
            strike=atm["strike"],
            expiry=atm["expiry"],
            call_closes=[(day.isoformat(), price) for day, price in call_h],
            put_closes=[(day.isoformat(), price) for day, price in put_h],
            r=self.r,
        )

    def _open_richness_row(self, row: int) -> None:
        if row < 0 or row >= len(self._table_underlyings):
            return
        underlying = self._table_underlyings[row]
        snaps = load_options_daily_snapshots(underlying)
        dated = self._fetch_dated_closes_now(underlying)
        points = richness_series(
            snaps,
            r=self.r,
            dated_closes=dated,
            contract_iv_by_date=self._contract_iv_map(underlying, snaps, dated),
        )
        self.app.push_screen(OptionRichnessHistoryScreen(underlying, points))

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        if event.data_table.id != "ow-portfolio":
            return
        if event.coordinate.column == self.RICHNESS_COLUMN_INDEX:
            return
        from textual.coordinate import Coordinate
        event.data_table.cursor_coordinate = Coordinate(
            event.coordinate.row, self.RICHNESS_COLUMN_INDEX
        )

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        if event.data_table.id != "ow-portfolio":
            return
        self._open_richness_row(event.coordinate.row)

    def action_clear_cache(self) -> None:
        """重抓「今日」快照——只移除目前清單各標的今天那一筆，保留歷史累積後重新抓取。
        移除今天的快照後 options_symbol_fresh() 轉 False，run_background_fetch()
        便會重新抓當天最新資料再 append 回去。"""
        from .storage import remove_options_daily_snapshot, taiwan_now
        today = taiwan_now().strftime("%Y-%m-%d")
        for u in self.underlyings:
            remove_options_daily_snapshot(u, today)
        self._set_header("[dim]重抓今日快照…[/dim]")
        self.run_background_fetch()


# ─────────────────────────────────────────────────────────────────────────────
# Calibration Status Screen (訊號回測校準狀態)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Options Watchlist Help Screen (期權觀察清單 — 各項數值說明)
# ─────────────────────────────────────────────────────────────────────────────

_OPTIONS_HELP_TEXT = """[bold]期權 — 各項數值說明[/bold]

這頁顯示各標的已觀察到的預期波動、ATM 權利金相對已實現波動的貴賤，以及你持有選擇權的淨風險，[bold]不預測股價漲跌[/bold]。

[bold yellow]── 一、上方：各標的期權分析總表 ──[/bold yellow]
• [bold]預期波動 ±1σ (~30 DTE)[/bold]：市場定價「到期之前大約會 ±多少」的 ±1 個標準差。取 DTE 最接近 30 天的到期日，以 [bold]現價 × ATM IV × √(DTE/365)[/bold] 計算（年化、無方向）。
   [dim]價格一律優先用買賣中間價 (bid+ask)/2；若無雙邊報價、價差過寬、或最後成交價過期，會退回並標 [yellow]⚠[/yellow]（低可信度）。[/dim]
• [bold]ATM IV / RV[/bold]：價平隱含波動 vs 近 20 個交易日已實現波動（對數報酬年化）。IV 明顯高於 RV 代表權利金相對近期實際波動偏貴。
• [bold]波動貴賤[/bold]：ATM IV − RV。差距 ≥ +3 個百分點為偏貴，≤ −3 為偏便宜，其間為合理。這是選擇權定價，[bold]不是[/bold]標的漲跌預測。
   [dim]游標停在此欄：↑↓ 選標的，Enter 開啟近 90 個日曆天的每日走勢。每天的 RV 是「當日往前 20 個交易日」的已實現波動；ATM IV 優先用當日快照，沒有快照則用目前價平合約的歷史價格反解（標 *）。超過 90 天的期權快照會刪除。[/dim]
   [dim]財報剩 N 天／財報今日：下次財報剩餘日曆天數 < 10 才顯示，避免把財報前的 IV 溢價當成平常的「偏貴」。[/dim]
• [bold]Call溢價 / Put溢價 / 跨式溢價[/bold]：市價中間價減去「把已實現波動當波動輸入」的 Black–Scholes 理論價。正數＝市場要的權利金高於該模型。若未來已實現波動剛好等於近20日 RV，這段差額是賣方視角的[bold]指示性模型差[/bold]，不是已驗證的超額報酬、也不是股價預測。
• [bold]持倉Δ$ / Θ/日 / Vega[/bold]：[magenta]◆[/magenta] 代表你持有該標的選擇權，這三欄是你「該標的選擇權部位」的淨風險（只算選擇權、不含現股）：
   [dim]Δ$（Delta 美元）＝標的每動 1%，你這部位大約賺/賠多少；Θ/日＝每過一天因時間價值流失賺/賠多少（買方通常為負）；Vega＝IV 每升 1 個百分點賺/賠多少。末列為你所有持倉選擇權的合計。[/dim]
• 觀察標的來源：[cyan]持倉自動帶入[/cyan]不可刪；按 [bold]a[/bold] 手動加入的標的可按 [bold]d[/bold] 刪除。

[bold yellow]── 二、快速鍵 ──[/bold yellow]
[bold]↑↓[/bold] 在波動貴賤欄選擇標的　[bold]Enter[/bold] 看該檔 ATM IV − RV 走勢　[bold]a[/bold] 新增標的　[bold]d[/bold] 刪除自訂標的　[bold]h[/bold] 本說明　[bold]c[/bold] 重抓今日快照（只刷新今天、保留歷史累積）　[bold]Esc[/bold] 返回

[bold yellow]── 三、資料來源與限制（重要）──[/bold yellow]
所有數字 100% 來自每日真實抓取並累積的期權鏈快照，[bold]不回填、不捏造[/bold]；資料不足時會誠實顯示「資料收集中」。
限制：yfinance 的權利金常是過時成交價、已實現波動用近20日現貨（不是對未來波動的保證）。歐洲式 BS 未處理提前履約與股利，模型差只是指示性數字。預期波動與貴賤都不是股價漲跌預測。

[dim]按 Esc 或 q 返回期權觀察清單。[/dim]
"""


class OptionRichnessHistoryScreen(Screen):
    """單檔每日 ATM IV − RV 走勢（波動貴賤明細）。"""

    BINDINGS = [
        Binding("escape", "go_back", "返回清單"),
        Binding("q",      "go_back", "返回清單", show=False),
    ]

    DEFAULT_CSS = """
    OptionRichnessHistoryScreen { background: #0d1117; layout: vertical; }
    #orh-body { height: 1fr; padding: 1 2; }
    #orh-static { height: auto; }
    """

    def __init__(self, underlying: str, points: list) -> None:
        super().__init__()
        self.underlying = underlying
        self.points = points

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="orh-body"):
            yield Static(format_richness_history(self.underlying, self.points), id="orh-static")
        yield Footer()

    def on_mount(self) -> None:
        body = self.query_one("#orh-body")
        body.can_focus = True
        body.focus()

    def action_go_back(self) -> None:
        self.dismiss()


class OptionsHelpScreen(Screen):
    """期權觀察清單各項數值的詳細說明頁（bug#00076）。"""

    BINDINGS = [
        Binding("escape", "go_back", "返回"),
        Binding("q",      "go_back", "返回", show=False),
    ]

    DEFAULT_CSS = """
    OptionsHelpScreen { background: #0d1117; layout: vertical; }
    #help-body { height: 1fr; padding: 1 2; }
    #help-static { height: auto; }
    """

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="help-body"):
            yield Static(_OPTIONS_HELP_TEXT, id="help-static")
        yield Footer()

    def on_mount(self) -> None:
        panel = self.query_one("#help-body")
        panel.can_focus = True
        panel.focus()

    def action_go_back(self) -> None:
        self.dismiss()


# ─────────────────────────────────────────────────────────────────────────────
# AssetTrack App
# ─────────────────────────────────────────────────────────────────────────────

class AssetTrackApp(App):
    """AssetTrack 全螢幕 Textual TUI 應用主體。"""

    TITLE     = "AssetTrack"
    SUB_TITLE = "即時投資組合看板"

    CSS = """
    Screen {
        background: #0d1117;
        layout: vertical;
    }

    #tui-header {
        height: 1;
        padding: 0 1;
    }

    Footer {
        background: #161b22;
    }

    #main-layout {
        height: 1fr;
    }

    #content-area {
        background: #0d1117;
        layout: vertical;
    }

    #metrics-row {
        width: 100%;
        height: auto;
        padding: 0 1;
    }

    #broker-dist {
        width: 100%;
        height: auto;
        padding: 0 1;
    }

    #holdings-label {
        height: 1;
        padding: 0 2;
    }

    #holdings-row {
        height: 1fr;
        min-height: 14;
        padding: 0 1;
    }

    #holdings-scroll {
        width: 1fr;
        height: 100%;
        padding: 0 1;
        border: solid #21262d;
    }

    #holdings-table {
        height: 100%;
    }

    #recent-events-panel {
        width: 36;
        height: auto;
        margin-left: 1;
    }

    #recommendations-scroll {
        height: auto;
        min-height: 5;
        max-height: 8;
        overflow-x: hidden;
        overflow-y: auto;
    }

    #sector-consensus-panel,
    #options-flow-panel,
    #etf-conclusions-panel {
        width: 1fr;
        height: auto;
        padding: 0 1;
    }

    #status-bar {
        height: 1;
        background: #161b22;
        color: #8b949e;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        user: str = "default",
        positions: Optional[list[Position]] = None,
        cash_positions: Optional[list[CashPosition]] = None,
        rate: float = 32.5,
    ) -> None:
        super().__init__()
        self.default_user = user
        self._user = user
        self._positions = positions if positions is not None else []
        self._cash_positions = cash_positions if cash_positions is not None else []
        self._rate = rate
        self._fetch_activity: dict = {}  # bug#00096: 常駐狀態列的目前抓取項目

    def on_mount(self) -> None:
        if self._positions or self._cash_positions:
            self._start_dashboard(self._user, self._positions, self._cash_positions)
        else:
            self.push_screen(LoginScreen(default_user=self.default_user), self._handle_login_complete)

    def _handle_login_complete(self, result: Optional[tuple[str, list[Position], list[CashPosition]]]) -> None:
        if result is None:
            self.exit()
            return
            
        user, positions, cash_positions = result
        self._user = user
        self._positions = positions
        self._cash_positions = cash_positions
        
        if not positions and not cash_positions:
            self.push_screen(OnboardingModal(), lambda choice: self._handle_onboarding_choice(choice, user))
        else:
            self._start_dashboard(user, positions, cash_positions)

    def _handle_onboarding_choice(self, choice: str, user: str) -> None:
        if choice == "sample":
            sample_positions = [
                Position(
                    broker="manual",
                    symbol="AAPL",
                    instrument_type="stock",
                    quantity=50.0,
                    avg_cost=185.0,
                    currency="USD",
                    source="interactive",
                    last_updated=datetime.utcnow()
                ),
                Position(
                    broker="manual",
                    symbol="TSLA",
                    instrument_type="stock",
                    quantity=10.0,
                    avg_cost=240.0,
                    currency="USD",
                    source="interactive",
                    last_updated=datetime.utcnow()
                )
            ]
            save_manual_positions(sample_positions, [], user=user)
            self.notify("已為您成功建立 AAPL (50股) 與 TSLA (10股) 預設範例部位！")
            self._start_dashboard(user, sample_positions, [])
        elif choice == "manual":
            self.push_screen(AddPositionModal(), lambda pos: self._handle_first_position(pos, user))
        else:
            self._start_dashboard(user, [], [])

    def _handle_first_position(self, result: Optional[list[Holding]], user: str) -> None:
        if result:
            positions = [item for item in result if isinstance(item, Position)]
            cash_positions = [
                item for item in result if isinstance(item, CashPosition)
            ]
            save_manual_positions(positions, cash_positions, user=user)
            n = len(result)
            self.notify(f"已新增 {n} 筆持倉！" if n > 1 else "新增持倉成功！")
            self._start_dashboard(user, positions, cash_positions)
        else:
            self._start_dashboard(user, [], [])

    def _start_dashboard(self, user: str, positions: list[Position], cash_positions: list[CashPosition] = None) -> None:
        rate = 32.0
        try:
            rate = _get_cached_usdtwd_rate()
        except Exception:
            pass
        self._rate = rate
        self._positions = positions
        self._cash_positions = cash_positions if cash_positions is not None else []

        if not getattr(self, "_bg_refresh_timer_started", False):
            self._bg_refresh_timer_started = True
            # bug#00061 follow-up (user decision): keep ETF/期權 daily snapshots
            # accumulating even while the user stays on Dashboard (or any other
            # screen) across a Taiwan-day boundary, instead of only refreshing
            # when 「主動式ETF排行」/「期權觀察清單」are actually opened. Interval
            # is deliberately loose (30 min) — the underlying freshness checks
            # (etf_symbol_cache_fresh/options_symbol_fresh) are idempotent, so
            # frequent no-op checks cost nothing; the real fetch only actually
            # fires once, shortly after each Taiwan-time day rollover. Guarded
            # so a logout→login cycle doesn't stack up duplicate timers on the
            # same long-lived App instance.
            self.set_interval(1800, self._background_data_refresh)

        # Research snapshots wait until the first live quote refresh finishes so
        # Yahoo is not stampeded at login. The 30-minute timer still runs.
        self._research_ingest_kicked = False

        self.push_screen(DashboardScreen(user, positions, self._cash_positions, self._rate), self._handle_dashboard_exit)

    def _kickoff_research_ingest_once(self) -> None:
        """Start ETF/options/sector ingest after the first quote refresh."""
        if getattr(self, "_research_ingest_kicked", False):
            return
        self._research_ingest_kicked = True
        self._background_data_refresh()

    @work(thread=True, exclusive=True)
    def _background_data_refresh(self) -> None:
        """screen-agnostic periodic maintenance: top up today's (Taiwan time)
        ETF holdings + options-chain snapshots regardless of which screen is
        currently mounted. Calls the same pure fetch-and-persist functions
        ActiveETFsScreen/OptionsWatchlistScreen use on their own on_mount
        (bug#00061 follow-up) — no UI to update here; the Dashboard's existing
        60s refresh and any screen's own on_mount already re-read the
        resulting data fresh from disk."""
        try:
            from .storage import etf_symbol_cache_fresh, options_symbol_fresh, load_manual_positions

            universe_result = ensure_active_etf_universe()
            all_symbols = [
                item["symbol"] for item in universe_result.get("records", [])
            ]
            stale_etf = [sym for sym in all_symbols if not etf_symbol_cache_fresh(sym)]
            if stale_etf:
                self._set_fetch_active('etf', f'主動式ETF持股（{len(stale_etf)} 檔）')
                _fetch_and_cache_etf_symbols(stale_etf)
                self._clear_fetch_active('etf')

            positions, _ = load_manual_positions(user=self._user)
            underlyings, _, _ = _watchlist_underlyings(self._user, positions)
            stale_opt = [u for u in underlyings if not options_symbol_fresh(u)]
            if stale_opt:
                self._set_fetch_active('options', f'期權鏈（{len(stale_opt)} 檔標的）')
                _fetch_and_cache_options_underlyings(stale_opt)
                self._clear_fetch_active('options')

            # 類股板塊分析：依市場時段的快取新鮮度決定是否補抓（開盤中 60s、收盤後一次），
            # 讓 Dashboard 類股共識卡片即使未進入板塊頁也保持最新 (bug#00080)。
            from .storage import load_sector_groups, sector_cache_needs_refresh
            sector_groups = load_sector_groups(self._user)
            if sector_groups:
                if sector_cache_needs_refresh(self._user):
                    self._set_fetch_active('sector', '類股板塊成分股')
                    _fetch_and_cache_sector_groups(self._user)
                    self._clear_fetch_active('sector')

        except Exception:
            # The refresh is best-effort and must never surface a modal, but a
            # network failure remains logged so missing cache updates are
            # diagnosable.
            logging.getLogger(__name__).exception(
                "background data refresh aborted"
            )
        finally:
            for _k in ('etf', 'options', 'sector'):
                self._clear_fetch_active(_k)

    def _set_fetch_active(self, key: str, label: str) -> None:
        """bug#00096 常駐狀態列：登記一項正在進行的背景抓取（GIL 下的簡單字典寫入）。"""
        try:
            self._fetch_activity[key] = label
        except Exception:
            pass

    def _clear_fetch_active(self, key: str) -> None:
        try:
            self._fetch_activity.pop(key, None)
        except Exception:
            pass

    def _handle_dashboard_exit(self, should_logout: bool) -> None:
        if should_logout:
            lock_vault()
            self.notify("🚪 已安全登出！")
            self.push_screen(LoginScreen(default_user=self.default_user), self._handle_login_complete)
        else:
            lock_vault()
            self.exit()


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_tui_dashboard(user: str) -> None:
    """
    啟動 AssetTrack Textual TUI 看板。
    """
    app = AssetTrackApp(user=user)
    app.run()


def _load_environment(env_path: Optional[Path] = None) -> Optional[Path]:
    """Load AssetTrack's .env from a deterministic path without overriding exports."""
    from dotenv import load_dotenv
    import sys

    if env_path is not None:
        candidates = [Path(env_path)]
    else:
        candidates = [
            Path(__file__).resolve().parent.parent / ".env",
            Path(sys.executable).resolve().parent / ".env",
            Path.cwd() / ".env",
        ]

    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            load_dotenv(dotenv_path=resolved, override=False)
            return resolved
    return None


def main() -> None:
    """
    套件命令列進入點（取代已移除的 cli.py/Typer 層）。

    整個系統只有單一功能（啟動 TUI 看板）與單一選項（--user/-u），
    不需要 Typer 的子指令框架，改用標準函式庫 argparse 即可。
    對應 `pyproject.toml` 的 `[project.scripts]` 與 `entrypoint.py`。
    """
    import argparse

    _load_environment()

    parser = argparse.ArgumentParser(
        prog="assettrack",
        description="AssetTrack 投資組合追蹤器 — 全功能 TUI 介面。",
    )
    parser.add_argument(
        "--user", "-u", default="default", help="指定使用者帳戶"
    )
    args = parser.parse_args()
    run_tui_dashboard(args.user)


if __name__ == "__main__":
    main()
