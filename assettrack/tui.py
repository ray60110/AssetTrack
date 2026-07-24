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

import time
from datetime import datetime
import calendar
from typing import Optional
from pathlib import Path
import subprocess
import keyring

from rich.box import Box as RichBox
from rich.panel import Panel
from rich.table import Table

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Static, DataTable, OptionList, Input, Select, Label, TabbedContent, TabPane
from textual.widgets.option_list import Option

from .models import Position, CashPosition
from .quotes import (
    enrich_positions_with_quotes, fetch_usdtwd_rate, fetch_beta,
    draw_bar, nearest_price, is_market_open,
    SOX_TICKERS, group_positions_by_broker, fetch_earnings_calendar,
    fetch_active_etf_performance, fetch_etf_holdings,
    fetch_prices_batch, estimate_shares,
)
from .storage import (
    load_manual_positions, save_manual_positions, KEYCHAIN_SERVICE,
    load_active_etf_data, save_active_etf_holdings, etf_cache_needs_refresh,
    load_etf_symbol_cache, save_etf_symbol_cache, etf_symbol_cache_fresh,
    load_aum_perf_cache, save_aum_perf_cache, aum_perf_cache_fresh,
    cleanup_old_etf_caches,
    append_etf_daily_snapshot, load_etf_daily_snapshots, prune_etf_history,
    load_options_daily_snapshots, prune_options_history,
)
from .analysis import (compute_symbol_trends, rank_symbol_trends, generate_etf_conclusions,
    generate_etf_recommendations, etf_stance_recommendation,
    backtest_etf_consensus, compute_etf_selection_tilt, etf_stance_phrase,
    backtest_etf_selection_tilt, etf_backtest_note)
from .options_analysis import (
    compute_options_flow, generate_options_conclusions,
    compute_iv_divergence, generate_divergence_conclusions, build_contract_view,
    generate_grouped_analysis_card, generate_options_recommendations, compute_iv_percentile,
    compute_portfolio_greeks,
    compute_expected_move, compute_directional_verdicts, generate_verdict_cards,
)
from .shared import Recommendation, dashboard_line, render_detail_recs

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


def _get_cached_usdtwd_rate() -> float:
    global _last_rate, _last_rate_time
    now = time.time()
    if _last_rate is None or (now - _last_rate_time) > 3600:
        try:
            rate = fetch_usdtwd_rate()
            if rate > 0:
                _last_rate = rate
                _last_rate_time = now
        except Exception:
            if _last_rate is not None:
                return _last_rate
            raise
    return _last_rate



def _calc_weights(positions: list[Position], rate: float) -> dict:
    total = sum(p.value if p.currency == "USD" else p.value / rate for p in positions)
    weights: dict = {}
    for p in positions:
        v = p.value if p.currency == "USD" else p.value / rate
        key = (p.broker, p.account or "", p.symbol)
        weights[key] = (v / total * 100) if total > 0 else 0.0
    return weights


# ─────────────────────────────────────────────────────────────────────────────
# Rich renderable builders (return renderables, never print)
# ─────────────────────────────────────────────────────────────────────────────

def _build_metrics_panel(positions: list[Position], rate: float) -> Table:
    """3-panel metrics row as a Rich Table (Portfolio Value, PnL, Beta)."""
    total_usd = 0.0
    total_cost_usd = 0.0
    has_cost = False
    has_quotes = any(p.market_price is not None or p.market_value is not None for p in positions)

    for p in positions:
        v = p.value if p.currency == "USD" else p.value / rate
        total_usd += v
        if p.total_cost is not None:
            c = p.total_cost if p.currency == "USD" else p.total_cost / rate
            total_cost_usd += c
            has_cost = True

    pnl_usd = (total_usd - total_cost_usd) if (has_cost and has_quotes) else None
    pnl_pct = (
        (pnl_usd / total_cost_usd * 100)
        if (pnl_usd is not None and total_cost_usd > 0) else None
    )

    # Weighted portfolio beta
    b_num = 0.0
    b_den = 0.0
    for p in positions:
        beta = fetch_beta(p.symbol, p.instrument_type, p.underlying, p.currency)
        if beta is not None:
            v = p.value if p.currency == "USD" else p.value / rate
            b_num += beta * v
            b_den += v
    portfolio_beta = (b_num / b_den) if (b_den > 0 and has_quotes) else None

    tbl = Table(box=None, padding=(0, 1), show_header=False, expand=True)
    for ratio in (3, 3, 2):
        tbl.add_column(justify="center", ratio=ratio)

    # Panel 1 – Total Value
    if has_quotes:
        p1 = Panel(
            f"[bold green]${total_usd:,.2f} USD[/bold green]\n"
            f"[dim]NT${total_usd * rate:,.2f} TWD[/dim]\n"
            f"[dim]USDTWD: {rate:.2f}[/dim]",
            title="📊 Total Portfolio Value",
            border_style="green",
        )
    else:
        p1 = Panel(
            "[yellow]⏳ 載入報價中...[/yellow]\n"
            f"[dim]USDTWD: {rate:.2f}[/dim]",
            title="📊 Total Portfolio Value",
            border_style="yellow",
        )

    # Panel 2 – Unrealized PnL
    if has_quotes and pnl_usd is not None and pnl_pct is not None:
        c = "green" if pnl_usd >= 0 else "red"
        s = "+" if pnl_usd >= 0 else ""
        p2 = Panel(
            f"[{c} bold]{s}${pnl_usd:,.2f}[/{c} bold]\n[{c}]{s}{pnl_pct:.2f}%[/{c}]",
            title="📈 Unrealized P&L",
            border_style=c,
        )
    elif not has_quotes:
        p2 = Panel(
            "[yellow]⏳ 載入中...[/yellow]",
            title="📈 Unrealized P&L",
            border_style="yellow",
        )
    else:
        p2 = Panel(
            "[dim]—[/dim]\n[dim]無成本資料[/dim]",
            title="📈 Unrealized P&L",
            border_style="dim",
        )

    # Portfolio Beta
    if has_quotes and portfolio_beta is not None:
        bc = "green" if portfolio_beta <= 0.8 else ("yellow" if portfolio_beta <= 1.2 else "red")
        p5 = Panel(
            f"[{bc} bold]{portfolio_beta:.2f}[/{bc} bold]\n[dim]vs SPY[/dim]",
            title="⚡ Portfolio Beta",
            border_style=bc,
        )
    elif not has_quotes:
        p5 = Panel(
            "[yellow]⏳ 載入中...[/yellow]",
            title="⚡ Portfolio Beta",
            border_style="yellow",
        )
    else:
        p5 = Panel(
            "[dim]—[/dim]\n[dim]資料不足[/dim]",
            title="⚡ Portfolio Beta",
            border_style="dim",
        )

    tbl.add_row(p1, p2, p5)
    return tbl


def _build_holdings_table(
    positions: list[Position], rate: float, weights: dict
) -> Table:
    """Broker-grouped holdings as a Rich Table (matches cli.py visual style)."""
    tbl = Table(
        box=_SEC_BOX,
        padding=(0, 2, 0, 1),
        show_header=True,
        header_style="bold dim",
        expand=True,
    )
    tbl.add_column("Symbol",        style="bold white", min_width=8,  no_wrap=True)
    tbl.add_column("Type",          style="dim",         min_width=6,  no_wrap=True)
    tbl.add_column("Qty",           justify="right",     min_width=6)
    tbl.add_column("Avg Cost",      justify="right",     min_width=9)
    tbl.add_column("Price",         justify="right",     min_width=9)
    tbl.add_column("Market Value",  justify="right",     style="bold", min_width=13)
    tbl.add_column("Wt%",           justify="right",     style="dim",  min_width=5)
    tbl.add_column("今日%",         justify="right",     min_width=8)
    tbl.add_column("今日漲跌",      justify="right",     min_width=11)
    tbl.add_column("市場",          justify="center",    min_width=6)
    tbl.add_column("Unrealized P&L",justify="right",     min_width=18)

    n_cols = 11

    has_quotes = any(p.market_price is not None or p.market_value is not None for p in positions)

    sorted_brokers = group_positions_by_broker(positions, rate)

    for i, (bk, bk_pos) in enumerate(sorted_brokers):
        bk_total = sum(
            p.value if p.currency == "USD" else p.value / rate for p in bk_pos
        )
        if i > 0:
            tbl.add_row(*[""] * n_cols, end_section=False)

        # Broker header row
        bk_total_s = f"[bold white]${bk_total:,.0f}[/bold white] [dim]USD[/dim]" if has_quotes else "[dim]—[/dim]"
        header = (
            [f"[bold cyan]▐  {bk.upper()}[/bold cyan]"]
            + [""] * (n_cols - 2)
            + [bk_total_s]
        )
        tbl.add_row(*header, style="cyan", end_section=True)

        for p in bk_pos:
            qty_s   = f"{p.quantity:,.2f}" if p.quantity % 1 != 0 else f"{int(p.quantity):,}"
            cost_s  = f"${p.avg_cost:,.2f}" if p.avg_cost is not None else "[dim]—[/dim]"
            price_s = f"${p.market_price:,.2f}" if p.market_price is not None else "[dim]—[/dim]"
            val_s   = f"${p.value:,.2f}" if (p.market_price is not None or p.market_value is not None) else "[dim]—[/dim]"
            mkt_s   = "[green]開市[/green]" if is_market_open(p) else "[dim]休市[/dim]"

            d_chg = p.daily_change
            d_pct = p.daily_change_pct
            if d_chg is not None and d_pct is not None:
                dc  = "green" if d_chg >= 0 else "red"
                ds  = "+" if d_chg >= 0 else ""
                ccy = "" if p.currency == "USD" else f" {p.currency}"
                dpct_s = f"[{dc}]{ds}{d_pct:.2f}%[/{dc}]"
                dchg_s = f"[{dc}]{ds}{d_chg:,.0f}{ccy}[/{dc}]"
            else:
                dpct_s = dchg_s = "[dim]—[/dim]"

            key  = (p.broker, p.account or "", p.symbol)
            wt_s = f"{weights.get(key, 0.0):.1f}%" if has_quotes else "[dim]—[/dim]"

            pnl = p.unrealized_pnl
            pct = p.unrealized_pnl_pct
            if pnl is not None and pct is not None:
                pc    = "green" if pnl >= 0 else "red"
                ps    = "+" if pnl >= 0 else ""
                pnl_s = f"[{pc}]{ps}${pnl:,.2f}[/{pc}] [dim]({ps}{pct:.2f}%)[/dim]"
            else:
                pnl_s = "[dim]—[/dim]"

            tbl.add_row(
                p.symbol, p.instrument_type, qty_s, cost_s, price_s,
                val_s, wt_s, dpct_s, dchg_s, mkt_s, pnl_s,
                end_section=False,
            )

    return tbl


def _build_broker_panel(positions: list[Position], rate: float) -> Panel:
    has_quotes = any(p.market_price is not None or p.market_value is not None for p in positions)
    if not has_quotes:
        return Panel("\n [yellow]⏳ 載入中...[/yellow]", title="🏦 券商資產分布", border_style="cyan")
    total = sum(p.value if p.currency == "USD" else p.value / rate for p in positions)
    broker_vals: dict[str, float] = {}
    for p in positions:
        bk = f"{p.broker} ({p.account})" if p.account else p.broker
        broker_vals[bk] = broker_vals.get(bk, 0.0) + (
            p.value if p.currency == "USD" else p.value / rate
        )
    max_bv = max(broker_vals.values()) if broker_vals else 1.0
    lines = []
    for bk, bv in sorted(broker_vals.items(), key=lambda x: -x[1]):
        bar = draw_bar(bv, max_bv, 12)
        pct = (bv / total * 100) if total > 0 else 0.0
        lines.append(
            f"[cyan]{bk:<22}[/cyan] [green]{bar}[/green]  "
            f"[bold]${bv:,.0f}[/bold] [dim]({pct:.1f}%)[/dim]"
        )
    return Panel("\n".join(lines), title="🏦 券商資產分布", border_style="cyan")


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
        emoji = m.group(1)
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


def _get_event_type(label: str) -> str:
    if "FED" in label or "NFP" in label or "CPI" in label or "利率" in label or "非農" in label or "通膨" in label:
        return "MACRO"
    if "持倉/SOX" in label or ("持倉" in label and "SOX" in label):
        return "PORTFOLIO_SOX"
    if "持倉" in label:
        return "PORTFOLIO"
    if "SOX" in label:
        return "SOX"
    return "OTHER"


def _event_is_completed(event_date, label: str, today) -> bool:
    """Determine completion without treating a not-yet-released same-day event as done."""
    return "(已發生)" in label or event_date < today


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


def _event_card(event_date, label: str, today, event_type: str):
    """Render one consistently aligned event card with a non-color status cue."""
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    completed = _event_is_completed(event_date, label, today)
    clean_label = label.replace("[bold red](已發生)[/bold red]", "").replace("  ", " ")
    label_lines = clean_label.splitlines()

    if completed:
        # Inline semantic colors are tuned for the dark theme and lose contrast
        # on the requested light-gray completed state, so completed copy is
        # intentionally flattened to a single dark foreground.
        title = Text.from_markup(label_lines[0]).plain
        details = [Text.from_markup(line).plain for line in label_lines[1:]]
        panel_style = "black on #d1d5db"
        border_style = "#9ca3af"
        date_style = "bold #374151"
        status = Text("✓ 已發生", style="bold #374151")
    else:
        title = Text.from_markup(label_lines[0])
        details = [Text.from_markup(line) for line in label_lines[1:]]
        panel_style = "white on #161b22"
        border_style = {
            "PORTFOLIO": "#3fb950",
            "PORTFOLIO_SOX": "#3fb950",
            "SOX": "#d29922",
            "MACRO": "#58a6ff",
        }.get(event_type, "#8b949e")
        date_style = "bold #c9d1d9"
        status = Text("○ 待發生", style="bold #58a6ff")

    content = Table.grid(expand=True, padding=(0, 1))
    content.add_column(width=15, no_wrap=True, vertical="top")
    content.add_column(width=10, no_wrap=True, vertical="top")
    content.add_column(ratio=1, overflow="fold", vertical="top")
    date_text = Text(
        f"{event_date.strftime('%m-%d')} · {_event_relative_label(event_date, today)}",
        style=date_style,
    )
    content.add_row(date_text, status, title)
    for detail in details:
        content.add_row("", "", detail)

    card = Panel(
        content,
        border_style=border_style,
        style=panel_style,
        padding=(0, 1),
        expand=True,
    )
    return Padding(card, (0, 0, 1, 0))


def _render_monthly_calendar(year: int, month: int, month_events: list, today) -> Table:
    import calendar
    from rich.console import Group
    from rich.text import Text

    day_to_events = {}
    for d, label in month_events:
        ev_type = _get_event_type(label)
        completed = _event_is_completed(d, label, today)
        day_to_events.setdefault(d.day, []).append((label, ev_type, completed))
        
    cal = calendar.Calendar(firstweekday=6) # Sunday starts
    weeks = cal.monthdayscalendar(year, month)
    
    grid_lines = []
    grid_lines.append("[bold cyan]日 一 二 三 四 五 六[/bold cyan]")
    grid_lines.append("┈" * 10)
    
    for week in weeks:
        week_str = []
        for day in week:
            if day == 0:
                week_str.append("  ")
            else:
                if day in day_to_events:
                    evs = day_to_events[day]
                    types = [e[1] for e in evs]
                    all_completed = all(e[2] for e in evs)
                    if all_completed:
                        week_str.append(f"[black on #d1d5db]{day:2d}[/black on #d1d5db]")
                    elif "PORTFOLIO_SOX" in types or ("PORTFOLIO" in types and "SOX" in types):
                        color = "green"
                        week_str.append(f"[{color} reverse]{day:2d}[/{color} reverse]")
                    elif "PORTFOLIO" in types:
                        week_str.append(f"[green reverse]{day:2d}[/green reverse]")
                    elif "MACRO" in types:
                        week_str.append(f"[cyan reverse]{day:2d}[/cyan reverse]")
                    else:
                        week_str.append(f"[yellow reverse]{day:2d}[/yellow reverse]")
                else:
                    week_str.append(f"{day:2d}")
        grid_lines.append(" ".join(week_str))
        
    grid_content = "\n".join(grid_lines)
    
    event_cards = []
    for d, label in sorted(month_events, key=lambda x: x[0]):
        ev_type = _get_event_type(label)
        event_cards.append(_event_card(d, label, today, ev_type))
    if event_cards:
        legend = Text.from_markup(
            "[black on #d1d5db] ✓ 已發生 [/black on #d1d5db]"
            "  [bold #58a6ff]○ 待發生[/bold #58a6ff]"
            "  [dim]灰底代表事件已完成[/dim]"
        )
        events_content = Group(legend, Text(""), *event_cards)
    else:
        events_content = Text("無重要事件", style="dim")
    
    month_name = datetime(year, month, 1).strftime("%Y-%m (%B)")
    tbl = Table(title=f"\n[bold magenta]📅 {month_name}[/bold magenta]", show_header=False, box=None, padding=(0, 1), expand=True)
    tbl.title_align = "left"
    tbl.add_column("Grid", width=24, vertical="top")
    tbl.add_column("Events", vertical="top")
    
    tbl.add_row(
        Panel(grid_content, border_style="dim", title="月曆圖", expand=False),
        Panel(
            events_content,
            border_style="dim",
            title="事件清單 · 依時間排序",
            padding=(0, 1),
            expand=True,
        )
    )
    return tbl


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
            yield Static("✨ AssetTrack", id="login-title")
            yield Static("Unified Portfolio & Asset Tracking System", id="login-subtitle")
            yield Label("👤 請輸入使用者帳號 (User ID):", id="login-input-label")
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
            
        pwd = keyring.get_password(KEYCHAIN_SERVICE, user)
        
        if pwd is None:
            modal = RegisterModal(user)
            self.app.push_screen(modal, lambda success: self._on_register_complete(success, user))
        else:
            self.query_one("#login-error-msg", Label).update("🔍 正在嘗試 Touch ID 登入...")
            self.run_touchid_auth(user)

    @work(thread=True)
    def run_touchid_auth(self, user: str) -> None:
        touchid_helper_path = Path(__file__).parent / "touchid_helper"
        success = False
        if touchid_helper_path.exists():
            try:
                res = subprocess.run([str(touchid_helper_path)], capture_output=True)
                if res.returncode == 0:
                    success = True
            except Exception:
                pass
        self.app.call_from_thread(self._on_touchid_complete, success, user)

    def _on_touchid_complete(self, success: bool, user: str) -> None:
        if success:
            self.query_one("#login-error-msg", Label).update("✅ Touch ID 驗證成功！")
            self._login_success(user)
        else:
            self.query_one("#login-error-msg", Label).update("⚠️ Touch ID 失敗，改用密碼登入。")
            modal = PasswordModal(user)
            self.app.push_screen(modal, lambda login_success: self._on_password_complete(login_success, user))

    def _on_password_complete(self, success: bool, user: str) -> None:
        if success:
            self._login_success(user)
        else:
            self.query_one("#login-error-msg", Label).update("❌ 密碼驗證失敗！")

    def _on_register_complete(self, success: bool, user: str) -> None:
        if success:
            self.query_one("#login-error-msg", Label).update("✅ 註冊成功，密碼已儲存！")
            self._login_success(user)
        else:
            self.query_one("#login-error-msg", Label).update("❌ 取消註冊。")

    def _login_success(self, user: str) -> None:
        positions, cash_positions = load_manual_positions(user=user)
        self.dismiss((user, positions, cash_positions))


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
        self.attempts = 3

    def compose(self) -> ComposeResult:
        with Vertical(id="pwd-dialog"):
            yield Label(f"🔑 請輸入 [bold white]{self.user}[/bold white] 的登入密碼:", id="pwd-msg")
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
        stored_pwd = keyring.get_password(KEYCHAIN_SERVICE, self.user)
        if stored_pwd is not None and val == stored_pwd:
            self.dismiss(True)
        else:
            self.attempts -= 1
            if self.attempts <= 0:
                self.dismiss(False)
            else:
                error_lbl.update(f"❌ 密碼錯誤！還剩 {self.attempts} 次機會。")
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
            yield Label("👤 [bold]註冊新使用者[/bold]", id="reg-title")
            yield Label("系統偵測到您是第一次使用此 ID，請設定登入密碼：", id="reg-desc")
            yield Input(placeholder="輸入密碼", password=True, id="pwd1", classes="reg-field")
            yield Input(placeholder="再次輸入確認密碼", password=True, id="pwd2", classes="reg-field")
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
            error_lbl.update("❌ 密碼不能為空！")
            return
            
        if pwd1 != pwd2:
            error_lbl.update("❌ 兩次輸入密碼不一致！")
            return
            
        keyring.set_password(KEYCHAIN_SERVICE, self.user, pwd1)
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
        border: thick #58a6ff;
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
            yield Label("⚠️ [bold yellow]偵測到您目前尚無任何持倉部位！[/bold yellow]", id="onboard-title")
            yield Label("請選擇以下任一操作來開始使用您的 AssetTrack 看板：", id="onboard-desc")
            yield OptionList(
                Option("1️⃣ 建立預設範例部位 (AAPL, TSLA)", id="sample"),
                Option("2️⃣ 手動新增持倉部位 (逐一輸入商品資訊)", id="manual"),
                Option("3️⃣ 保持空白並直接進入看板", id="empty"),
                id="onboard-list"
            )

    def on_mount(self) -> None:
        self.query_one("#onboard-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss("empty")


class AddPositionModal(ModalScreen[Optional[list[Position]]]):
    """手動新增/修改持股對話框。

    新增模式支援「批次累積」：每筆填完按「儲存並繼續」加入待存清單，
    最後一次「完成儲存」整批回傳 list[Position]；修改模式回傳單元素 list。
    Symbol 輸入時自動推斷市場/幣別（如 2330 或 2330.TW → TW/TWD），
    非必要欄位（帳戶/交易所/幣別/備註/板塊）收於可展開的「進階欄位」區。
    """

    # Ordered list of all focusable field IDs (Inputs + Selects + adv toggle)
    _FIELD_IDS: list[str] = [
        "add-broker", "add-symbol", "add-type",
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
        border: thick #58a6ff;
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

    def __init__(self, position: Optional[Position] = None) -> None:
        super().__init__()
        self.position = position
        self._pending: list[Position] = []   # 批次新增的待存清單（僅新增模式）
        self._adv_visible: bool = False      # 進階欄位是否展開
        # 目前由 Symbol 推斷出的 (market, currency)；僅在使用者未手動改過時才覆寫
        self._inferred: tuple[str, str] = ("US", "USD")

    def compose(self) -> ComposeResult:
        brokers = [("manual", "manual"), ("FT", "FT"), ("IBKR", "IBKR")]
        types   = [("stock", "stock"), ("etf", "etf"), ("option", "option")]
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

        acct_val = p.account if p else ""
        sym_val = p.symbol if p else ""
        
        t_val = "stock"
        if p and p.instrument_type:
            t_lower = p.instrument_type.lower()
            if t_lower in ("stock", "etf", "option"):
                t_val = t_lower

        # Option-specific values
        udl_val = p.underlying if (p and p.underlying) else ""
        strike_val = f"{p.strike}" if (p and p.strike is not None) else ""
        exp_val = p.expiry if (p and p.expiry) else ""
        opt_type_val = p.option_type if (p and p.option_type) else "call"
        mult_val = f"{p.multiplier}" if (p and p.multiplier is not None) else "100"
        
        qty_val = ""
        side_val = "long"
        if p and p.quantity is not None:
            side_val = "short" if p.quantity < 0 else "long"
            abs_qty = abs(p.quantity)
            qty_val = f"{abs_qty:,.2f}" if abs_qty % 1 != 0 else f"{int(abs_qty)}"

        cost_val = ""
        if p and p.avg_cost is not None:
            cost_val = f"{p.avg_cost}"

        m_val = "US"
        if p and p.market:
            m_upper = p.market.upper()
            if m_upper in ("US", "TW", "HK", "OTHER"):
                m_val = m_upper
            else:
                m_val = "other"

        exch_val = p.exchange if p else ""
        curr_val = p.currency if p else "USD"
        notes_val = p.notes if p else ""
        sect_val = p.sector if p else ""

        title = "✏️ [bold]修改持倉部位[/bold]" if p else "➕ [bold]新增持倉部位（可連續多筆）[/bold]"
        btn_label = "確認修改" if p else "完成儲存"

        with Vertical(id="add-dialog"):
            yield Label(title, id="add-title")
            yield Label(
                "💡 [dim]↑↓ 切換欄位　Enter 移至下一欄　[red]★[/red] 必填　[dim]✦ 建議填寫[/dim]",
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

            with Horizontal(classes="form-row"):
                yield Label("持倉方向 [dim](Side)[/dim]:", classes="form-label")
                yield Select(
                    [("Long 多/做多", "long"), ("Short 空/放空", "short")],
                    value=side_val, id="add-side"
                )

            with Horizontal(classes="form-row"):
                yield Label("[red]★[/red] 數量 [dim](Qty)[/dim]:", classes="form-label")
                yield Input(value=qty_val, placeholder="正數，例如 100", id="add-qty",
                            classes="form-input")

            with Horizontal(classes="form-row"):
                yield Label("[yellow]✦[/yellow] 成本 [dim](Cost)[/dim]:", classes="form-label")
                yield Input(value=cost_val, placeholder="正數，例如 150.5（建議填寫）", id="add-cost",
                            classes="form-input")

            with Horizontal(classes="form-row"):
                yield Label("市場 [dim](Market)[/dim]:", classes="form-label")
                yield Select(markets, value=m_val, id="add-market")

            with Horizontal(classes="form-row"):
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
                    yield Button("儲存並繼續 ➕", variant="success", id="confirm-next")
                yield Button(btn_label, variant="primary", id="confirm")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        t_val = self.query_one("#add-type", Select).value
        is_opt = (t_val == "option")
        self.query_one("#option-fields-container").display = is_opt
        # bug#00047: option symbols are fully derived from underlying/strike/expiry/type,
        # so the top-level Symbol field is redundant (and was never actually required) —
        # hide it for option type to avoid asking the user to enter the ticker twice.
        self.query_one("#symbol-field-row").display = not is_opt
        self.query_one("#adv-fields-container").display = False
        self.query_one("#batch-list", Label).display = False
        if self.position:
            # 修改模式：以既有值為推斷基準，避免游標經過 Symbol 時覆寫使用者資料
            m_val = self.query_one("#add-market", Select).value
            c_val = self.query_one("#add-curr", Input).value.strip().upper()
            self._inferred = (str(m_val), c_val)
        if is_opt:
            self.query_one("#add-underlying", Input).focus()
        else:
            self.query_one("#add-symbol", Input).focus()

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
            is_opt = (event.value == "option")
            self.query_one("#option-fields-container").display = is_opt
            self.query_one("#symbol-field-row").display = not is_opt

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

            # Skip option-specific fields if the container is hidden
            opt_container = self.query_one("#option-fields-container")
            is_opt = opt_container.display

            visible_fids = []
            for fid in self._FIELD_IDS:
                if fid in ("add-underlying", "add-strike", "add-expiry", "add-option-type", "add-multiplier"):
                    if is_opt:
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

    def _collect(self) -> Optional[Position]:
        """驗證目前表單內容並組成 Position；失敗時顯示錯誤並回傳 None。"""
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

        error_lbl = self.query_one("#add-error", Label)

        # For option type, symbol may be left blank (auto-generated from underlying/expiry/strike)
        if not symbol and inst_type != "option":
            error_lbl.update("❌ [red]★ 商品代碼[/red] 為必填，請輸入代碼（例如 AAPL）")
            self.query_one("#add-symbol", Input).focus()
            return

        # Option validation
        underlying = None
        strike = None
        expiry = None
        opt_type = None
        multiplier = None

        if inst_type == "option":
            underlying = self.query_one("#add-underlying", Input).value.strip().upper()
            strike_str = self.query_one("#add-strike", Input).value.strip()
            expiry = self.query_one("#add-expiry", Input).value.strip()
            opt_type = self.query_one("#add-option-type", Select).value
            mult_str = self.query_one("#add-multiplier", Input).value.strip()

            if not underlying:
                error_lbl.update("❌ [red]★ 標的代碼[/red] 為必填，請輸入（例如 AAPL）")
                self.query_one("#add-underlying", Input).focus()
                return
            if not strike_str:
                error_lbl.update("❌ [red]★ 履約價[/red] 為必填，請輸入履約價格")
                self.query_one("#add-strike", Input).focus()
                return
            try:
                strike = float(strike_str)
                if strike <= 0:
                    error_lbl.update("❌ 履約價必須大於 0")
                    self.query_one("#add-strike", Input).focus()
                    return
            except ValueError:
                error_lbl.update("❌ 請輸入有效的履約價（數字）")
                self.query_one("#add-strike", Input).focus()
                return
            if not expiry:
                error_lbl.update("❌ [red]★ 到期日[/red] 為必填，請輸入到期日（YYYY-MM-DD）")
                self.query_one("#add-expiry", Input).focus()
                return
            
            import re
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", expiry):
                error_lbl.update("❌ 到期日格式必須為 YYYY-MM-DD，例如 2026-06-19")
                self.query_one("#add-expiry", Input).focus()
                return

            if mult_str:
                try:
                    multiplier = float(mult_str)
                    if multiplier <= 0:
                        error_lbl.update("❌ 合約乘數必須大於 0")
                        self.query_one("#add-multiplier", Input).focus()
                        return
                except ValueError:
                    error_lbl.update("❌ 請輸入有效的合約乘數（數字）")
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
                error_lbl.update(f"❌ 無法自動生成選擇權代碼: {_e}")
                self.query_one("#add-expiry", Input).focus()
                return

        if not qty_str:
            error_lbl.update("❌ [red]★ 持股數量[/red] 為必填，請輸入數量")
            self.query_one("#add-qty", Input).focus()
            return

        try:
            qty = float(qty_str)
            if qty <= 0:
                error_lbl.update("❌ 數量必須大於 0")
                self.query_one("#add-qty", Input).focus()
                return
        except ValueError:
            error_lbl.update("❌ 請輸入有效的數量（數字）")
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
                    error_lbl.update("❌ 平均成本不能為負數")
                    self.query_one("#add-cost", Input).focus()
                    return
            except ValueError:
                error_lbl.update("❌ 請輸入有效的成本（數字）")
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
            error_lbl.update(f"❌ 資料驗證失敗: {e}")
            return None

    def _form_is_empty(self) -> bool:
        """主要輸入欄位（代碼/標的與數量）皆為空 → 視為沒有待送出的表單。"""
        is_opt = (self.query_one("#add-type", Select).value == "option")
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
        items = "、".join(f"{p.symbol}×{abs(p.quantity):g}" for p in shown)
        prefix = "…" if len(self._pending) > 5 else ""
        lbl.update(f"📋 待存清單 ({len(self._pending)})：{prefix}{items}")
        lbl.display = True

    def _reset_entry_fields(self) -> None:
        """加入待存清單後清空「本筆專屬」欄位，保留券商/類型/市場等共通設定。"""
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
            f"[green]✅ 已加入待存清單，可繼續輸入下一筆（或按「完成儲存」寫入全部）[/green]"
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
                    "❌ 尚未輸入任何部位（請先填寫代碼與數量）"
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
            yield Label(f"[bold cyan]部位操作:[/] {self.pos.broker} - {self.pos.symbol}", id="actions-title")
            yield OptionList(
                Option("📝 修改備註 (Notes)", id="notes"),
                Option("🏷️ 修改持倉分類 (Sector)", id="sector"),
                Option("💵 修改計價幣別 (Currency)", id="currency"),
                Option("💱 修改成本幣別 (Cost Currency)", id="cost_currency"),
                Option("🏦 修改券商與帳戶", id="broker_account"),
                Option("🗑️ 移除此持倉 (Delete)", id="delete"),
                Option("❌ 取消", id="cancel"),
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

    def __init__(self, position: Position | list[Position]) -> None:
        super().__init__()
        plist = position if isinstance(position, list) else [position]
        self.positions: list[Position] = plist
        self.position = plist[0]  # 向後相容：單筆呼叫端仍可讀取 .position

    def compose(self) -> ComposeResult:
        descs = [
            f"{p.broker.upper()} - {p.account or 'default'} - {p.symbol} ({p.instrument_type})"
            for p in self.positions
        ]
        shown = descs[:6]
        if len(descs) > 6:
            shown.append(f"…及其他 {len(descs) - 6} 筆")
        desc = "\n".join(f"[cyan]{d}[/]" for d in shown)
        n_note = f"以下 [bold]{len(descs)}[/bold] 筆部位" if len(descs) > 1 else "以下部位"
        with Vertical(id="delete-confirm-dialog"):
            yield Label("⚠️ 刪除確認 (Confirm Deletion)", id="delete-confirm-title")
            yield Label(
                f"您確定要[bold red]完整刪除[/bold red]{n_note}嗎？此操作無法復原：\n\n{desc}",
                id="delete-confirm-msg"
            )
            with Horizontal(id="delete-confirm-buttons"):
                yield Button("確認刪除", variant="error", id="confirm")
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
    """讓畫面上的投資建議可點選『🔍 查看公式細節』連結推入公式細節頁（bug#00118）。
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


def _format_cpi_event_actuals(result: Optional[dict]) -> str:
    if not result:
        return _fred_unavailable_text("CPI", ("CPIAUCSL", "CPIAUCNS"))
    yoy_prev = result.get("prev_yoy_pct")
    mom_prev = result.get("prev_mom_pct")
    as_of = result.get("as_of")
    period = f" {as_of.strftime('%Y-%m')}" if hasattr(as_of, "strftime") else ""
    yoy_cmp = f"前期 {yoy_prev:.2f}%，變動 {result['yoy_pct'] - yoy_prev:+.2f}pp" if yoy_prev is not None else "前期 —"
    mom_cmp = f"前期 {mom_prev:.2f}%，變動 {result['mom_pct'] - mom_prev:+.2f}pp" if mom_prev is not None else "前期 —"
    return (
        f"CPI{period} YoY {result['yoy_pct']:.2f}%（{yoy_cmp}）｜"
        f"MoM {result['mom_pct']:.2f}%（{mom_cmp}）"
    )


def _format_nfp_event_actuals(nfp: Optional[dict], unemployment: Optional[dict]) -> str:
    parts = []
    if nfp:
        current_k = nfp["change"] / 1000.0
        previous = nfp.get("prev_change")
        as_of = nfp.get("as_of")
        period = f" {as_of.strftime('%Y-%m')}" if hasattr(as_of, "strftime") else ""
        if previous is None:
            parts.append(f"NFP{period} {current_k:+,.0f}K（前期 —）")
        else:
            previous_k = previous / 1000.0
            parts.append(
                f"NFP{period} {current_k:+,.0f}K"
                f"（前期 {previous_k:+,.0f}K，變動 {current_k - previous_k:+,.0f}K）"
            )
    else:
        parts.append(_fred_unavailable_text("NFP", ("PAYEMS",)))
    if unemployment:
        parts.append(
            f"失業率 {unemployment['rate_pct']:.1f}%"
            f"（前期 {unemployment['prev_pct']:.1f}%，變動 {unemployment['change_pp']:+.1f}pp）"
        )
    else:
        parts.append(_fred_unavailable_text("失業率", ("UNRATE",)))
    return "｜".join(parts)


def _format_fed_event_actuals(result: Optional[dict]) -> str:
    if not result:
        return _fred_unavailable_text("利率決議", ("DFEDTARU", "DFEDTARL"))
    before = result["range_before"]
    after = result["range_after"]
    return (
        f"目標利率 {after[0]:.2f}–{after[1]:.2f}%"
        f"（前期 {before[0]:.2f}–{before[1]:.2f}%，變動 {result['delta_bps']:+d}bp）"
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
            yield Label("🌐 調整事件顯示時區", id="timezone-title")
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
    """重要日曆事件 Screen (持倉財報、SOX 十大財報、總經重大事件)。"""

    BINDINGS = [
        Binding("t", "adjust_timezone", "調整時區"),
        Binding("escape", "go_back", "返回看板"),
        Binding("q", "go_back", "返回看板", show=False),
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
    #events-holdings-container {
        height: 30%;
        padding: 0 2;
        margin: 1 0;
    }
    #events-holdings-label {
        height: auto;
        margin-bottom: 0;
    }
    #events-holdings-table {
        height: 1fr;
        border: tall #334155;
    }
    #events-holdings-table:focus {
        border: tall $accent;
    }
    #events-calendar-container {
        height: 1fr;
        padding: 0 2;
        layout: vertical;
    }
    #events-calendar-label {
        height: auto;
        margin-bottom: 0;
    }
    #events-right-panel {
        height: 1fr;
        padding: 0;
        border: tall #334155;
    }
    #events-right-panel:focus {
        border: tall $accent;
    }
    #events-static {
        height: auto;
    }
    #events-macro {
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
        border: round #58a6ff;
    }
    #events-macro.hidden { display: none; }
    """

    def __init__(self, user: str, positions: list[Position], rate: float) -> None:
        super().__init__()
        from .storage import load_user_preferences

        self.user = user
        self.positions = positions
        self.rate = rate
        preferences = load_user_preferences(user)
        self.event_timezone = preferences.get("event_timezone") or "Asia/Taipei"
        self._header_status: str = ""
        self._macro_readings_markup: Optional[str] = None
        self._macro_recs: list = []  # bug#00119: 結構化總經指標建議（可點選公式細節）
        self._calendar_month_views: list = []

    def compose(self) -> ComposeResult:
        yield Static("", id="events-header")
        with Vertical(id="events-holdings-container"):
            yield Static(
                "[bold]持有部位[/bold] [dim]Holdings[/dim]",
                id="events-holdings-label",
            )
            yield DataTable(id="events-holdings-table")
        with Vertical(id="events-calendar-container"):
            yield Static(
                "[bold]事件日曆與總經追蹤[/bold] [dim]Events & Macro[/dim]",
                id="events-calendar-label",
            )
            with ScrollableContainer(id="events-right-panel"):
                yield Static("", id="events-macro", classes="hidden")
                yield Static("", id="events-static")
        yield Footer()

    def _update_header(self, status: str) -> None:
        self._header_status = status
        self._render_header()

    def _render_header(self) -> None:
        from rich.panel import Panel
        from rich.console import Group
        from .shared import format_timezone_label

        title_line = (
            f"[bold cyan]📅 近期重大事件[/bold cyan]  "
            f"[dim]│[/dim]  "
            f"{self._header_status}"
        )
        body_lines = [
            title_line,
            (
                f"[dim]顯示時區：{format_timezone_label(self.event_timezone)}"
                "　·　按 T 調整並保存[/dim]"
            ),
        ]
        if self._macro_readings_markup:
            body_lines.append(
                f"[dim]最新總經數據 (FRED):[/dim] {self._macro_readings_markup}"
            )
        body = Group(*body_lines)
        self.query_one("#events-header", Static).update(
            Panel(body, border_style="cyan", padding=(0, 1))
        )

    def _update_events_static(self) -> None:
        from rich.console import Group
        from rich.panel import Panel

        # bug#00119: 總經指標分析改用三層寫作格式，另放 #events-macro（markup 字串，
        # 支援 @click『查看公式細節』）；此處只保留行情日曆的 Rich Table 檢視。
        self._render_macro_recs()

        elements = list(self._calendar_month_views)

        if not elements:
            self.query_one("#events-static", Static).update(
                Panel(
                    "[dim]上個月起至未來 90 天內無重大事件與財報日期[/dim]",
                    title="📅 行情日曆",
                    border_style="dim",
                )
            )
        else:
            self.query_one("#events-static", Static).update(Group(*elements))

    def _render_macro_recs(self) -> None:
        """把重點經濟指標的三層結構化建議 render 成可點選 markup（bug#00119/00118）。"""
        w = self.query_one("#events-macro", Static)
        if not self._macro_recs:
            w.add_class("hidden")
            self._recs_by_id = {}
            return
        w.remove_class("hidden")
        w.border_title = "📊 重點經濟指標期對期變動與動態解析（點『🔍 查看公式細節』看計算）"
        body, mapping = render_detail_recs(self._macro_recs)
        self._recs_by_id = mapping
        w.update(body)

    @work(thread=True)
    def run_macro_readings_fetch(self) -> None:
        """背景抓取各總經指標最新一期已公佈數值（FRED），完成後更新表頭與解析面板。
        缺 API key／資料時 format_macro_readings 回傳 None，不更新表頭（不臆測）。"""
        from .quotes import fetch_latest_macro_readings
        from .shared import format_macro_readings, macro_recommendations

        readings = fetch_latest_macro_readings()
        markup = format_macro_readings(readings)
        recs = macro_recommendations(readings)
        if markup:
            self.app.call_from_thread(self._on_macro_readings, markup, recs)

    def _on_macro_readings(self, markup: str, recs: list) -> None:
        self._macro_readings_markup = markup
        self._macro_recs = recs
        self._render_header()
        self._update_events_static()

    def _render_holdings(self) -> None:
        table = self.query_one("#events-holdings-table", DataTable)
        table.clear(columns=False)
        if not self.positions:
            table.add_row("[yellow]⚠️ 尚無任何持倉。[/yellow]", "", "", "", "", "", "", "", "", "", "")
            return

        weights = _calc_weights(self.positions, self.rate)
        has_quotes = any(p.market_price is not None or p.market_value is not None for p in self.positions)

        sorted_brokers = group_positions_by_broker(self.positions, self.rate)

        for bk, bk_pos in sorted_brokers:
            bk_total = sum(p.value if p.currency == "USD" else p.value / self.rate for p in bk_pos)
            bk_total_s = f"[bold white]${bk_total:,.0f}[/bold white] [dim]USD[/dim]" if has_quotes else "—"
            table.add_row(
                f"[bold cyan]▐  {bk.upper()}[/bold cyan]",
                "", "", "", "", "", "", "", "", "", bk_total_s
            )

            for p in bk_pos:
                qty_s   = f"{p.quantity:,.2f}" if p.quantity % 1 != 0 else f"{int(p.quantity):,}"
                cost_s  = f"${p.avg_cost:,.2f}" if p.avg_cost is not None else "—"
                price_s = f"${p.market_price:,.2f}" if p.market_price is not None else "—"
                val_s   = f"${p.value:,.2f}" if (p.market_price is not None or p.market_value is not None) else "—"
                mkt_s   = "開市" if is_market_open(p) else "休市"

                d_chg = p.daily_change
                d_pct = p.daily_change_pct
                if d_chg is not None and d_pct is not None:
                    dc  = "green" if d_chg >= 0 else "red"
                    ds  = "+" if d_chg >= 0 else ""
                    ccy = "" if p.currency == "USD" else f" {p.currency}"
                    dpct_s = f"[{dc}]{ds}{d_pct:.2f}%[/{dc}]"
                    dchg_s = f"[{dc}]{ds}{d_chg:,.0f}{ccy}[/{dc}]"
                else:
                    dpct_s = dchg_s = "—"

                key  = (p.broker, p.account or "", p.symbol)
                wt_s = f"{weights.get(key, 0.0):.1f}%" if has_quotes else "—"

                pnl = p.unrealized_pnl
                pct = p.unrealized_pnl_pct
                if pnl is not None and pct is not None:
                    pc    = "green" if pnl >= 0 else "red"
                    ps    = "+" if pnl >= 0 else ""
                    pnl_s = f"[{pc}]{ps}${pnl:,.2f}[/{pc}] [dim]({ps}{pct:.2f}%)[/dim]"
                else:
                    pnl_s = "—"

                table.add_row(
                    f"[bold white]{p.symbol}[/bold white]",
                    f"[dim]{p.instrument_type}[/dim]",
                    qty_s,
                    cost_s,
                    price_s,
                    f"[bold]{val_s}[/bold]" if val_s != "—" else val_s,
                    f"[dim]{wt_s}[/dim]",
                    dpct_s,
                    dchg_s,
                    f"[green]{mkt_s}[/green]" if mkt_s == "開市" else f"[dim]{mkt_s}[/dim]",
                    pnl_s
                )

    def on_mount(self) -> None:
        table = self.query_one("#events-holdings-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Symbol", "Type", "Qty", "Avg Cost", "Price",
            "Market Value", "Wt%", "今日%", "今日漲跌", "市場", "Unrealized P&L"
        )
        self._render_holdings()

        panel = self.query_one("#events-right-panel")
        panel.can_focus = True
        panel.focus()

        self._update_header("[yellow]⏳ 正在抓取最新行事曆與財報日期...[/yellow]")
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
        self._update_header("[yellow]⏳ 正在依新時區重新整理事件...[/yellow]")
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
            fetch_earnings_actuals_batch,
        )
        from .shared import (
            MACRO_EVENT_NAMES,
            event_timezone,
            format_timezone_label,
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

        def offset_label(event_dt) -> str:
            label = format_timezone_label(self.event_timezone, event_dt)
            return label.split(" (", 1)[1].rstrip(")")

        events = []
        earnings_events = []
        occurred_symbols = set()
        current_history = []
        current_history_ids = set()

        def earnings_label(sym: str, is_user: bool, is_sox: bool, occurred: bool) -> str:
            marker = "[bold red](已發生)[/bold red]" if occurred else ""
            if is_user and is_sox:
                return f"🔔 [bold white]{sym}[/bold white]{marker} 財報公佈 (持倉/SOX 十大)"
            if is_user:
                return f"🔔 [bold white]{sym}[/bold white]{marker} 財報公佈 (持倉)"
            return f"💻 {sym}{marker} 財報公佈 (SOX 十大)"

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
                label_base = earnings_label(sym, is_user, is_sox, occurred)
                if period_str:
                    label = f"{label_base} ({period_str} {time_str} {offset_label(event_dt)})"
                else:
                    label = f"{label_base} ({time_str} {offset_label(event_dt)})"
                earnings_events.append([info_date, label, sym, occurred])
                remember_event(sym, info_date, event_dt, period_str, is_user, is_sox)
                if occurred:
                    occurred_symbols.add(sym)
            else:
                for d in dates_list:
                    if isinstance(d, dt_cls):
                        d = d.date()
                    if start_date <= d <= cutoff:
                        occurred = d < today
                        fallback_label = earnings_label(sym, is_user, is_sox, occurred)
                        fallback_label += " (時間待公布，來源尚未提供精確時間)"
                        earnings_events.append([d, fallback_label, sym, occurred])
                        remember_event(sym, d, None, period_str, is_user, is_sox)
                        if occurred:
                            occurred_symbols.add(sym)

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
            label = earnings_label(
                sym,
                bool(item.get("is_user")),
                bool(item.get("is_sox")),
                occurred,
            )
            if time_text:
                period = f"{item.get('period')} " if item.get("period") else ""
                label += f" ({period}{time_text} {offset_label(local_dt)})"
            else:
                label += " (時間待公布，來源尚未提供精確時間)"
            earnings_events.append([event_date, label, sym, occurred])
            if occurred:
                occurred_symbols.add(sym)

        save_event_history(retained_history, self.user)

        earnings_actuals = fetch_earnings_actuals_batch(sorted(occurred_symbols))
        for event_date, label, sym, occurred in earnings_events:
            if occurred:
                label += f"\n   [dim]↳ 更新：{_format_earnings_actuals(earnings_actuals.get(sym))}[/dim]"
            events.append((event_date, label))

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
        for ev_date, ev_label, time_str in macro_list:
            if start_date <= ev_date <= cutoff:
                event_name = MACRO_EVENT_NAMES.get(ev_label, ev_label)
                event_dt = dt_cls.combine(ev_date, time_cls.fromisoformat(time_str), tzinfo=timezone)
                occurred = event_dt <= now
                status = " [bold red](已發生)[/bold red]" if occurred else ""
                label = f"{event_name}{status} ({time_str} {offset_label(event_dt)})"
                if occurred:
                    if ev_label == "◆CPI":
                        update = _format_cpi_event_actuals(cpi_actuals)
                    elif ev_label == "★NFP":
                        update = _format_nfp_event_actuals(nfp_actuals, unemployment_actuals)
                    else:
                        meeting_date_et = event_dt.astimezone(event_timezone("America/New_York")).date()
                        update = _format_fed_event_actuals(compute_fed_decision_conclusion(meeting_date_et))
                    label += f"\n   [dim]↳ 更新：{update}[/dim]"
                events.append((ev_date, label))

        # Update UI back on the event loop
        self.app.call_from_thread(self._on_fetch_complete, events, today)

    def _on_fetch_complete(self, events: list[tuple], today) -> None:
        from rich.console import Group
        from rich.panel import Panel

        self._update_header("[green]✅ 行事曆資料更新成功！[/green]")
        
        if not events:
            self._calendar_month_views = []
            self._update_events_static()
            return

        events.sort(key=lambda x: x[0])

        # Group by month
        by_month = {}
        for d, label in events:
            by_month.setdefault((d.year, d.month), []).append((d, label))

        month_views = []
        for (y, m), ev_list in sorted(by_month.items()):
            tbl = _render_monthly_calendar(y, m, ev_list, today)
            month_views.append(tbl)

        self._calendar_month_views = month_views
        self._update_events_static()


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard Screen
# ─────────────────────────────────────────────────────────────────────────────

def _pos_key(p: Position) -> tuple[str, str, str, str]:
    """部位識別 key（與既有新增/刪除比對邏輯一致：券商+帳戶+代碼+類型）。"""
    return (p.broker.lower(), (p.account or "").lower(), p.symbol.upper(), p.instrument_type)


def _run_calibration_cycle(user: str, force: bool = False):
    """bug#00095: 用「目前生效參數」重跑 ETF/類股回測，並在到期（或 force）時產生
    「需確認」的校準提案（run_recalibration 只存 pending、絕不自動套用）。純本機。"""
    from .storage import load_sector_groups, load_sector_daily_snapshots, taiwan_now
    from . import calibration_schedule as cs, sector_analysis
    today = taiwan_now().date()
    state = cs.ensure_state(user)
    if not force and not cs.due_for_recalibration(state, today):
        return state
    ap = state.get("active_params", {})
    ect = ap.get("etf", {}).get("consensus_threshold", 0.5)
    eme = ap.get("etf", {}).get("min_etfs_evaluated", 4)
    etf_snaps = {sym: load_etf_daily_snapshots(sym) for sym in US_ACTIVE_TICKERS}
    etf_bt = backtest_etf_consensus(etf_snaps, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS,
                                    consensus_threshold=ect, min_etfs_evaluated=eme)
    sbth = ap.get("sector", {}).get("breadth_threshold", 0.5)
    smd = ap.get("sector", {}).get("min_days", 3)
    groups = load_sector_groups(user)
    sec_snaps = {name: load_sector_daily_snapshots(name) for name in groups}
    sec_bt = (sector_analysis.backtest_sector_flow(sec_snaps, breadth_threshold=sbth, min_days=smd)
              if groups else None)
    return cs.run_recalibration(user, {"etf": etf_bt, "sector": sec_bt}, today, force=force)


class CalibrationModal(ModalScreen):
    """bug#00095: 校準狀態與提案確認對話框。系統只提案，套用一律需在此按確認。"""

    DEFAULT_CSS = """
    CalibrationModal { align: center middle; }
    #cal-dialog { width: 84; height: auto; max-height: 90%; border: thick $accent;
                  background: $panel; padding: 1 2; }
    #cal-body { height: auto; margin-bottom: 1; }
    #cal-buttons { height: auto; align: center middle; }
    #cal-buttons Button { margin: 0 1; }
    """

    def __init__(self, user: str) -> None:
        super().__init__()
        self.user = user

    def compose(self) -> ComposeResult:
        with Vertical(id="cal-dialog"):
            yield Static("", id="cal-body")
            with Horizontal(id="cal-buttons"):
                yield Button("套用調整", variant="success", id="apply")
                yield Button("略過建議", variant="warning", id="dismiss")
                yield Button("立即重算", variant="primary", id="recompute")
                yield Button("切換週期", variant="default", id="cadence")
                yield Button("關閉", variant="default", id="close")

    def on_mount(self) -> None:
        self._refresh_body()
        self.query_one("#close").focus()

    def _refresh_body(self) -> None:
        from .storage import taiwan_now
        from . import calibration_schedule as cs
        state = cs.ensure_state(self.user)
        today = taiwan_now().date()
        lines = ["[bold cyan]⚙️ 投資建議校準[/bold cyan]", "", cs.format_status(state, today), ""]
        prop = cs.format_proposal(state)
        if prop:
            lines += prop
        else:
            lines.append("[dim]目前沒有待確認的校準建議。系統到期時只在有統計顯著證據時才提出調整。[/dim]")
        ap = state.get("active_params", {})
        lines += ["", "[dim]目前生效門檻：[/dim]",
                  f"[dim]  ETF 一致門檻 {ap.get('etf',{}).get('consensus_threshold','—')}、"
                  f"最少檔數 {ap.get('etf',{}).get('min_etfs_evaluated','—')}[/dim]",
                  f"[dim]  類股廣度門檻 {ap.get('sector',{}).get('breadth_threshold','—')}、"
                  f"持續天數 {ap.get('sector',{}).get('min_days','—')}[/dim]"]
        self.query_one("#cal-body", Static).update("\n".join(lines))
        has_pending = bool((state.get("pending") or {}).get("changes"))
        self.query_one("#apply").disabled = not has_pending
        self.query_one("#dismiss").disabled = not has_pending

    def on_button_pressed(self, event: Button.Pressed) -> None:
        from .storage import taiwan_now
        from . import calibration_schedule as cs
        bid = event.button.id
        if bid == "close":
            self.dismiss(None)
        elif bid == "apply":
            cs.apply_pending(self.user, today=taiwan_now().date())
            self.app.notify("✅ 已套用校準調整，主頁建議將以新門檻計算。")
            self.dismiss("applied")
        elif bid == "dismiss":
            cs.dismiss_pending(self.user, today=taiwan_now().date())
            self.app.notify("已略過本次校準建議。")
            self.dismiss("dismissed")
        elif bid == "cadence":
            state = cs.ensure_state(self.user)
            cur = state.get("cadence_days", cs.DEFAULT_CADENCE_DAYS)
            cs.set_cadence(self.user, cs.CADENCE_WEEKLY if cur != cs.CADENCE_WEEKLY else cs.CADENCE_BIWEEKLY)
            self._refresh_body()
        elif bid == "recompute":
            try:
                _run_calibration_cycle(self.user, force=True)
                self.app.notify("已用最新快照重算校準。")
            except Exception:
                self.app.notify("重算時發生問題，維持原狀。")
            self._refresh_body()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


def _active_params(user: str) -> dict:
    """bug#00095 接線：讀取使用者校準狀態的「目前生效參數」——使用者在校準對話框
    按下確認後的門檻調整，即透過此函式讓所有推薦計算生效。讀檔失敗一律回退預設，
    永不讓主頁渲染因此崩潰。"""
    try:
        from .calibration_schedule import ensure_state
        return ensure_state(user).get("active_params", {})
    except Exception:
        try:
            from .calibration_schedule import default_params
            return default_params()
        except Exception:
            return {}


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
        dir_color = {"多": "green", "空": "red", "觀望": "yellow"}.get(rec.direction, "cyan")
        head_lines = [rec.verdict]
        if (rec.basis or "").strip():
            head_lines.append(f"[dim]判斷依據：[/dim]{rec.basis}")
        self.query_one("#rd-head", Static).update(
            _Panel("\n".join(head_lines), title="[bold]🔍 投資建議 · 公式與計算細節[/bold]",
                   border_style=dir_color, padding=(0, 1))
        )

        from rich.console import Group
        renderables: list = []
        sections = self.rec.detail_sections or []
        if not sections:
            renderables.append(Static("[dim]此建議無額外公式細節。[/dim]"))
        for i, sec in enumerate(sections, 1):
            body_lines = []
            if sec.get("formula"):
                body_lines.append(f"[bold cyan]公式[/bold cyan]\n{sec['formula']}")
            if sec.get("substitution"):
                body_lines.append(f"[bold green]帶入此標的數字[/bold green]\n{sec['substitution']}")
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
        Binding("1",   "add_position",         "新增部位"),
        Binding("2",   "refresh_now",          "立即重整"),
        Binding("3",   "logout",               "安全登出"),
        Binding("4",   "upcoming_events",      "近期重大事件"),
        Binding("5",   "save_snapshot",        "儲存快照"),
        Binding("6",   "active_etfs",          "主動式 ETF 排行"),
        Binding("7",   "options_watchlist",    "期權觀察清單"),
        Binding("8",   "sector_analysis",      "類股板塊分析"),
        Binding("k",   "calibration",          "投資建議校準"),
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
        self.row_data: list[Optional[Position]] = []
        self._marked: set[tuple[str, str, str, str]] = set()  # space 多選標記（批次刪除用）
        self._upcoming_events: list[tuple] = []
        self._events_fetched: bool = False
        self._fetching_events: bool = False
        self._rf_rate: float         = 0.04  # risk-free rate (^IRX), warmed in background

    # ── Layout ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("", id="tui-header")
        with Horizontal(id="main-layout"):
            with Vertical(id="content-area"):
                with Horizontal(id="top-row"):
                    yield Static("", id="broker-dist")
                    yield Static("", id="metrics-row")
                yield Static(
                    "[bold dim] Holdings[/bold dim]  [dim](e 編輯　x 刪除　space 多選)[/dim]",
                    id="holdings-label"
                )
                with Horizontal(id="holdings-row"):
                    with ScrollableContainer(id="holdings-scroll"):
                        yield DataTable(id="holdings-table")
                    yield Static("", id="recent-events-panel")
                yield Static("", id="cross-model-panel")
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
            "Symbol", "Type", "Qty", "Avg Cost", "Price",
            "Market Value", "Wt%", "今日%", "今日漲跌", "市場", "Unrealized P&L"
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
                    self._marked.symmetric_difference_update({_pos_key(pos)})
                    self._render_all()
            elif event.key == "e":
                if pos is not None:
                    self.app.push_screen(
                        AddPositionModal(pos),
                        lambda res: self._handle_edit_position_result(pos, res)
                    )
            elif event.key == "x":
                if self._marked:
                    targets = [p for p in self.row_data if p is not None and _pos_key(p) in self._marked]
                elif pos is not None:
                    targets = [pos]
                else:
                    targets = []
                if targets:
                    self.app.push_screen(
                        DeleteConfirmModal(targets),
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

        # Map column index to field
        # Columns: "Symbol", "Type", "Qty", "Avg Cost", "Price", "Market Value", "Wt%", "今日%", "今日漲跌", "市場", "Unrealized P&L"
        editable_fields = {
            0: ("symbol", "商品代碼 (Symbol)", None),
            1: ("instrument_type", "持倉類型 (Type)", ["stock", "etf", "option"]),
            2: ("quantity", "持倉數量 (Quantity)", None),
            3: ("avg_cost", "平均成本 (Avg Cost)", None),
            9: ("market", "交易市場 (Market)", ["US", "TW", "HK"]),
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
            modal = DeleteConfirmModal(pos)
            self.app.push_screen(modal, lambda confirmed: self._handle_delete_confirm(pos, confirmed))

    def _apply_metadata_edit(self, pos: Position, field_name: str, new_val: Optional[str]) -> None:
        if new_val is None:
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
        self._do_refresh_worker()

    def _handle_delete_confirm(self, pos: Position, confirmed: Optional[bool]) -> None:
        if not confirmed:
            return
        positions, cash_positions = load_manual_positions(user=self._user)
        target = next((p for p in positions if p.broker == pos.broker and (p.account or "") == (pos.account or "") and p.symbol == pos.symbol), None)
        if target:
            positions.remove(target)
            save_manual_positions(positions, cash_positions=cash_positions, user=self._user)
            self._do_refresh_worker()

    # ── Header tick (every 1s, lightweight) ──────────────────────────────────

    def _tick_header(self) -> None:
        now_str = datetime.now().strftime("%H:%M:%S")
        status  = (
            "[yellow]⏳ 更新中...[/yellow]"
            if self._loading
            else "[dim]⏱️ 每 60 秒自動刷新[/dim]"
        )
        self.query_one("#tui-header", Static).update(
            Panel(
                f"[bold cyan]✨ AssetTrack[/bold cyan]  "
                f"[dim]│[/dim]  "
                f"[bold]👤 {self._user}[/bold]  "
                f"[dim]│[/dim]  "
                f"[dim]🕒 {now_str}[/dim]  "
                f"[dim]│[/dim]  "
                f"{status}",
                border_style="cyan",
                padding=(0, 1),
            )
        )
        # bug#00096 常駐狀態列：持續顯示背景目前正在抓取什麼資訊
        try:
            activity = list(getattr(self.app, "_fetch_activity", {}).values())
        except RuntimeError:
            activity = []
        if activity:
            bar = "[yellow]⏳ 正在抓取：[/yellow]" + "、".join(activity)
        else:
            bar = "[dim]✓ 資料已是最新（背景閒置）[/dim]"
        try:
            self.query_one("#status-bar", Static).update(bar)
        except Exception:
            pass

    # ── Full render ───────────────────────────────────────────────────────────

    def _render_all(self) -> None:
        """Render all dashboard widgets from current in-memory data."""
        table = self.query_one("#holdings-table", DataTable)
        
        # Save cursor coordinate and focus state
        old_coordinate = table.cursor_coordinate
        had_focus = (self.focused == table)

        if not self._positions:
            self.query_one("#metrics-row",     Static).update(
                Panel("[dim]尚無持倉部位[/dim]", border_style="dim")
            )
            table.clear(columns=False)
            table.add_row(
                "[yellow]⚠️ 尚無任何持倉。請按 [bold]1[/bold] 新增部位。[/yellow]",
                "", "", "", "", "", "", "", "", "", ""
            )
            self.row_data = [None]
            self.query_one("#broker-dist",     Static).update("")
            self.query_one("#recent-events-panel", Static).update(
                self._build_recent_events_panel()
            )
            self.query_one("#etf-conclusions-panel", Static).update(
                self._build_etf_conclusions_panel()
            )
            self.query_one("#options-flow-panel", Static).update(
                self._build_options_flow_panel()
            )
            self.query_one("#sector-consensus-panel", Static).update(
                self._build_sector_consensus_panel()
            )
            self._refresh_cross_model_panel()

            if had_focus:
                table.focus()
            return

        weights = _calc_weights(self._positions, self._rate)
        has_quotes = any(p.market_price is not None or p.market_value is not None for p in self._positions)

        self.query_one("#metrics-row", Static).update(
            _build_metrics_panel(self._positions, self._rate)
        )

        table.clear(columns=False)
        self.row_data = []

        sorted_brokers = group_positions_by_broker(self._positions, self._rate)

        for i, (bk, bk_pos) in enumerate(sorted_brokers):
            bk_total = sum(
                p.value if p.currency == "USD" else p.value / self._rate for p in bk_pos
            )
            
            bk_total_s = f"[bold white]${bk_total:,.0f}[/bold white] [dim]USD[/dim]" if has_quotes else "—"
            table.add_row(
                f"[bold cyan]▐  {bk.upper()}[/bold cyan]",
                "", "", "", "", "", "", "", "", "",
                bk_total_s
            )
            self.row_data.append(None)

            for p in bk_pos:
                qty_s   = f"{p.quantity:,.2f}" if p.quantity % 1 != 0 else f"{int(p.quantity):,}"
                cost_s  = f"${p.avg_cost:,.2f}" if p.avg_cost is not None else "—"
                price_s = f"${p.market_price:,.2f}" if p.market_price is not None else "—"
                val_s   = f"${p.value:,.2f}" if (p.market_price is not None or p.market_value is not None) else "—"
                mkt_s   = "開市" if is_market_open(p) else "休市"

                d_chg = p.daily_change
                d_pct = p.daily_change_pct
                if d_chg is not None and d_pct is not None:
                    dc  = "green" if d_chg >= 0 else "red"
                    ds  = "+" if d_chg >= 0 else ""
                    ccy = "" if p.currency == "USD" else f" {p.currency}"
                    dpct_s = f"[{dc}]{ds}{d_pct:.2f}%[/{dc}]"
                    dchg_s = f"[{dc}]{ds}{d_chg:,.0f}{ccy}[/{dc}]"
                else:
                    dpct_s = dchg_s = "—"

                key  = (p.broker, p.account or "", p.symbol)
                wt_s = f"{weights.get(key, 0.0):.1f}%" if has_quotes else "—"

                pnl = p.unrealized_pnl
                pct = p.unrealized_pnl_pct
                if pnl is not None and pct is not None:
                    pc    = "green" if pnl >= 0 else "red"
                    ps    = "+" if pnl >= 0 else ""
                    pnl_s = f"[{pc}]{ps}${pnl:,.2f}[/{pc}] [dim]({ps}{pct:.2f}%)[/dim]"
                else:
                    pnl_s = "—"

                mark_s = "[bold green]✔ [/bold green]" if _pos_key(p) in self._marked else ""
                table.add_row(
                    f"{mark_s}[bold white]{p.symbol}[/bold white]",
                    f"[dim]{p.instrument_type}[/dim]",
                    qty_s,
                    cost_s,
                    price_s,
                    f"[bold]{val_s}[/bold]" if val_s != "—" else val_s,
                    f"[dim]{wt_s}[/dim]",
                    dpct_s,
                    dchg_s,
                    f"[green]{mkt_s}[/green]" if mkt_s == "開市" else f"[dim]{mkt_s}[/dim]",
                    pnl_s
                )
                self.row_data.append(p)

        self.query_one("#broker-dist", Static).update(
            _build_broker_panel(self._positions, self._rate)
        )
        self.query_one("#recent-events-panel", Static).update(
            self._build_recent_events_panel()
        )
        self.query_one("#etf-conclusions-panel", Static).update(
            self._build_etf_conclusions_panel()
        )
        self.query_one("#options-flow-panel", Static).update(
            self._build_options_flow_panel()
        )
        self.query_one("#sector-consensus-panel", Static).update(
            self._build_sector_consensus_panel()
        )
        self._refresh_cross_model_panel()

        # Restore coordinate and focus state
        if len(self.row_data) > 0:
            old_row, old_col = old_coordinate
            new_row = min(old_row, len(self.row_data) - 1)
            new_col = min(old_col, 10)
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
            self._rate      = _get_cached_usdtwd_rate()
            # Warm the ^IRX risk-free cache off the UI thread so the options card's
            # divergence math uses the same real rate as the 期權觀察清單 page (bug#00067).
            from .quotes import fetch_risk_free_rate
            self._rf_rate = fetch_risk_free_rate(default=self._rf_rate)
            if load_from_disk:
                self._positions, self._cash_positions = load_manual_positions(user=self._user)
            if self._positions:
                self._positions = enrich_positions_with_quotes(self._positions)
        except Exception:
            pass
        finally:
            self._loading = False
            self.app._clear_fetch_active('quotes')
        # Schedule UI update back on the event loop
        self.app.call_from_thread(self._render_all)
        if load_from_disk:
            self._events_fetched = False
            self._fetch_upcoming_events_worker()

    @work(thread=True)
    def _fetch_upcoming_events_worker(self) -> None:
        if self._fetching_events:
            return
        self._fetching_events = True
        self.app._set_fetch_active('events', '財報行事曆與總經數據')
        
        from datetime import datetime as dt_cls, timedelta
        import concurrent.futures
        import yfinance as yf
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

            today = datetime.utcnow().date()
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

        self.app.call_from_thread(self._render_all)

    def _on_events_fetched(self, events: list[tuple]) -> None:
        self._upcoming_events = events
        self._events_fetched = True
        self._render_all()

    def _build_recent_events_panel(self) -> Panel:
        from rich.panel import Panel
        from datetime import datetime as dt_cls, timedelta
        
        today = datetime.utcnow().date()
        
        if not self._events_fetched:
            return Panel("\n [yellow]⏳ 正在背景同步行事曆...[/yellow]", title="📅 近期重大事件", border_style="cyan")
            
        if not self._upcoming_events:
            return Panel("\n [dim]近期 30 天無重大事件[/dim]", title="📅 近期重大事件", border_style="cyan")
            
        cutoff = today + timedelta(days=30)
        recent = []
        for d, label in self._upcoming_events:
            if today <= d <= cutoff:
                recent.append((d, label))
                
        if not recent:
            return Panel("\n [dim]近期 30 天無重大事件[/dim]", title="📅 近期重大事件", border_style="cyan")
            
        recent.sort(key=lambda x: x[0])
        
        lines = []
        for d, label in recent[:8]:
            days_away = (d - today).days
            days_str = "今天" if days_away == 0 else f"{days_away}天後"
            date_str = d.strftime("%m-%d")
            simplified = _simplify_event_label(label)
            lines.append(f"[cyan]{date_str}[/cyan] [dim]({days_str:^4})[/dim] {simplified}")
            
        if len(recent) > 8:
            lines.append(f"[dim]... 還有 {len(recent) - 8} 個事件 (按 [bold]4[/bold] 詳情)[/dim]")
            
        return Panel("\n".join(lines), title="📅 近期重大事件", border_style="cyan")

    def _refresh_cross_model_panel(self) -> None:
        """bug#00096 / bug#00119 / bug#00118: 跨模型總結建議卡（主頁）——把三項有回測背書
        的方向訊號（主動式 ETF／期權／類股）各自的淨方向分數，以「該項回測可信度」加權，
        合成一個整體傾向；「近期重大事件」不投方向票，改作謹慎度修正。100% 離線，與各分項
        卡片共用同一份 report/回測；三層寫作格式，附可點選『🔍 查看公式細節』。"""
        w = self.query_one("#cross-model-panel", Static)
        w.border_title = "🧭 跨模型總結建議（主頁 · 點『🔍 查看公式細節』看加權公式）"
        try:
            from .cross_model import synthesize_cross_model
            from .storage import (load_sector_groups, load_sector_daily_snapshots,
                                  taiwan_now)
            from .shared import get_upcoming_macro_events
            from .calibration import backtest_verdicts
            from . import sector_analysis

            # ETF（美股宇集，與 ETF 卡片一致）；bug#00095 接線：套用已確認校準參數。
            _ap = _active_params(self._user)
            _ect = _ap.get('etf', {}).get('consensus_threshold', 0.5)
            _eme = _ap.get('etf', {}).get('min_etfs_evaluated', 4)
            etf_snaps = {sym: load_etf_daily_snapshots(sym) for sym in US_ACTIVE_TICKERS}
            etf_report = compute_symbol_trends(etf_snaps, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ect)
            etf_bt = backtest_etf_consensus(etf_snaps, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ect, min_etfs_evaluated=_eme)

            # 期權（持倉 ∪ 自訂清單）
            underlyings, _, _ = _watchlist_underlyings(self._user, self._positions)
            opt_snaps = {u: load_options_daily_snapshots(u) for u in underlyings}
            opt_verdicts = (compute_directional_verdicts(
                opt_snaps, r=self._rf_rate, window_days=OPTIONS_FLOW_WINDOW_DAYS)
                if underlyings else {})
            opt_bt = (backtest_verdicts(
                opt_snaps, window_days=OPTIONS_FLOW_WINDOW_DAYS, r=self._rf_rate)
                if underlyings else None)

            # 類股；bug#00095 接線：套用已確認校準參數。
            _sbth = _ap.get('sector', {}).get('breadth_threshold', 0.5)
            _smd = _ap.get('sector', {}).get('min_days', 3)
            groups = load_sector_groups(self._user)
            sec_snaps = {name: load_sector_daily_snapshots(name) for name in groups}
            sec_flows = {name: sector_analysis.detect_broad_flow(sec_snaps[name], breadth_threshold=_sbth, min_days=_smd) for name in groups}
            sec_bt = sector_analysis.backtest_sector_flow(sec_snaps, breadth_threshold=_sbth, min_days=_smd) if groups else None

            macro = get_upcoming_macro_events(days=30, start_days_ago=0)
            result = synthesize_cross_model(
                etf_report=etf_report, etf_backtest=etf_bt,
                options_verdict_report=opt_verdicts, options_backtest=opt_bt,
                sector_flows=sec_flows, sector_backtest=sec_bt,
                upcoming_macro=macro, today=taiwan_now().date(),
                etf_min_etfs_evaluated=_eme,
            )
            color = {"偏多": "green", "強烈偏多": "green", "偏空": "red",
                     "強烈偏空": "red"}.get(result["overall_direction"], "yellow")
            body = "\n".join(result["summary_lines"])
            rec = result.get("recommendation")
            if rec is not None:
                self._recs_by_id = {"xm": rec}
                body += "\n[@click=screen.show_formula('xm')]🔍 查看公式細節 ›[/]"
            else:
                self._recs_by_id = {}
            try:
                w.styles.border = ("round", color)
            except Exception:
                pass
            w.update(body)
        except Exception as exc:
            self._recs_by_id = {}
            w.update(f"[dim]跨模型總結計算中… ({type(exc).__name__})[/dim]")

    def _build_etf_conclusions_panel(self) -> Panel:
        """bug#00061: 首頁「交易策略建議」卡片之一 —— 主動式ETF跨基金持股趨勢結論。
        100% 離線本機運算（讀取 etf_cache/history/*.jsonl 真實累積快照），無網路請求；
        與「主動式ETF排行」頁面的進階分析畫面共用同一份 generate_etf_conclusions()
        輸出，兩處文字保證一致。資料不足時誠實顯示收集進度，不生成假結論。
        """
        from rich.panel import Panel

        # 投資建議一律以美股為主（bug#00091）：ETF 趨勢共識只納入美股主動式 ETF。
        # bug#00095 接線：套用使用者已確認的校準參數（consensus_threshold / min_etfs_evaluated）。
        _ap = _active_params(self._user).get('etf', {})
        _ct = _ap.get('consensus_threshold', 0.5)
        _me = _ap.get('min_etfs_evaluated', 4)
        all_symbols = US_ACTIVE_TICKERS
        snapshots_by_etf = {sym: load_etf_daily_snapshots(sym) for sym in all_symbols}
        report = compute_symbol_trends(snapshots_by_etf, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ct)
        # bug#00092: 與結論卡共用同一套 walk-forward 回測，命中率就地顯示於每則多數性結論。
        _bt = backtest_etf_consensus(snapshots_by_etf, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ct, min_etfs_evaluated=_me)
        bullets = generate_etf_conclusions(report, top_n=2, positions=self._positions, backtest=_bt, min_etfs_evaluated=_me)

        # bug#00114：此卡片改為「截取」ActiveETFsScreen 下方 detail 分析框的結論——最上面
        # 一行是每日主動選股多空傾向（etf_stance_phrase，與分析框共用同一 tilt 輸出），
        # 下面接跨ETF共識 top-N，導引改指向「按 6 → 下方分析框」看完整依據與每日多空。
        tilt = compute_etf_selection_tilt(report)
        stance_line = etf_stance_phrase(tilt)

        if not bullets:
            body = (
                f"{stance_line}\n\n"
                f"[dim]資料收集中：{report['etfs_ready_count']}/{report['etfs_total_count']} "
                f"檔 ETF 已有足夠真實快照\n尚無法產生趨勢結論，持續使用系統會逐日累積資料\n"
                f"按 [bold]6[/bold] 進入「主動式ETF排行」，下方分析框有完整依據[/dim]"
            )
        else:
            body = (
                f"{stance_line}\n\n"
                + "\n".join(bullets)
                + "\n\n[dim]按 [bold]6[/bold] 進入「主動式ETF排行」，下方分析框有完整依據與每日多空[/dim]"
            )

        return Panel(body, title="📊 ETF趨勢結論", border_style="cyan")

    def _build_options_flow_panel(self) -> Panel:
        """bug#00061 / bug#00067 / bug#00099: 首頁「交易策略建議」卡片之二 —— 期權觀察結論。
        100% 離線本機運算，與「期權觀察清單」頁面共用 generate_grouped_analysis_card()，
        但卡片以 summary_only=True 只顯示「每檔一行總結」（方向＋該檔獨立 walk-forward
        回測命中率），完整明細（依據/合約事件/IV 位階…）留到頁面呈現，避免首頁被佔滿；
        卡片只列有方向者（頁面另含觀望）。觀察標的與頁面一致採「持倉 ∪ 自訂清單」。
        無風險利率取非阻塞的 cached_risk_free_rate()（由背景 worker 以 ^IRX 暖快取）。
        資料不足時誠實顯示收集進度。
        """
        from rich.panel import Panel

        underlyings, _, _ = _watchlist_underlyings(self._user, self._positions)
        if not underlyings:
            return Panel(
                "[dim]尚無持倉或自訂標的，無法建立期權觀察清單[/dim]",
                title="🎯 期權觀察結論", border_style="magenta",
            )

        snapshots_by_underlying = {u: load_options_daily_snapshots(u) for u in underlyings}
        flow_report = compute_options_flow(snapshots_by_underlying, window_days=OPTIONS_FLOW_WINDOW_DAYS)
        div_report = compute_iv_divergence(
            snapshots_by_underlying, r=self._rf_rate, window_days=OPTIONS_FLOW_WINDOW_DAYS
        )
        iv_pct = {u: compute_iv_percentile(snapshots_by_underlying[u]) for u in underlyings}
        # 綜合方向結論（skew＋殘差）。回測改為逐標的獨立進行（見
        # generate_grouped_analysis_card），backtest_verdicts 對每檔各跑一次、
        # 有資料簽章快取，60 秒重繪週期直接取快取，不重算。
        verdict_report = compute_directional_verdicts(
            snapshots_by_underlying, r=self._rf_rate, window_days=OPTIONS_FLOW_WINDOW_DAYS
        )
        # bug#00099 / bug#00100: 首頁卡片只顯示「每檔一行總結」（方向＋該檔獨立回測
        # 命中率），完整明細（依據/事件/IV…）留給「期權觀察清單」頁，避免卡片被佔滿。
        bullets = generate_grouped_analysis_card(
            verdict_report, flow_report, div_report, snapshots_by_underlying,
            r=self._rf_rate, window_days=OPTIONS_FLOW_WINDOW_DAYS,
            positions=self._positions, iv_pct_by_underlying=iv_pct,
            include_neutral=False, summary_only=True,
        )

        if not bullets:
            body = (
                f"[dim]資料收集中：{div_report['ready_count']}/{div_report['total_count']} "
                f"檔標的已有 ≥2 天真實快照\n尚無法產生訊號，持續使用系統會逐日累積資料\n"
                f"按 [bold]7[/bold] 查看期權觀察清單[/dim]"
            )
        else:
            body = "\n".join(bullets) + "\n\n[dim]按 [bold]7[/bold] 查看完整清單[/dim]"

        return Panel(body, title="🎯 期權觀察結論", border_style="cyan")

    def _build_sector_consensus_panel(self) -> Panel:
        """item#4 / bug#00078 擴充：首頁「交易策略建議」第三張卡片 —— 類股板塊共識。
        每日累計追蹤各板塊是否「普遍」上漲/下跌，抓出市場對特定類股族群的共同買進上
        漲/共同賣出下跌。100% 離線本機運算（讀取 sector_cache/history/*.jsonl 真實累
        積快照），與「類股板塊分析」頁面共用同一份 generate_sector_conclusions() 輸
        出，兩處文字保證一致。資料不足時誠實顯示收集進度，不生成假結論。"""
        from rich.panel import Panel
        from .storage import load_sector_groups, load_sector_daily_snapshots
        from . import sector_analysis

        groups = load_sector_groups(self._user)
        if not groups:
            return Panel(
                "[dim]尚無任何板塊，按 [bold]8[/bold] 進入類股板塊分析新增[/dim]",
                title="📊 類股共識", border_style="magenta",
            )

        # bug#00095 接線：套用已確認的校準參數（breadth_threshold / min_days）。
        _sap = _active_params(self._user).get('sector', {})
        _bth = _sap.get('breadth_threshold', 0.5)
        _md = _sap.get('min_days', 3)
        snapshots_by_group = {name: load_sector_daily_snapshots(name) for name in groups}
        flows = {
            name: sector_analysis.detect_broad_flow(snapshots_by_group[name], breadth_threshold=_bth, min_days=_md)
            for name in groups
        }
        # bug#00093: 與類股頁共用同一套 walk-forward 回測，命中率就地顯示於每則類股共識。
        _sec_bt = sector_analysis.backtest_sector_flow(snapshots_by_group, breadth_threshold=_bth, min_days=_md)
        bullets = sector_analysis.generate_sector_conclusions(flows, backtest=_sec_bt)

        if not bullets:
            ready = sum(1 for f in flows.values() if f.get("ready"))
            body = (
                f"[dim]資料收集中：{ready}/{len(groups)} 個板塊已有足夠真實快照\n"
                f"尚無「普遍」共識訊號，持續使用系統會逐日累積廣度資料\n"
                f"按 [bold]8[/bold] 查看類股板塊分析[/dim]"
            )
        else:
            body = "\n".join(bullets) + "\n\n[dim]按 [bold]8[/bold] 查看完整分析[/dim]"

        return Panel(body, title="📊 類股共識", border_style="cyan")

    # ── Action handlers ───────────────────────────────────────────────────────

    def action_add_position(self) -> None:
        """[1] 直接開啟批次新增部位對話框（編輯/刪除改由表格 e / x / space 直接操作）。"""
        self.app.push_screen(AddPositionModal(), self._handle_add_position_result)

    def _handle_edit_position_result(self, old_pos: Position, result: Optional[list[Position]]) -> None:
        if result:
            updated_pos = result[0]
            positions, cash_positions = load_manual_positions(self._user)
            for idx, p in enumerate(positions):
                if _pos_key(p) == _pos_key(old_pos):
                    positions[idx] = updated_pos
                    break
            save_manual_positions(positions, cash_positions=cash_positions, user=self._user)
            # 部位識別 key 可能已變更，移除舊標記避免殘留
            self._marked.discard(_pos_key(old_pos))
            self.app.notify("✅ 修改持倉成功！")
            self._positions = positions
            self._do_refresh_worker()

    def _handle_batch_delete_confirm(self, targets: list[Position], confirmed: bool | None) -> None:
        if not confirmed:
            return
        keys = {_pos_key(p) for p in targets}
        positions, cash_positions = load_manual_positions(self._user)
        new_positions = [p for p in positions if _pos_key(p) not in keys]
        removed = len(positions) - len(new_positions)
        save_manual_positions(new_positions, cash_positions=cash_positions, user=self._user)
        self._marked -= keys
        self.app.notify(f"🗑️ 已刪除 {removed} 筆部位！")
        self._positions = new_positions
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

    def _handle_add_position_result(self, result: Optional[list[Position]]) -> None:
        if result:
            positions, cash_positions = load_manual_positions(self._user)
            for pos in result:
                self._merge_position(positions, pos)
            save_manual_positions(positions, cash_positions=cash_positions, user=self._user)
            n = len(result)
            self.app.notify(f"✅ 已儲存 {n} 筆持倉！" if n > 1 else "✅ 新增持倉成功！")
            self._positions = positions
            self._do_refresh_worker()

    def action_refresh_now(self) -> None:
        """[2] 立即重整：背景更新報價。"""
        self._do_refresh_worker()

    def action_logout(self) -> None:
        """[3] 安全登出：Textual Modal 確認 → 返回 LoginScreen。"""
        self.app.push_screen(LogoutConfirmModal(), self._handle_logout_confirm)

    def _handle_logout_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
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
            self.app.call_from_thread(self.app.notify, "✅ 市值快照儲存成功！", title="快照")
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"❌ 儲存快照失敗: {e}", title="快照", severity="error")

    def action_upcoming_events(self) -> None:
        """[4] 近期重大事件：推入 UpcomingEventsScreen，不 suspend。"""
        self.app.push_screen(UpcomingEventsScreen(self._user, self._positions, self._rate))

    def action_active_etfs(self) -> None:
        """[6] 主動式 ETF 排行：推入 ActiveETFsScreen，不 suspend。"""
        self.app.push_screen(ActiveETFsScreen(self._user, self._rate))

    def action_options_watchlist(self) -> None:
        """[7] 期權觀察清單：推入 OptionsWatchlistScreen，不 suspend。"""
        self.app.push_screen(OptionsWatchlistScreen(self._user, self._positions))

    def action_sector_analysis(self) -> None:
        """[8] 類股板塊分析：推入 SectorAnalysisScreen，不 suspend。"""
        self.app.push_screen(SectorAnalysisScreen(self._user))

    def action_calibration(self) -> None:
        """[k] 投資建議校準：檢視校準狀態與待確認提案（套用需在對話框確認）。"""
        self.app.push_screen(CalibrationModal(self._user))


# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Active ETFs Screen
# ─────────────────────────────────────────────────────────────────────────────
# Ticker universe — single source of truth.
# All AUM, performance, and holdings come from live yfinance calls (cached daily
# per-ETF in data/etf_cache/). Symbols that yfinance has no data for simply show
# "—" / "暫無資料" in the UI — nothing here is backfilled with fabricated data.

US_ACTIVE_TICKERS: list[str] = [
    # Top 20 largest actively managed ETFs by AUM
    "DFAC", "JEPI", "JEPQ", "JPST", "DFUS", "DFIV", "DFAI", "DFUV", "DFAS", "DFAT",
    "DFIC", "JCPB", "DUHP", "DFAU", "JIRE", "AVUV", "JPIE", "JGRO", "CGDV", "ARKK",
    "SEQUX",
    # Other active ETFs under consideration (will be filtered by top 30 size)
    "AVDV", "AVDE", "ARKW", "AVUS", "AVIV", "CGGR", "CGGO", "AVLV", "AVMV", "ARKG",
    "ARKQ", "ARKF", "DFGR", "DFHV", "DFSV", "DFVX", "MSTY", "CONY", "TSLY", "NVDY",
    "AMZY", "FBCG", "FMAG", "FDIG", "JGLO", "JUSA", "AVSC", "AVMC", "AVQC", "AVLC",
    "AVSF", "CGMU", "CGSD", "CGMS", "CGCP", "JEMA", "JGER", "JPMB", "JPIN", "DFIP",
    "CGIC", "CVGD", "CVSM",
]

# bug#00091：投資建議一律以美股為主，台股主動式ETF排行已移除；此清單保留供未來
# 參考，目前不再用於任何排行/抓取/投資建議路徑。
TWD_ACTIVE_TICKERS: list[str] = [
    "0050.TW", "0056.TW", "00878.TW", "00919.TW", "00929.TW",
    "00713.TW", "00940.TW", "00757.TW", "00850.TW", "00881.TW",
    "00900.TW", "00905.TW", "00907.TW", "00915.TW", "00918.TW",
    "00921.TW", "00922.TW", "00927.TW", "00930.TW", "00933.TW",
]

_ETF_TOP_N = 30  # ETFs shown per tab (AUM top-30)

# yfinance FundsData.asset_classes keys -> display label. Used to show a fund's
# full stock/bond/cash/preferred/convertible/other split in the holdings panel,
# so it isn't limited to just the top named stock-type positions.
_ASSET_CLASS_LABELS: dict[str, str] = {
    "stockPosition": "📈 股票",
    "bondPosition": "📄 債券",
    "cashPosition": "💵 現金",
    "preferredPosition": "⭐ 特別股",
    "convertiblePosition": "🔄 可轉債",
    "otherPosition": "❔ 其他（含衍生性金融商品等）",
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

    if not stale_symbols:
        return {"aums": {}, "perf": {}, "etf_cache": {}, "perf_fail_count": 0}

    stale_perf = fetch_active_etf_performance(stale_symbols)

    aums: dict[str, float] = {}
    perf: dict[str, dict] = {}
    etf_cache: dict[str, dict] = {}

    def _fetch_one_etf_details(sym: str) -> tuple[str, float | None, str | None, dict | None]:
        import time as _time
        # Add a small delay between requests to prevent Yahoo rate limiting
        _time.sleep(0.35)
        try:
            t = _yf.Ticker(sym)
            aum_val = t.info.get("totalAssets") or t.info.get("marketCap")
            aum = float(aum_val) if aum_val else None
            name = t.info.get("longName") or t.info.get("shortName") or sym
            holdings_res = fetch_etf_holdings(sym, aum=aum)
            return sym, aum, name, holdings_res
        except Exception:
            try:
                holdings_res = fetch_etf_holdings(sym, aum=None)
                name = holdings_res.get("name", sym) if holdings_res else sym
                return sym, None, name, holdings_res
            except Exception:
                return sym, None, sym, None

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
    for _, _, _, holdings_res in fetched:
        if holdings_res:
            for h in holdings_res.get("holdings", []):
                if h.get("symbol"):
                    holding_symbols.add(h["symbol"])
    price_map = fetch_prices_batch(list(holding_symbols)) if holding_symbols else {}

    for sym, aum, name, holdings_res in fetched:
        cached = load_etf_symbol_cache(sym)
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
            if k in p_item:
                cached[k] = p_item[k]

        p_constructed = {k: cached[k] for k in ("price", "change_pct", "return_ytd", "return_1y") if k in cached}
        if p_constructed:
            perf[sym] = p_constructed

        # Update holdings + full stock/bond/cash/other asset-class breakdown
        if holdings_res:
            holdings_list = holdings_res.get("holdings", [])
            for h in holdings_list:
                real_price = price_map.get(h.get("symbol"))
                h["price"] = real_price
                h["shares"] = estimate_shares(h.get("symbol", ""), h.get("weight", 0.0), aum, real_price)
            cached["holdings"] = holdings_list
            cached["asset_classes"] = holdings_res.get("asset_classes", {})
            cached["holdings_as_of_date"] = holdings_res.get("as_of_date", "")

            # 進階分析 (bug#00060): record today's *real* holdings as one
            # dated line in this symbol's history log. This is the only
            # source the trend/consensus report reads from — nothing here
            # is backfilled or estimated for days we didn't actually fetch.
            append_etf_daily_snapshot(sym, cached["holdings"], cached.get("aum"), asset_classes=cached.get("asset_classes"))

        # Automated ETF trade history pipeline (bug#00101): derive & parse
        # trade history from daily snapshot diffs and official trade sources.
        from .etf_trades import update_etf_trade_history
        updated_history = update_etf_trade_history(sym)
        cached["history"] = updated_history

        # Save cache file
        save_etf_symbol_cache(sym, cached)
        etf_cache[sym] = cached

    return {"aums": aums, "perf": perf, "etf_cache": etf_cache, "perf_fail_count": perf_fail_count}


class ActiveETFsScreen(_FormulaDrillMixin, Screen):
    r"""主動式 ETF 績效與持股分析 - 三欄式版面。

    Layout::

        ┌──────────────┬──────────────────────┐
        │  左欄 50%    │    右欄 50% (上下分割) │
        │  US / TW 分頁│    持股細節           │
        │              │    歷史買賣紀錄        │
        └──────────────┴──────────────────────┘
    """

    BINDINGS = [
        Binding("escape", "go_back", "返回看板"),
        Binding("c",      "clear_cache", "清除快取並重新載入"),
        Binding("a",      "advanced_analysis", "進階分析"),
        Binding("q",      "go_back", "返回看板", show=False),
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
    #etf-body {
        height: 3fr;
        layout: horizontal;
        margin: 1 2 0 2;
    }
    /* Left column */
    #etf-left-col {
        width: 50%;
        height: 1fr;
        layout: vertical;
        margin-right: 1;
    }
    #etf-left-tabbed {
        height: 1fr;
        border: tall #334155;
    }
    #etf-left-tabbed:focus-within { border: tall $accent; }
    #etf-us-table {
        height: 1fr;
        border: none;
    }
    /* Right column (vertical split) */
    #etf-right-col {
        width: 50%;
        height: 1fr;
        layout: vertical;
    }
    #etf-holdings-box {
        height: 1fr;
        layout: vertical;
        margin-bottom: 1;
    }
    #etf-holdings-title {
        height: 1;
        padding: 0 1;
        color: $accent;
        text-style: bold;
    }
    #etf-holdings-status {
        height: 1;
        padding: 0 1;
    }
    #etf-holdings-panel {
        height: 1fr;
        border: tall #334155;
    }
    #etf-holdings-panel:focus-within { border: tall $accent; }
    #etf-holdings-table { height: 1fr; border: none; }

    #etf-history-box {
        height: 1fr;
        layout: vertical;
    }
    #etf-history-title {
        height: 1;
        padding: 0 1;
        color: $accent;
        text-style: bold;
    }
    #etf-history-status {
        height: 1;
        padding: 0 1;
    }
    #etf-history-panel {
        height: 1fr;
        border: tall #334155;
    }
    #etf-history-panel:focus-within { border: tall $accent; }
    #etf-history-table { height: 1fr; border: none; }

    /* bug#00115：下方全寬 detail 分析框（不分頁，內嵌於本頁）。 */
    #etf-analysis-box {
        height: 2fr;
        margin: 1 2 1 2;
        border: tall #334155;
        background: #0d1117;
    }
    #etf-analysis-box:focus-within { border: tall $accent; }
    #etf-analysis-content { height: auto; padding: 0 1; }
    """

    def __init__(self, user: str, rate: float) -> None:
        super().__init__()
        self.user = user
        self.rate = rate
        self.etf_cache: dict[str, dict] = {}
        self.performance_data: dict = {}
        self.realtime_aums: dict[str, float] = {}
        self.us_symbols: list[str] = []
        self.selected_symbol: str | None = None
        # bug#00115：下方 detail 分析框的離線分析結果（背景 worker 算好後填入）。
        self._analysis_report: dict | None = None
        self._analysis_tilt: dict | None = None
        self._analysis_bt_consensus: dict | None = None
        self._analysis_bt_tilt: dict | None = None
        self._analysis_min_etfs: int = 4
        self._analysis_loaded: bool = False

    # ── Compose ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("", id="etf-header")
        with Horizontal(id="etf-body"):
            # Left half: ranking
            with Vertical(id="etf-left-col"):
                with TabbedContent(id="etf-left-tabbed"):
                    # bug#00091：投資建議一律以美股為主，台股主動式ETF排行已移除。
                    with TabPane("🇺🇸 美股主動型", id="tab-us-active"):
                        yield DataTable(id="etf-us-table")
            # Right half: split vertically
            with Vertical(id="etf-right-col"):
                # Top half: holdings
                with Vertical(id="etf-holdings-box"):
                    yield Static("當下持股細節", id="etf-holdings-title")
                    yield Static("", id="etf-holdings-status")
                    with Container(id="etf-holdings-panel"):
                        yield DataTable(id="etf-holdings-table")
                # Bottom half: history
                with Vertical(id="etf-history-box"):
                    yield Static("歷史買賣紀錄", id="etf-history-title")
                    yield Static("", id="etf-history-status")
                    with Container(id="etf-history-panel"):
                        yield DataTable(id="etf-history-table")
        # bug#00115：下方空白處內嵌 detail 分析框（全寬、不分頁、可捲動）。
        with ScrollableContainer(id="etf-analysis-box"):
            yield Static("", id="etf-analysis-content")
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
        us_t.add_columns("Symbol", "AUM", "YTD", "1Y", "最大持股")

        h_t = self.query_one("#etf-holdings-table", DataTable)
        h_t.cursor_type = "row"
        h_t.add_columns("Symbol", "名稱", "權重", "股數", "市值")

        tr_t = self.query_one("#etf-history-table", DataTable)
        tr_t.cursor_type = "row"
        tr_t.add_columns("日期", "操作", "Symbol", "股數", "價格", "權重△")

        self._set_header("⏳ 確認快取並載入資料...")
        self._set_mid_status("[dim]← 選取左欄 ETF 以查看持股[/dim]")
        self._set_right_status("[dim]← 選取左欄 ETF 以查看歷史[/dim]")
        self.query_one("#etf-analysis-content", Static).update(
            "[dim]⏳ 主動式ETF趨勢分析計算中（離線讀取本機真實快照）…[/dim]"
        )
        us_t.focus()

        # Run per-ETF cache retention cleanup in background (non-blocking).
        # Retention window is the single source of truth in storage
        # (ANALYSIS_CACHE_RETENTION_DAYS = 365, bug#00090).
        cleanup_old_etf_caches()

        # Load whatever is already cached for immediate display
        # bug#00091：僅美股主動式 ETF。
        all_symbols = US_ACTIVE_TICKERS

        # Trim each symbol's real daily-snapshot history log to the retention
        # window (365 days, storage.ANALYSIS_CACHE_RETENTION_DAYS) — a full year
        # of real snapshots for the walk-forward backtest, still bounded.
        for sym in all_symbols:
            prune_etf_history(sym)

        for sym in all_symbols:
            cached = load_etf_symbol_cache(sym)
            if cached:
                self.etf_cache[sym] = cached
                if "aum" in cached and cached["aum"] is not None:
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

        # Render immediately with whatever cache we have
        self._render_ranking_tables()

        # Automatically select the first symbol to view details
        if self.us_symbols:
            self._refresh_detail_panels(self.us_symbols[0])
            try:
                from textual.coordinate import Coordinate
                us_t.cursor_coordinate = Coordinate(0, 0)
            except Exception:
                pass

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
        snaps = {sym: load_etf_daily_snapshots(sym) for sym in US_ACTIVE_TICKERS}
        report = compute_symbol_trends(
            snaps, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ct)
        bt_consensus = backtest_etf_consensus(
            snaps, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS,
            consensus_threshold=_ct, min_etfs_evaluated=_me)
        tilt = compute_etf_selection_tilt(report)
        bt_tilt = backtest_etf_selection_tilt(
            snaps, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ct)
        self.app.call_from_thread(
            self._on_analysis_ready, report, bt_consensus, tilt, bt_tilt, _me)

    def _on_analysis_ready(self, report, bt_consensus, tilt, bt_tilt, min_etfs) -> None:
        self._analysis_report = report
        self._analysis_bt_consensus = bt_consensus
        self._analysis_tilt = tilt
        self._analysis_bt_tilt = bt_tilt
        self._analysis_min_etfs = min_etfs
        self._analysis_loaded = True
        self._render_analysis(self.selected_symbol)

    def _render_analysis(self, etf_symbol: "str | None") -> None:
        """把分析框內容重繪為：整體（每日主動選股多空＋跨ETF共識結論＋覆蓋率，永遠顯示）
        ＋（若選中某檔 ETF）該檔的主動選股明細與其跨基金共識持股。焦點切換只重繪、不重算。"""
        try:
            target = self.query_one("#etf-analysis-content", Static)
        except Exception:
            return
        if not self._analysis_loaded or self._analysis_report is None:
            target.update("[dim]⏳ 主動式ETF趨勢分析計算中…[/dim]")
            return

        report = self._analysis_report
        tilt = self._analysis_tilt or {}
        # bug#00119/00118：整框改為 markup 字串（支援 @click 公式細節連結），三層寫作格式。
        sections: list[str] = []
        mapping: dict = {}

        # ── 區塊 A：每日主動選股多空（整體）＋回測命中率（可點選公式細節） ──
        coverage = (
            f"[dim]資料收集進度：{report['etfs_ready_count']}/{report['etfs_total_count']} "
            f"檔 ETF 已有 ≥2 天真實快照（{report['etfs_ready_pct']:.0f}%）　"
            f"視窗 {report['window_days']} 天　更新於 {report['as_of']}[/dim]"
        )
        stance_recs = etf_stance_recommendation(tilt, backtest=self._analysis_bt_tilt)
        body_a, map_a = render_detail_recs(
            stance_recs, header="[bold cyan]🧭 每日主動選股多空（整體）[/bold cyan]", start=0)
        mapping.update(map_a)
        sections.append(f"{body_a}\n{coverage}")

        # ── 區塊 B：跨ETF持股趨勢共識結論（依據；與首頁卡片、進階分析頁同一函式，可點選公式細節） ──
        cons_recs = generate_etf_recommendations(
            report, positions=None, backtest=self._analysis_bt_consensus,
            min_etfs_evaluated=self._analysis_min_etfs)
        if cons_recs:
            body_b, map_b = render_detail_recs(
                cons_recs, header="[bold magenta]📝 跨ETF持股趨勢共識（依據）[/bold magenta]",
                start=len(stance_recs))
            mapping.update(map_b)
        else:
            body_b = ("[bold magenta]📝 跨ETF持股趨勢共識（依據）[/bold magenta]\n"
                      "[dim]尚無足夠真實資料生成跨ETF共識結論（需更多 ETF 累積 ≥2 天快照）。[/dim]")
        sections.append(body_b)

        self._recs_by_id = mapping

        # ── 區塊 C：選中某檔 ETF 的主動選股明細（純明細，無公式頁） ──
        etfs = tilt.get("etfs") or {}
        if etf_symbol and etf_symbol in etfs:
            d = etfs[etf_symbol]
            tilt_label = {"long": "🟢 偏多", "short": "🔴 偏空", "neutral": "⚪ 中性"}.get(d["tilt"], "⚪ 中性")
            buys = "、".join(d["top_buys"]) if d["top_buys"] else "—"
            sells = "、".join(d["top_sells"]) if d["top_sells"] else "—"
            sym_consensus = []
            for c in report.get("raw_contributions", []):
                if c["etf"] != etf_symbol or c["direction"] == "flat":
                    continue
                info = (report.get("symbols") or {}).get(c["symbol"], {})
                if info.get("consensus") in ("up", "down"):
                    arrow = "🟢買超" if info["consensus"] == "up" else "🔴賣超"
                    sym_consensus.append(f"{c['symbol']}（跨基金{arrow} {info['consensus_pct']:.0f}%一致）")
            consensus_line = ("　達跨基金共識：" + "；".join(dict.fromkeys(sym_consensus))) if sym_consensus else "　（本檔目前無達跨基金共識的持股）"
            sections.append(
                f"[bold green]🔍 {etf_symbol} 主動選股明細[/bold green]\n"
                f"傾向：{tilt_label}（淨分數 {d['net_score']:+.2f}；加碼 {d['up_n']} / 減碼 {d['down_n']} / 持平 {d['flat_n']}）\n"
                f"主要加碼：{buys}\n主要減碼：{sells}\n{consensus_line}"
            )
        else:
            sections.append("[dim]← 選取左欄某檔 ETF 可在此看它自己的主動選股傾向與共識持股[/dim]")

        target.update("\n\n".join(sections))

    def _set_header(self, status: str) -> None:
        from rich.panel import Panel as _Panel
        self.query_one("#etf-header", Static).update(
            _Panel(
                f"[bold cyan]📈 主動式 ETF 排行與持股分析[/bold cyan]  [dim]│[/dim]  {status}",
                border_style="cyan", padding=(0, 1),
            )
        )

    def _set_mid_title(self, text: str) -> None:
        self.query_one("#etf-holdings-title", Static).update(text)

    def _set_right_title(self, text: str) -> None:
        self.query_one("#etf-history-title", Static).update(text)

    def _set_mid_status(self, text: str) -> None:
        self.query_one("#etf-holdings-status", Static).update(text)

    def _set_right_status(self, text: str) -> None:
        self.query_one("#etf-history-status", Static).update(text)

    # ── Keyboard Navigation ───────────────────────────────────────────────────

    def on_key(self, event) -> None:
        from textual.widgets import Tabs
        
        # Right arrow to move rightwards across columns
        if event.key == "right":
            focused = self.focused
            if isinstance(focused, DataTable):
                if focused.id == "etf-us-table":
                    self.query_one("#etf-holdings-table", DataTable).focus()
                    event.prevent_default()
                    event.stop()
                elif focused.id == "etf-holdings-table":
                    self.query_one("#etf-history-table", DataTable).focus()
                    event.prevent_default()
                    event.stop()
        # Left arrow to move leftwards across columns
        elif event.key == "left":
            focused = self.focused
            if isinstance(focused, DataTable):
                if focused.id == "etf-holdings-table":
                    self.query_one("#etf-us-table", DataTable).focus()
                    event.prevent_default()
                    event.stop()
                elif focused.id == "etf-history-table":
                    self.query_one("#etf-holdings-table", DataTable).focus()
                    event.prevent_default()
                    event.stop()
        # Up arrow at the very top of lists to jump focus to the tab headers
        elif event.key == "up":
            focused = self.focused
            if isinstance(focused, DataTable) and focused.id == "etf-us-table":
                if focused.cursor_row == 0:
                    try:
                        self.query_one(Tabs).focus()
                        event.prevent_default()
                        event.stop()
                    except Exception:
                        pass
        # Down arrow on tab headers to jump focus to the list table
        elif event.key == "down":
            focused = self.focused
            if isinstance(focused, Tabs):
                try:
                    self.query_one("#etf-us-table", DataTable).focus()
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

        all_symbols = US_ACTIVE_TICKERS  # bug#00091：僅美股主動式 ETF

        # ── 1. Identify stale symbols ─────────────────────────────────────────
        stale_symbols = [sym for sym in all_symbols if not etf_symbol_cache_fresh(sym)]

        if not stale_symbols:
            self.app.call_from_thread(
                self._set_header, "[green]✅ 快取皆為今日最新，已直接載入[/green]"
            )
            return

        self.app.call_from_thread(
            self._set_header, f"⏳ 正在背景更新 {len(stale_symbols)} 個 ETF 的即時數據..."
        )

        result = _fetch_and_cache_etf_symbols(stale_symbols)

        aums = dict(self.realtime_aums)
        aums.update(result["aums"])
        perf = dict(self.performance_data)
        perf.update(result["perf"])
        etf_cache = dict(self.etf_cache)
        etf_cache.update(result["etf_cache"])

        self.app.call_from_thread(
            self._on_fetch_complete, aums, perf, etf_cache, result["perf_fail_count"], len(stale_symbols)
        )

    def _on_fetch_complete(
        self,
        aums: dict[str, float],
        perf: dict[str, dict],
        etf_cache: dict[str, dict],
        perf_fail_count: int = 0,
        perf_attempted_count: int = 0,
    ) -> None:
        self.realtime_aums = aums
        self.performance_data = perf
        self.etf_cache = etf_cache
        if perf_fail_count > 0:
            # bug#00058: surface partial performance-fetch failures instead of
            # silently showing "—" with no explanation of why.
            self._set_header(
                f"[yellow]⚠️ 即時數據載入完成，但 {perf_fail_count}/{perf_attempted_count} 檔 ETF 績效抓取失敗"
                f"（將於下次刷新自動重試）[/yellow]"
            )
        else:
            self._set_header("[green]✅ 即時數據載入完成[/green]")
        self._render_ranking_tables()
        sym = self.selected_symbol or (self.us_symbols[0] if self.us_symbols else None)
        if sym:
            self._refresh_detail_panels(sym)

    # ── Render ranking tables (left col) ──────────────────────────────────────

    def _render_ranking_tables(self) -> None:
        new_us: list[str] = []
        self._render_one_tab("#etf-us-table", US_ACTIVE_TICKERS, new_us)
        self.us_symbols = new_us

    def _render_one_tab(
        self,
        selector: str,
        universe: list[str],
        out_symbols: list[str],
    ) -> None:
        table = self.query_one(selector, DataTable)
        table.clear(columns=False)

        # Sort purely by AUM descending
        by_aum = sorted(
            universe, key=lambda s: self.realtime_aums.get(s, 0.0), reverse=True
        )[:_ETF_TOP_N]

        for symbol in by_aum:
            is_tw = symbol.endswith(".TW") or symbol.endswith(".TWO")
            aum_s = self._fmt_aum(self.realtime_aums.get(symbol), is_tw=is_tw)
            p = self.performance_data.get(symbol, {})
            holdings = self.etf_cache.get(symbol, {}).get("holdings", [])

            # Yellow star prefix for the one non-ETF mutual fund in the universe
            is_special = symbol == "SEQUX"
            sym_display = f"[bold yellow]★ {symbol}[/bold yellow]" if is_special else f"[bold white]{symbol}[/bold white]"

            if holdings:
                top_h = max(holdings, key=lambda h: h.get("weight", 0.0))
                w = top_h.get("weight", 0.0)
                top_h_s = f"[dim]{top_h.get('symbol', '—')} ({w:.1f}%)[/dim]"
            else:
                top_h_s = "[dim]—[/dim]"

            table.add_row(
                sym_display,
                f"[dim]{aum_s}[/dim]",
                self._fmt_pct(p.get("return_ytd")),
                self._fmt_pct(p.get("return_1y")),
                top_h_s,
            )
            out_symbols.append(symbol)

    # ── Detail panels (middle + right cols) ───────────────────────────────────

    def _refresh_detail_panels(self, symbol: str) -> None:
        self.selected_symbol = symbol
        cached = self.etf_cache.get(symbol, {})
        fund_name = cached.get("name") or symbol
        self._set_mid_title(f"[bold cyan]{symbol}[/bold cyan]  [dim]{fund_name}[/dim]  當下持股細節")
        self._set_right_title(f"[bold cyan]{symbol}[/bold cyan]  歷史買賣紀錄")
        self._set_mid_status(f"[yellow]⏳ 載入 {symbol} 持股...[/yellow]")
        self._set_right_status(f"[yellow]⏳ 載入 {symbol} 歷史...[/yellow]")
        self._render_holdings(symbol)
        self._render_history(symbol)

    def _render_holdings(self, symbol: str) -> None:
        table = self.query_one("#etf-holdings-table", DataTable)
        table.clear(columns=False)

        info = self.etf_cache.get(symbol, {})
        holdings = info.get("holdings", [])
        asset_classes = info.get("asset_classes") or {}
        as_of = info.get("holdings_as_of_date", "")

        if not holdings and not asset_classes:
            self._set_mid_status(
                f"[dim]{symbol} 持股資料更新中，或 yfinance 未提供此 ETF 持股[/dim]"
            )
            return

        date_badge = (
            f"[green]✅ 資訊更新日: {as_of}[/green]" if as_of
            else "[dim]資訊更新日: 未知[/dim]"
        )
        self._set_mid_status(date_badge)

        aum = info.get("aum")
        is_tw = symbol.endswith(".TW") or symbol.endswith(".TWO")

        # Section 1: whole-fund stock/bond/cash/preferred/convertible/other split.
        # `holdings` below is only Yahoo's curated top-N *named* positions (mostly
        # equities); a fund with a meaningful cash/bond/options-overlay sleeve would
        # otherwise look 100% stock-only. This surfaces the real full composition.
        if asset_classes:
            table.add_row("[bold cyan]▾ 資產配置[/bold cyan]", "[dim]整體基金比例[/dim]", "", "", "")
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
                table.add_row("[bold cyan]▾ 前十大持股[/bold cyan]", "[dim]個股持有明細[/dim]", "", "", "")
            for h in holdings:
                w = h.get("weight")
                s = h.get("shares")
                mv_s = "—"
                if aum and w:
                    mv_s = self._fmt_aum(aum * (w / 100.0), is_tw=is_tw)
                table.add_row(
                    f"[bold white]{h.get('symbol', '—')}[/bold white]",
                    f"[dim]{h.get('name', '—')}[/dim]",
                    f"{w:.2f}%" if w is not None else "—",
                    f"{int(s):,}" if s is not None else "—",
                    mv_s,
                )

    def _render_history(self, symbol: str) -> None:
        table = self.query_one("#etf-history-table", DataTable)
        table.clear(columns=False)

        history = self.etf_cache.get(symbol, {}).get("history", [])
        if history:
            self._set_right_status(
                f"[green]✅ {symbol} 歷史交易 ({len(history)} 筆)[/green]"
            )
            for h in history:
                action = h.get("action", "—")
                a_col = "green" if action == "BUY" else "red"
                wc = h.get("weight_change")
                wc_s = "—"
                if wc is not None:
                    col = "green" if wc >= 0 else "red"
                    sign = "+" if wc >= 0 else ""
                    wc_s = f"[{col}]{sign}{wc:.2f}%[/{col}]"
                price = h.get("price")
                shares = h.get("shares")
                table.add_row(
                    h.get("date", "—"),
                    f"[{a_col}]{action}[/{a_col}]",
                    f"[bold white]{h.get('symbol', '—')}[/bold white]",
                    f"{int(shares):,}" if shares is not None else "—",
                    f"${price:,.2f}" if price is not None else "—",
                    wc_s,
                )
        else:
            self._set_right_status(
                f"[dim]{symbol} 無歷史交易紀錄 "
                f"(ETF 持股調整紀錄需由外部 scraper 寫入)[/dim]"
            )

    # ── Unified row navigation ─────────────────────────────────────────────────

    def _handle_row(self, table_id: str, row_idx: int) -> None:
        if table_id == "etf-us-table" and 0 <= row_idx < len(self.us_symbols):
            sym = self.us_symbols[row_idx]
            self._refresh_detail_panels(sym)
            # bug#00115：選中 ETF 時，下方分析框改繪該檔的主動選股明細（若分析已算好）。
            if self._analysis_loaded:
                self._render_analysis(sym)

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
        """清除快取：刪除快取檔案並在背景發起全新的即時抓取載入流程。"""
        from .storage import get_etf_cache_dir
        cache_dir = get_etf_cache_dir()
        if cache_dir.exists():
            for f in cache_dir.glob("*.json"):
                try:
                    f.unlink()
                except Exception:
                    pass
        # Clear memory caches
        self.etf_cache.clear()
        self.realtime_aums.clear()
        self.performance_data.clear()
        self.us_symbols.clear()
        self.selected_symbol = None
        
        # Clear UI tables
        self.query_one("#etf-us-table", DataTable).clear(columns=False)
        self.query_one("#etf-holdings-table", DataTable).clear(columns=False)
        self.query_one("#etf-history-table", DataTable).clear(columns=False)
        
        # Reset headers and statuses
        self._set_header("⏳ 快取已清除，正在重新抓取全部新數據...")
        self._set_mid_status("[dim]← 快取已清除，等待重新載入[/dim]")
        self._set_right_status("[dim]← 快取已清除，等待重新載入[/dim]")
        
        # Kick off background fetch
        self.run_background_fetch()

    def action_advanced_analysis(self) -> None:
        """[a] 進階分析：離線讀取本機已累積的真實每日快照，計算跨ETF持股趨勢共識。"""
        self.app.push_screen(AdvancedAnalysisScreen(self.user))


# ─────────────────────────────────────────────────────────────────────────────
# Advanced Analysis Screen (進階分析)
# ─────────────────────────────────────────────────────────────────────────────
# bug#00060 / bug#00104: 100% 離線運算 —— 只讀取 storage.py 已在背景刷新時逐日真實累積下來的
# per-ETF 快照（etf_cache/history/*.jsonl）。14 天視窗內若某檔 ETF 累積不足 2 筆真實快照，
# 就不會被納入計算，並誠實在畫面上顯示目前的資料收集進度。

ADVANCED_ANALYSIS_WINDOW_DAYS = 14


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
        border: round #d070d0;
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
        table.add_columns("股票代碼", "共識方向", "共識比例", "看漲ETF", "看跌ETF", "持平ETF", "估計總股數變化")
        table.display = False
        self.query_one("#aa-empty", Static).display = False

        self._run_analysis()

    def _run_analysis(self) -> None:
        # 投資建議一律以美股為主（bug#00091）：跨ETF趨勢共識只納入美股主動式 ETF。
        # bug#00095 接線：套用已確認校準參數。
        _ap = _active_params(self.user).get('etf', {})
        _ct = _ap.get('consensus_threshold', 0.5)
        _me = _ap.get('min_etfs_evaluated', 4)
        all_symbols = US_ACTIVE_TICKERS
        snapshots_by_etf = {sym: load_etf_daily_snapshots(sym) for sym in all_symbols}

        report = compute_symbol_trends(snapshots_by_etf, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ct)
        # bug#00092: walk-forward 回測（與結論卡共用同一套邏輯）。
        _bt = backtest_etf_consensus(snapshots_by_etf, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS, consensus_threshold=_ct, min_etfs_evaluated=_me)
        ranked = rank_symbol_trends(report, min_etfs_evaluated=_me)

        coverage_line = (
            f"[bold cyan]📊 進階分析 — 跨ETF持股趨勢共識[/bold cyan]  [dim]│[/dim]  "
            f"視窗 {report['window_days']} 天　"
            f"資料收集進度：{report['etfs_ready_count']}/{report['etfs_total_count']} 檔 ETF 已有 ≥2 天真實快照 "
            f"({report['etfs_ready_pct']:.0f}%)　更新於 {report['as_of']}"
        )
        from rich.panel import Panel as _Panel
        self.query_one("#aa-header", Static).update(
            _Panel(coverage_line, border_style="cyan", padding=(0, 1))
        )

        # bug#00061 / bug#00119: 結論區塊 — 多數性 + 規模性，三層寫作格式（結論／判斷依據／
        # 可點選公式細節），與 Dashboard 首頁卡片共用同一份 generate_etf_recommendations()。
        recs = generate_etf_recommendations(report, backtest=_bt, min_etfs_evaluated=_me)
        from .calibration import calibration_status_label
        _bt_status = calibration_status_label(_bt)
        w = self.query_one("#aa-conclusions", Static)
        if recs:
            header = f"[dim]回測校準狀態：{_bt_status}[/dim]"
            conclusion_body, mapping = render_detail_recs(recs, header=header)
            self._recs_by_id = mapping
        else:
            conclusion_body = "[dim]目前尚無足夠真實資料可生成結論（需要更多 ETF 累積 ≥2 天快照）。[/dim]"
            self._recs_by_id = {}
        w.border_title = "📝 結論（點『🔍 查看公式細節』看公式與計算）"
        w.update(conclusion_body)

        table = self.query_one("#aa-table", DataTable)
        empty = self.query_one("#aa-empty", Static)

        if not ranked:
            table.display = False
            empty.display = True
            if report["etfs_ready_count"] == 0:
                empty.update(
                    "[yellow]目前所有主動式 ETF 均尚未累積滿 2 天的真實每日持股快照，"
                    "無法計算任何趨勢。[/yellow]\n\n"
                    "[dim]本功能只使用系統背景刷新時真實記錄下來的每日持股快照（不會回填或"
                    "捏造歷史資料），每次你開啟「主動式ETF排行」畫面且該ETF快取過期時，"
                    "就會多累積一筆真實紀錄。請持續使用幾天後再回來查看，資料越多、"
                    "趨勢判讀會越準確。[/dim]"
                )
            else:
                empty.update(
                    "[dim]目前已有部分 ETF 累積到足夠資料，但尚未出現任何跨 ETF 一致的"
                    "買進/賣出共識（或個股僅被單一 ETF 持有，未達最低比較門檻）。"
                    "隨著資料持續累積，此頁面會自動更新。[/dim]"
                )
            return

        table.display = True
        empty.display = False
        table.clear(columns=False)

        for sym, info in ranked:
            consensus = info["consensus"]
            if consensus == "up":
                dir_s = "[bold green]▲ 買超[/bold green]"
            elif consensus == "down":
                dir_s = "[bold red]▼ 賣超[/bold red]"
            else:
                dir_s = "[dim]分歧[/dim]"

            delta = info["est_total_share_delta"]
            delta_s = f"~{delta:,} 股" if delta is not None else "—"

            table.add_row(
                f"[bold white]{sym}[/bold white]",
                dir_s,
                f"{info['consensus_pct']:.0f}%",
                f"[green]{len(info['etfs_up'])}[/green]",
                f"[red]{len(info['etfs_down'])}[/red]",
                f"[dim]{len(info['etfs_flat'])}[/dim]",
                delta_s,
            )

    def action_go_back(self) -> None:
        self.dismiss()


# ─────────────────────────────────────────────────────────────────────────────
# Options Watchlist Screen (期權觀察清單)
# ─────────────────────────────────────────────────────────────────────────────
# bug#00061 / bug#00066: 期權觀察清單。標的來源 = 持倉自動帶入 ∪ 使用者自訂新增
# （storage.load/save_options_watchlist），每日真實累積價內外 ≤60 天到期合約的快照
# （quotes.fetch_options_snapshot）。右欄顯示每張合約的未平倉量與希臘字母（greeks.py
# 就地以 Black-Scholes 計算，無風險利率取 ^IRX）與損益兩平點；投資建議則以
# options_analysis.compute_iv_divergence「排除當日股價變動因素後」偵測期權異常震盪
# 與背離。100% 離線運算，資料不足時誠實顯示收集進度，絕不回填或捏造。

OPTIONS_FLOW_WINDOW_DAYS = 14


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
                    earnings_date=earnings.get(u.upper()),
                )


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
        save_sector_summaries_cache,
    )
    from . import sector_analysis
    from .quotes import fetch_sector_members_data

    groups = load_sector_groups(user)
    if not groups:
        return {}
    union = sorted({s for members in groups.values() for s in members})
    data = fetch_sector_members_data(union)

    summaries: dict[str, dict] = {}
    for name, members in groups.items():
        prune_sector_history(name)
        summary = sector_analysis.summarize_group(data, members)
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
    return summaries


class SectorGroupModal(ModalScreen[Optional[dict]]):
    """新增 / 編輯 / 刪除板塊群組 (item#3)。回傳：
      {"action":"save","name":str,"members":[symbol,...]} 儲存（新增或編輯，含改名）；
      {"action":"delete"} 刪除此板塊（僅編輯模式）；None 取消。
    成分股以空白或逗號分隔輸入，一律轉大寫並去重（保留輸入順序）。"""
    DEFAULT_CSS = """
    SectorGroupModal { align: center middle; }
    #sg-dialog { width: 64; height: auto; border: thick #58a6ff; background: #161b22; padding: 1 2; }
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
        title = "➕ [bold]新增板塊[/bold]" if self.mode == "add" else "✏️ [bold]編輯板塊[/bold]"
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
            self.query_one("#sg-error", Label).update("❌ 請輸入板塊名稱")
            return
        if not members:
            self.query_one("#sg-error", Label).update("❌ 請至少輸入一檔成分股")
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
        Binding("escape", "go_back", "返回看板"),
        Binding("q",      "go_back", "返回看板", show=False),
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
    #sec-conclusions { height: auto; max-height: 12; padding: 0 1; margin: 0 2 1 2; border: round #d070d0; }
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
        self._set_header("⏳ 載入板塊即時數據..." if not self.summaries else "[dim]顯示快取數據，檢查是否需更新...[/dim]")
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
            _Panel(
                f"[bold cyan]📊 類股板塊分析[/bold cyan]  [dim]│[/dim]  {status}",
                border_style="cyan", padding=(0, 1),
            )
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
            return f"[green]📈 普遍漲 {f['up_days']}/{f['days_evaluated']}[/green]"
        if f["direction"] == "down":
            return f"[red]📉 普遍跌 {f['down_days']}/{f['days_evaluated']}[/red]"
        return "[dim]—[/dim]"

    # ── Breadth flows (offline) ─────────────────────────────────────────────────

    def _recompute_flows(self) -> None:
        from .storage import load_sector_daily_snapshots
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
        """底部「類股投資建議」區塊 (bug#00082 / bug#00119)：三層寫作格式（結論／判斷依據／
        可點選公式細節），與 Dashboard「類股共識」卡片共用同一份 generate_sector_recommendations()
        輸出、兩處一致。尚無足夠真實快照時誠實顯示收集進度，不生成假結論。"""
        from . import sector_analysis

        _sec_bt = getattr(self, "_sector_backtest", None)
        recs = sector_analysis.generate_sector_recommendations(self.flows, backtest=_sec_bt)
        w = self.query_one("#sec-conclusions", Static)
        w.border_title = "📝 類股投資建議（每日累計廣度共識 · 點『🔍 查看公式細節』看公式）"
        if recs:
            from .calibration import calibration_status_label
            _status = calibration_status_label(_sec_bt) if _sec_bt else "資料累積中"
            body, mapping = render_detail_recs(recs, header=f"[dim]回測校準狀態：{_status}[/dim]")
            self._recs_by_id = mapping
        else:
            ready = sum(1 for f in self.flows.values() if f.get("ready"))
            total = len(self.flows)
            body = (
                f"[dim]資料收集中：{ready}/{total} 個板塊已有足夠真實快照，尚無「普遍」共識訊號。"
                f"每日累計追蹤各板塊是否普遍上漲/下跌，持續運行系統會逐日累積廣度資料。[/dim]"
            )
            self._recs_by_id = {}
        w.update(body)

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
            cached = load_sector_summaries_cache(self.user).get("summaries") or {}
            if cached:
                self.app.call_from_thread(self._on_fetch_complete, cached, True)
                return

        self.app.call_from_thread(self._set_header, "⏳ 正在更新各板塊成分股即時數據...")
        summaries = _fetch_and_cache_sector_groups(self.user)
        self.app.call_from_thread(self._on_fetch_complete, summaries, False)

    def _on_fetch_complete(self, summaries: dict, from_cache: bool = False) -> None:
        # 載入新價前保留上一筆最後資料：抓取結果為空（無群組/失敗）時不清空畫面。
        if summaries:
            self.summaries = summaries
        self._recompute_flows()
        if from_cache:
            from .storage import load_sector_summaries_cache
            ts_raw = load_sector_summaries_cache(self.user).get("last_refreshed") or ""
            try:
                self._updated_at = datetime.fromisoformat(ts_raw)
            except (ValueError, TypeError):
                self._updated_at = None
            ts = self._updated_at.strftime("%Y-%m-%d %H:%M") if self._updated_at else "—"
            self._set_header(f"[green]✅ 已載入快取數據[/green] [dim]（{ts} 更新）[/dim]")
        else:
            from .storage import taiwan_now
            self._updated_at = taiwan_now()
            ts = self._updated_at.strftime("%Y-%m-%d %H:%M")
            self._set_header(f"[green]✅ 板塊即時數據載入完成[/green] [dim]（{ts}）[/dim]")
        self._render_groups()

    # ── Actions ──────────────────────────────────────────────────────────────────

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_refresh_now(self) -> None:
        """手動重新整理 (bug#00083)：略過快取新鮮度判定，強制重新抓取——用於快取被節流
        時抓到不完整資料、市場休市又無法自動重抓的情況。"""
        self._set_header("⏳ 手動重新整理中，強制重新抓取即時數據...")
        self.run_background_fetch(force=True)

    # ── Group create / edit / delete (item#3) ────────────────────────────────────

    def action_add_group(self) -> None:
        self.app.push_screen(SectorGroupModal(mode="add"), self._on_group_modal_result)

    def action_edit_group(self) -> None:
        if not self.selected_group:
            self.app.notify("⚠️ 請先於左欄選取要編輯的板塊", severity="warning")
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
            self.app.notify("⚠️ 請先於左欄選取要刪除的板塊", severity="warning")
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
                self.app.notify(f"🗑️ 已刪除板塊「{original}」")
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
        self.app.notify(f"✅ 已{'更新' if original else '新增'}板塊「{name}」")
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
            self._set_header("⏳ 重新抓取板塊即時數據...")
            self.run_background_fetch()
        else:
            self._set_header("[dim]尚無任何板塊，按 [bold]a[/bold] 新增[/dim]")


class AddTickerModal(ModalScreen[Optional[str]]):
    """輸入要加入期權觀察清單的標的代碼 (bug#00066)。"""
    DEFAULT_CSS = """
    AddTickerModal { align: center middle; }
    #at-dialog { width: 52; height: auto; border: thick #58a6ff; background: #161b22; padding: 1 2; }
    #at-title { text-style: bold; color: #58a6ff; margin-bottom: 1; }
    #at-input { margin-bottom: 1; border: solid #30363d; background: #0d1117; }
    #at-input:focus { border: solid #58a6ff; }
    #at-error { color: #ff7b72; height: auto; margin-bottom: 1; }
    #at-buttons { height: auto; align: right middle; }
    #at-buttons Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="at-dialog"):
            yield Label("➕ [bold]新增觀察標的[/bold]", id="at-title")
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

    def on_input_submitted(self, event) -> None:
        self._submit()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)

    def _submit(self) -> None:
        val = self.query_one("#at-input", Input).value.strip().upper()
        if not val:
            self.query_one("#at-error", Label).update("❌ 請輸入標的代碼")
            return
        self.dismiss(val)


class RemoveTickerModal(ModalScreen[Optional[str]]):
    """自使用者額外新增的標的中選擇一個移除（持倉自動帶入的標的不可移除）。"""
    DEFAULT_CSS = """
    RemoveTickerModal { align: center middle; }
    #rt-dialog { width: 52; height: auto; border: thick red; background: #161b22; padding: 1 2; }
    #rt-title { text-style: bold; color: red; margin-bottom: 1; }
    #rt-list { height: auto; max-height: 16; border: solid $accent; }
    """

    def __init__(self, removable: list[str]) -> None:
        super().__init__()
        self.removable = removable

    def compose(self) -> ComposeResult:
        opts = [Option(t, id=t) for t in self.removable]
        opts.append(Option("❌ 取消", id="__cancel__"))
        with Vertical(id="rt-dialog"):
            yield Label("🗑️ [bold]移除觀察標的（僅限自訂新增）[/bold]", id="rt-title")
            yield OptionList(*opts, id="rt-list")

    def on_mount(self) -> None:
        self.query_one("#rt-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        val = event.option.id
        self.dismiss(None if val == "__cancel__" else val)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class OptionsWatchlistScreen(_FormulaDrillMixin, Screen):
    """期權觀察清單 —— 標的自管理 + 價內外 ≤60 天合約的希臘字母與異常震盪/背離偵測。"""

    BINDINGS = [
        Binding("a",      "add_ticker",    "新增標的"),
        Binding("d",      "remove_ticker", "刪除標的"),
        Binding("k",      "calibration",   "校準狀態"),
        Binding("h",      "help",          "說明"),
        Binding("c",      "clear_cache",   "重抓今日"),
        Binding("escape", "go_back",       "返回看板"),
        Binding("q",      "go_back",       "返回看板", show=False),
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
        /* bug#00089 捲動修正：結論卡內容變長時，上方區塊原本會把下方標的清單/
           合約明細擠出畫面且無捲軸可用。改以可捲動容器包住「期權分析總表＋分析
           結論卡」：內容短時只占實際高度(height:auto)，過長時封頂並出現捲軸
           （滑鼠滾輪或點擊後方向鍵皆可捲動），#ow-body 永遠保有其餘空間。
           bug#00119：上限由 65% 降到 42%，避免上方把下方買/賣權合約表壓到過小；
           內容超出時在此區塊內捲動即可。 */
        height: auto;
        max-height: 42%;
    }
    #ow-top:focus-within { border-left: tall $accent; }
    #ow-portfolio {
        height: auto;
        padding: 0 1;
        margin: 1 2 0 2;
    }
    #ow-verdicts {
        height: auto;
        padding: 0 1;
        margin: 1 2 0 2;
        border: round #d070d0;
    }
    #ow-body {
        /* bug#00119：買/賣權合約表區——1fr 吃滿 #ow-top 以外的空間，並保底 14 列
           高度，確保下方合約明細的可視範圍不會被上方壓縮到過小。 */
        height: 1fr;
        min-height: 14;
        layout: horizontal;
        margin: 1 2;
    }
    #ow-left-col {
        width: 28%;
        height: 1fr;
        border: tall #334155;
        margin-right: 1;
    }
    #ow-left-col:focus-within { border: tall $accent; }
    #ow-list-table { height: 1fr; border: none; }
    #ow-right-col {
        width: 72%;
        height: 1fr;
        layout: horizontal;
    }
    #ow-calls-col {
        width: 1fr;
        height: 1fr;
        border: tall #1c3320;
        margin-right: 1;
    }
    #ow-calls-col:focus-within { border: tall $success; }
    #ow-puts-col {
        width: 1fr;
        height: 1fr;
        border: tall #331c1c;
    }
    #ow-puts-col:focus-within { border: tall $error; }
    #ow-calls-label { height: 1; padding: 0 1; background: #1c3320; color: #4ade80; }
    #ow-puts-label  { height: 1; padding: 0 1; background: #331c1c; color: #f87171; }
    #ow-calls-table { height: 1fr; border: none; }
    #ow-puts-table  { height: 1fr; border: none; }
    """

    def __init__(self, user: str, positions: list[Position]) -> None:
        super().__init__()
        self.user = user
        self.positions = positions
        self.underlyings, self.pos_set, self.extra_set = _watchlist_underlyings(user, positions)
        self.report: dict = {}
        self.iv_pct: dict = {}  # {underlying: compute_iv_percentile(...)}
        self.expected_move: dict = {}  # {underlying: compute_expected_move(...)}
        self.spot_by_underlying: dict = {}  # underlying -> spot, for portfolio Greeks
        self.selected_underlying: Optional[str] = None
        self.r: float = 0.04  # risk-free rate; refreshed from ^IRX in background

    def compose(self) -> ComposeResult:
        yield Static("", id="ow-header")
        with ScrollableContainer(id="ow-top"):
            yield Static("", id="ow-portfolio")
            yield Static("", id="ow-verdicts")
        with Horizontal(id="ow-body"):
            with Container(id="ow-left-col"):
                yield DataTable(id="ow-list-table")
            with Horizontal(id="ow-right-col"):
                with Container(id="ow-calls-col"):
                    yield Static("[bold green]📗 買權 Call[/bold green]", id="ow-calls-label")
                    yield DataTable(id="ow-calls-table")
                with Container(id="ow-puts-col"):
                    yield Static("[bold red]📕 賣權 Put[/bold red]", id="ow-puts-label")
                    yield DataTable(id="ow-puts-table")
        yield Footer()

    def on_mount(self) -> None:
        list_t = self.query_one("#ow-list-table", DataTable)
        list_t.cursor_type = "row"
        list_t.add_columns("標的", "來源", "異常", "IV位階")

        _g_cols = ("履約價", "價內外", "DTE", "未平倉", "IV", "Δ", "Γ", "Θ", "損益兩平")
        ct = self.query_one("#ow-calls-table", DataTable)
        ct.cursor_type = "row"
        ct.add_columns(*_g_cols)
        pt = self.query_one("#ow-puts-table", DataTable)
        pt.cursor_type = "row"
        pt.add_columns(*_g_cols)

        for u in self.underlyings:
            prune_options_history(u)

        self._set_header(f"⏳ 載入 {len(self.underlyings)} 檔標的資料...")
        self._render_list()
        self._run_analysis()
        list_t.focus()
        self.run_background_fetch()

    def _set_header(self, status: str) -> None:
        from rich.panel import Panel as _Panel
        self.query_one("#ow-header", Static).update(
            _Panel(
                f"[bold cyan]🎯 期權觀察清單 — 希臘字母與異常震盪偵測[/bold cyan]  "
                f"[dim]│[/dim]  [dim]a 新增　d 刪除　k 校準　h 說明　c 重抓今日[/dim]  [dim]│[/dim]  {status}",
                border_style="cyan", padding=(0, 1),
            )
        )

    def _render_portfolio(self) -> None:
        """bug#00073: 各標的期權分析總表 —— **以觀察清單標的為列**（新增標的會即時多一列），
        每列顯示由價平跨式估算的『預期波動區間』、ATM IV，以及若你持有該標的選擇權時的
        『淨 Greeks』(僅選擇權、逐標的、bug#00071/72)。現股不納入 Greeks。"""
        from rich.panel import Panel as _Panel
        from rich.table import Table as _Table
        from rich.console import Group as _Group
        from rich.text import Text as _Text

        if not self.underlyings:
            self.query_one("#ow-portfolio", Static).update(
                _Panel("[dim]清單為空，按 a 新增標的[/dim]",
                       title="[bold]📊 各標的期權分析[/bold]", border_style="green", padding=(0, 1))
            )
            return

        pg = compute_portfolio_greeks(self.positions, self.spot_by_underlying, r=self.r, options_only=True)
        by_u = pg["by_underlying"]
        total = pg["total"]

        def _money(v: float, signed: bool = True) -> str:
            c = "green" if v >= 0 else "red"
            sign = "+" if (signed and v >= 0) else ""
            return f"[{c}]{sign}${v:,.0f}[/{c}]"

        # bug#00119: 每列單行呈現——把「損益兩平」由原本的第二行改成獨立欄位，
        # 避免預期波動格換行成兩行、壓縮下方買賣權表的可視高度。
        tbl = _Table(show_header=True, header_style="bold", box=None, expand=True, padding=(0, 1))
        tbl.add_column("標的")
        tbl.add_column("預期波動 ±1σ", justify="right")
        tbl.add_column("損益兩平", justify="right")
        tbl.add_column("ATM IV", justify="right")
        tbl.add_column("持倉Δ$", justify="right")
        tbl.add_column("Θ/日", justify="right")
        tbl.add_column("Vega", justify="right")

        MAX_ROWS = 10
        shown = self.underlyings[:MAX_ROWS]
        for u in shown:
            em = self.expected_move.get(u)
            if em and em.get("sigma_abs") is not None:
                warn = " [yellow]⚠[/yellow]" if em.get("low_confidence") else ""
                em_s = (f"[white]±${em['sigma_abs']:.2f}[/white] "
                        f"[dim](±{em['sigma_pct']:.1f}%,{em['dte']}d)[/dim]{warn}")
                be_s = f"[dim]±${em['breakeven_abs']:.2f}[/dim]"
                iv_s = f"{em['atm_iv'] * 100:.0f}%{warn}" if em.get("atm_iv") else "—"
            else:
                em_s = "[dim]資料收集中[/dim]"
                be_s = "[dim]—[/dim]"
                iv_s = "—"

            g = by_u.get(u)
            if g and g["priced"] > 0:
                d_s = _money(g["delta_dollars"], signed=False)
                t_s = _money(g["theta_day"])
                v_s = f"${g['vega_1pt']:,.0f}"
                held = " [magenta]◆[/magenta]"
            else:
                d_s = t_s = v_s = "[dim]—[/dim]"
                held = ""
            tbl.add_row(f"[bold white]{u}[/bold white]{held}", em_s, be_s, iv_s, d_s, t_s, v_s)

        # 持倉選擇權 Greeks 合計（僅在有持倉時顯示）
        if total["priced"] > 0:
            tbl.add_row(
                "[dim]— 持倉選擇權合計 —[/dim]", "", "", "",
                _money(total["delta_dollars"], signed=False),
                _money(total["theta_day"]),
                f"${total['vega_1pt']:,.0f}",
            )

        foot = ["[dim]±1σ＝現價×IV×√(DTE/365)；損益兩平＝價平跨式價；[yellow]⚠[/yellow]＝低可信度；"
                "◆＝持有該標的選擇權（Δ$/Θ/Vega 淨值，現股不計）[/dim]"]
        if len(self.underlyings) > len(shown):
            foot.append(f"[dim]… 另有 {len(self.underlyings) - len(shown)} 檔未列出（清單前 {MAX_ROWS} 檔）[/dim]")
        if total["unpriced"]:
            foot.append(f"[dim][yellow]*[/yellow] {len(total['unpriced'])} 筆選擇權無法定價（缺現價/無法反解 IV），未計入合計[/dim]")

        self.query_one("#ow-portfolio", Static).update(
            _Panel(_Group(tbl, _Text.from_markup("\n".join(foot))),
                   title="[bold]📊 各標的期權分析（預期波動 + 你的選擇權淨 Greeks）[/bold]",
                   border_style="green", padding=(0, 1))
        )

    # ── Left list ─────────────────────────────────────────────────────────────

    def _render_list(self) -> None:
        list_t = self.query_one("#ow-list-table", DataTable)
        list_t.clear(columns=False)
        if not self.underlyings:
            list_t.add_row("[dim]清單為空，按 a 新增標的[/dim]", "", "", "")
            return
        div_by_u: dict[str, int] = {}
        for e in self.report.get("events", []):
            div_by_u[e["underlying"]] = div_by_u.get(e["underlying"], 0) + 1
        for u in self.underlyings:
            src = "[cyan]📌持倉[/cyan]" if u in self.pos_set else "[magenta]➕自訂[/magenta]"
            n = div_by_u.get(u, 0)
            nb = f"[yellow]{n}[/yellow]" if n else "[dim]0[/dim]"
            info = self.iv_pct.get(u)
            if info and info.get("ready") and info.get("percentile") is not None:
                p = info["percentile"]
                col = "red" if p >= 70 else "green" if p <= 30 else "white"
                iv_s = f"[{col}]{p}%[/{col}]"
            else:
                iv_s = "[dim]—[/dim]"
            list_t.add_row(f"[bold white]{u}[/bold white]", src, nb, iv_s)

    # ── Background worker ──────────────────────────────────────────────────────

    @work(thread=True)
    def run_background_fetch(self) -> None:
        """背景抓取：先取無風險利率（^IRX），再對尚未有今日快照的標的抓期權鏈。
        期權鏈抓取沿用共用的 _fetch_and_cache_options_underlyings()。"""
        from .storage import options_symbol_fresh
        from .quotes import fetch_risk_free_rate

        self.r = fetch_risk_free_rate(default=self.r)
        self._refresh_underlying_spots()

        stale = [u for u in self.underlyings if not options_symbol_fresh(u)]
        if not stale:
            self.app.call_from_thread(self._on_fetch_complete, "[green]✅ 快取皆為今日最新[/green]")
            return

        self.app.call_from_thread(self._set_header, f"⏳ 正在背景更新 {len(stale)} 檔標的的期權資料...")
        _fetch_and_cache_options_underlyings(stale)
        self.app.call_from_thread(self._on_fetch_complete, "[green]✅ 期權資料載入完成[/green]")

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

    def _on_fetch_complete(self, _msg: str = "") -> None:
        self._run_analysis()

    # ── Analysis + render ────────────────────────────────────────────────────

    def _run_analysis(self) -> None:
        snapshots_by_underlying = {u: load_options_daily_snapshots(u) for u in self.underlyings}
        # bug#00067: compute both signal sets and render them via the SAME combined
        # generator the Dashboard card uses, so the two views stay aligned.
        flow_report = compute_options_flow(snapshots_by_underlying, window_days=OPTIONS_FLOW_WINDOW_DAYS)
        self.report = compute_iv_divergence(
            snapshots_by_underlying, r=self.r, window_days=OPTIONS_FLOW_WINDOW_DAYS
        )
        report = self.report
        # bug#00068 #1: IV 位階（每檔標的相對自身歷史的 IV 百分位）
        self.iv_pct = {u: compute_iv_percentile(snapshots_by_underlying[u]) for u in self.underlyings}
        # bug#00073: 預期波動區間（由最新快照的價平跨式估算）
        self.expected_move = {
            u: compute_expected_move(snapshots_by_underlying[u][-1] if snapshots_by_underlying[u] else None)
            for u in self.underlyings
        }
        self._render_portfolio()  # 各標的期權分析（預期波動 + 選擇權淨 Greeks）

        # bug#00099 / bug#00119: 分析結論卡 —— 三層寫作格式（結論／判斷依據／可點選公式細節）。
        # 每檔用自己的獨立 walk-forward 回測（generate_options_recommendations 內對每檔各跑一次
        # backtest_verdicts({u: snaps})）。頁面為完整檢視，含觀望標的。與 Dashboard 卡片共用同一
        # 份生成函式，只差 include_neutral 與每檔事件數。
        verdict_report = compute_directional_verdicts(
            snapshots_by_underlying, r=self.r, window_days=OPTIONS_FLOW_WINDOW_DAYS
        )
        recs = generate_options_recommendations(
            verdict_report, flow_report, report, snapshots_by_underlying,
            r=self.r, window_days=OPTIONS_FLOW_WINDOW_DAYS, positions=self.positions,
            iv_pct_by_underlying=self.iv_pct, include_neutral=True,
            top_events_per_underlying=4,
        )
        w = self.query_one("#ow-verdicts", Static)
        w.border_title = "📋 分析結論卡（綜合方向判斷 · 點『🔍 查看公式細節』看公式/回測/事件）"
        if recs:
            footer = ("[dim]方向結論＝未平倉建倉 skew＋排除股價變動的殘差偏向，兩訊號矛盾時顯示觀望；"
                      "每檔命中率來自該標的自己的 walk-forward 回測（按 [bold]k[/bold] 看 1/5/10 日完整校準）。"
                      "僅供參考，非投資建議。[/dim]")
            body, mapping = render_detail_recs(recs)
            body = body + "\n\n" + footer
            self._recs_by_id = mapping
        elif report["ready_count"] == 0:
            body = "[dim]資料收集中：需累積 ≥2 天真實快照才能產生方向結論（下方合約明細於首次抓取後即時顯示）。[/dim]"
            self._recs_by_id = {}
        else:
            body = "[dim]近期無方向訊號，亦無「超出股價變動」的異常震盪/背離或未平倉建倉事件。[/dim]"
            self._recs_by_id = {}
        w.update(body)

        self._set_header(
            f"清單 {len(self.underlyings)} 檔　資料進度 {report['ready_count']}/{report['total_count']}　"
            f"視窗 {report['window_days']} 天　無風險利率 {self.r * 100:.2f}%　更新 {report['as_of']}"
        )

        self._render_list()
        if self.underlyings:
            if self.selected_underlying not in self.underlyings:
                self.selected_underlying = self.underlyings[0]
            self._render_greeks(self.selected_underlying)
        else:
            self._render_greeks(None)

    def _render_greeks(self, underlying: Optional[str]) -> None:
        """bug#00077: 買權/賣權拆成左右兩欄分別顯示。"""
        ct = self.query_one("#ow-calls-table", DataTable)
        pt = self.query_one("#ow-puts-table", DataTable)
        ct.clear(columns=False)
        pt.clear(columns=False)
        _empty = ("", "", "", "", "", "", "", "")

        if not underlying:
            ct.add_row("[dim]清單為空，按 a 新增標的[/dim]", *_empty)
            pt.add_row("[dim]清單為空，按 a 新增標的[/dim]", *_empty)
            return

        view = build_contract_view(underlying, load_options_daily_snapshots(underlying), r=self.r)
        rows = view["rows"]
        if not rows:
            ct.add_row("[dim]資料收集中，尚無今日真實期權快照[/dim]", *_empty)
            pt.add_row("[dim]資料收集中，尚無今日真實期權快照[/dim]", *_empty)
            return

        def _fmt(row: dict) -> tuple:
            strike_s = f"[bold white]${row['strike']:g}[/bold white] {row['expiry']}"
            mny = row["moneyness"] or "—"
            mny_s = f"[green]{mny}[/green]" if mny == "ITM" else f"[dim]{mny}[/dim]"
            dte_s = str(row["dte"]) if row["dte"] is not None else "—"
            oi_s = f"{int(row['open_interest']):,}" if row["open_interest"] is not None else "—"
            iv_s = f"{row['iv'] * 100:.0f}%" if row["iv"] is not None else "—"
            d_s = f"{row['delta']:.2f}" if row["delta"] is not None else "—"
            g_s = f"{row['gamma']:.3f}" if row["gamma"] is not None else "—"
            th_s = f"{row['theta']:.3f}" if row["theta"] is not None else "—"
            be_s = f"${row['break_even']:g}" if row["break_even"] is not None else "—"
            return (strike_s, mny_s, dte_s, oi_s, iv_s, d_s, g_s, th_s, be_s)

        calls = [r for r in rows if r["type"] == "call"]
        puts  = [r for r in rows if r["type"] == "put"]

        for r in calls:
            ct.add_row(*_fmt(r))
        if not calls:
            ct.add_row("[dim]無買權資料[/dim]", *_empty)

        for r in puts:
            pt.add_row(*_fmt(r))
        if not puts:
            pt.add_row("[dim]無賣權資料[/dim]", *_empty)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "ow-list-table" and 0 <= event.cursor_row < len(self.underlyings):
            self.selected_underlying = self.underlyings[event.cursor_row]
            self._render_greeks(self.selected_underlying)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.on_data_table_row_highlighted(event)

    # ── Add / remove tickers ───────────────────────────────────────────────────

    def action_add_ticker(self) -> None:
        self.app.push_screen(AddTickerModal(), self._handle_add_ticker)

    def _handle_add_ticker(self, ticker: Optional[str]) -> None:
        if not ticker:
            return
        ticker = ticker.strip().upper()
        if ticker in self.underlyings:
            self.app.notify(f"⚠️ {ticker} 已在清單中")
            return
        from .storage import load_options_watchlist, save_options_watchlist
        save_options_watchlist(self.user, load_options_watchlist(self.user) + [ticker])
        self.underlyings, self.pos_set, self.extra_set = _watchlist_underlyings(self.user, self.positions)
        self.selected_underlying = ticker
        prune_options_history(ticker)
        self.app.notify(f"✅ 已加入 {ticker}，背景抓取期權資料中...")
        # 立即重跑分析：上方「各標的期權分析」表會馬上多一列（新標的先顯示「資料收集中」，
        # 待背景抓完再自動補上預期波動/IV/Greeks）(bug#00073)
        self._run_analysis()
        self.run_background_fetch()

    def action_remove_ticker(self) -> None:
        removable = sorted(self.extra_set)
        if not removable:
            self.app.notify("⚠️ 無可移除標的（持倉自動帶入的標的不可移除）", severity="warning")
            return
        self.app.push_screen(RemoveTickerModal(removable), self._handle_remove_ticker)

    def _handle_remove_ticker(self, ticker: Optional[str]) -> None:
        if not ticker:
            return
        from .storage import load_options_watchlist, save_options_watchlist
        save_options_watchlist(self.user, [t for t in load_options_watchlist(self.user) if t != ticker])
        self.underlyings, self.pos_set, self.extra_set = _watchlist_underlyings(self.user, self.positions)
        if self.selected_underlying == ticker:
            self.selected_underlying = self.underlyings[0] if self.underlyings else None
        self.app.notify(f"🗑️ 已移除 {ticker}")
        self._run_analysis()

    def action_go_back(self) -> None:
        self.dismiss()

    def action_calibration(self) -> None:
        """[k] 開啟訊號回測校準狀態畫面（bug#00070）。"""
        self.app.push_screen(CalibrationScreen(self.user, self.underlyings))

    def action_help(self) -> None:
        """[h] 開啟本頁各項數值的詳細說明頁（bug#00076）。"""
        self.app.push_screen(OptionsHelpScreen())

    def action_clear_cache(self) -> None:
        """bug#00116：重抓「今日」快照——只移除目前清單各標的今天那一筆，保留所有
        歷史累積後重新抓取（不再刪除整個歷史，避免把 flow/背離/IV 位階/回測所依賴的
        多日累積一次歸零）。移除今天的快照後 options_symbol_fresh() 轉 False，
        run_background_fetch() 便會重新抓當天最新資料再 append 回去。"""
        from .storage import remove_options_daily_snapshot, taiwan_now
        today = taiwan_now().strftime("%Y-%m-%d")
        for u in self.underlyings:
            remove_options_daily_snapshot(u, today)
        self._set_header("⏳ 正在重抓今日快照（保留歷史累積）...")
        self.run_background_fetch()


# ─────────────────────────────────────────────────────────────────────────────
# Calibration Status Screen (訊號回測校準狀態)
# ─────────────────────────────────────────────────────────────────────────────

class CalibrationScreen(Screen):
    """訊號回測校準狀態 —— walk-forward、純離線，讓使用者隨時知道訊號可信度(bug#00070)。"""

    BINDINGS = [
        Binding("escape", "go_back", "返回"),
        Binding("q",      "go_back", "返回", show=False),
    ]

    DEFAULT_CSS = """
    CalibrationScreen { background: #0d1117; layout: vertical; }
    #cal-body { height: 1fr; padding: 1 2; }
    #cal-static { height: auto; }
    """

    def __init__(self, user: str, underlyings: list[str]) -> None:
        super().__init__()
        self.user = user
        self.underlyings = underlyings

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="cal-body"):
            yield Static("", id="cal-static")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#cal-static", Static).update("[dim]⏳ 正在以累積的真實快照回測訊號...[/dim]")
        self.run_backtest()

    @work(thread=True)
    def run_backtest(self) -> None:
        from .calibration import backtest_verdicts
        snaps = {u: load_options_daily_snapshots(u) for u in self.underlyings}
        rep = backtest_verdicts(snaps, window_days=OPTIONS_FLOW_WINDOW_DAYS)
        self.app.call_from_thread(self._show_report, rep)

    def _show_report(self, rep: dict) -> None:
        from rich.panel import Panel as _Panel
        from .calibration import calibration_status_label

        def _pct(x):
            return f"{x * 100:.0f}%" if x is not None else "—"

        def _ret(x):
            if x is None:
                return "—"
            c = "green" if x >= 0 else "red"
            return f"[{c}]{'+' if x >= 0 else ''}{x * 100:.2f}%[/{c}]"

        status = calibration_status_label(rep)
        horizons_s = "/".join(str(h) for h in rep["horizons"])
        lines = [
            f"[bold]校準狀態：[/bold] {status}",
            "",
            f"資料累積：{rep['underlyings_with_data']} 檔標的、{rep['total_snapshot_days']} 筆快照日"
            + (f"、範圍 {rep['first_date']} ~ {rep['last_date']}" if rep['first_date'] else ""),
            f"方法：walk-forward，前瞻 {horizons_s} 日三組、訊號視窗 {rep['window_days']} 日，"
            f"校準對象＝分析結論卡的綜合方向結論（未平倉skew＋排除股價變動的殘差偏向）"
            f"——與畫面結論卡同一套邏輯（bug#00089）",
            "",
        ]

        any_eval = any(st["evaluated_signals"] for st in rep["by_horizon"].values())
        if not any_eval:
            lines.append("[dim]目前尚無可評估訊號 —— 系統剛開始或資料仍在累積。持續每日使用，")
            lines.append("待累積足夠真實快照(需要同一標的跨越前瞻期)後，這裡會自動出現各前瞻期的命中率與 edge。[/dim]")
        else:
            for h in rep["horizons"]:
                st = rep["by_horizon"][h]
                base = st["baseline_up_rate"]
                base_down = (1 - base) if base is not None else None
                bh, bn = st["bullish_hit_rate"], st["bullish_n"]
                eh, en = st["bearish_hit_rate"], st["bearish_n"]
                bull_edge = (bh - base) if (bh is not None and base is not None) else None
                bear_edge = (eh - base_down) if (eh is not None and base_down is not None) else None

                lines.append(
                    f"[bold cyan]── 前瞻 {h} 日 ──[/bold cyan]　"
                    f"基準上漲比例 {_pct(base)}（n={st['baseline_n']}，無條件）　"
                    f"可評估訊號 [bold]{st['evaluated_signals']}[/bold]（門檻 {rep['min_signals']}）"
                )
                lines.append(
                    f"　📈 看多結論 n={bn}：命中率 [bold]{_pct(bh)}[/bold]"
                    + (f"（vs 基準 {_pct(base)}，edge {bull_edge * 100:+.0f}pp）" if bull_edge is not None else "")
                    + f"　平均前瞻報酬 {_ret(st['bullish_mean_fwd'])}"
                )
                lines.append(
                    f"　📉 看空結論 n={en}：命中率 [bold]{_pct(eh)}[/bold]"
                    + (f"（vs 基準下跌 {_pct(base_down)}，edge {bear_edge * 100:+.0f}pp）" if bear_edge is not None else "")
                    + f"　平均前瞻報酬 {_ret(st['bearish_mean_fwd'])}"
                )
                if not st["ready"]:
                    lines.append(f"　[yellow]⚠️ 樣本數 {st['evaluated_signals']} < 門檻 {rep['min_signals']}，僅供參考。[/yellow]")
                lines.append("")

        lines += [
            "",
            "[dim]註：連續多日對同一標的的訊號高度自相關，命中率會略樂觀，需夠大樣本才穩健；",
            "前瞻以日曆日計、系統沒開的日子沒有快照；觀望（無方向）日不計入命中率、只計入基準。",
            "校準對象即結論卡的綜合方向結論本身。100% 使用真實累積資料，不回填不捏造。[/dim]",
        ]

        self.query_one("#cal-static", Static).update(
            _Panel("\n".join(lines), title="[bold cyan]📏 訊號回測校準狀態[/bold cyan]",
                   border_style="cyan", padding=(1, 2))
        )

    def action_go_back(self) -> None:
        self.dismiss()


# ─────────────────────────────────────────────────────────────────────────────
# Options Watchlist Help Screen (期權觀察清單 — 各項數值說明)
# ─────────────────────────────────────────────────────────────────────────────

_OPTIONS_HELP_TEXT = """[bold cyan]📖 期權觀察清單 — 各項數值說明[/bold cyan]

這頁把「期權鏈的即時狀態」與「你的選擇權部位風險」整合在一起。以下逐區塊解釋，
每項都附白話說明與例子。[dim]（本頁為資訊參考，非投資建議。）[/dim]

[bold yellow]── 一、左欄：觀察標的清單 ──[/bold yellow]
• [bold]標的[/bold]：你在觀察的股票代碼。來源 [cyan]📌持倉[/cyan]＝由你的持倉自動帶入、不可刪；[magenta]➕自訂[/magenta]＝你按 a 手動加入、可按 d 刪除。
• [bold]異常[/bold]：該標的近期「異常震盪/背離」訊號的筆數（見第三區）。數字越大代表越多合約出現不尋常變化。
• [bold]IV位階[/bold]：目前隱含波動率(IV)相對它自己過去的百分位。
   [dim]例：72% 代表現在 IV 比過去 72% 的日子還高 → 選擇權相對「貴」（紅字）；18% 代表相對「便宜」（綠字）。需累積 ≥8 天資料才顯示。[/dim]

[bold yellow]── 二、上方：各標的期權分析總表（版面第一區）──[/bold yellow]
• [bold]預期波動 ±1σ (~30 DTE)[/bold]：市場定價「到期之前大約會 ±多少」的 ±1 個標準差。取 DTE 最接近 30 天的到期日（避開只剩幾天、被財報/週選放大的短天期），以 [bold]現價 × ATM IV × √(DTE/365)[/bold] 計算（年化、無方向）。
   [dim]例：INTC ±$2.55 (±8.5%, 31d) → 市場認為到期前約 68% 機率落在 ±$2.55（約 ±8.5%）內。[/dim]
   同格下方另列 [bold]損益兩平 ±$[/bold]：價平跨式價（買權+賣權權利金），代表到期損益平衡區間的寬度——與 ±1σ 意義不同，不要混為一談。
   [dim]價格一律優先用買賣中間價 (bid+ask)/2；若無雙邊報價、價差過寬、或最後成交價過期，會退回並標 [yellow]⚠[/yellow]（低可信度），此時該格數字僅供粗略參考。[/dim]
• [bold]ATM IV[/bold]：價平合約的隱含波動率（市場對未來波動的年化預期）。40% 比 20% 代表市場預期波動更大、權利金更貴。跟預期波動同一組合約，因此也會帶 [yellow]⚠[/yellow] 低可信度標記。
• [bold]持倉Δ$ / Θ/日 / Vega[/bold]：[magenta]◆[/magenta] 代表你持有該標的選擇權，這三欄是你「該標的選擇權部位」的淨風險（只算選擇權、不含現股）：
   [dim]Δ$（Delta 美元）＝標的每動 1%，你這部位大約賺/賠多少；Θ/日＝每過一天因時間價值流失賺/賠多少（買方通常為負）；Vega＝IV 每升 1 個百分點賺/賠多少。末列為你所有持倉選擇權的合計。[/dim]

[bold yellow]── 三、其次：📋 分析結論卡（綜合方向判斷＋回測命中率＋重點異常事件，版面第二區）──[/bold yellow]
單一整合卡片：上半是每檔標的的投資結論，下半是支持判讀的合約層級異常事件（已排除當日股價變動）。
• [bold]方向結論[/bold]：🟢看多／🔴看空／⚪觀望。由兩條方向性訊號合成：「未平倉建倉 skew」（買權/賣權建倉占比 ≥70%/≤30%；OI 增減無法區分開/平倉，方向僅供參考）＋「排除股價變動後的淨殘差偏向」（買權殘差偏多、賣權殘差偏空）；兩訊號矛盾時誠實顯示觀望、不硬給方向。
   每則附 [bold]walk-forward 回測命中率[/bold]（與按 [bold]k[/bold] 的校準畫面同一套邏輯，1/5/10 日三組前瞻期取樣本最多的一組），樣本不足時明確標示「僅供參考」或「樣本累積中」——命中率驗證的就是結論卡本身這套判斷。並附與你部位方向[green]一致[/green]／[red]相反[/red]提示與標的 IV 位階，幫你判斷該不該行動。
• [bold]重點異常事件[/bold]（結論下方，合約層級證據）：
   [bold]🌀 異常震盪[/bold]：把權利金變動中「股價本身造成的部分」扣掉後，剩下的殘差仍很大 → 多半是波動率/事件驅動。
   [dim]例：股價只動 +2%（理論上權利金該 +$1.08），實際權利金卻 +$3.00，扣掉後殘差 +$1.92 → 有別的力量（IV 上升）在推動。[/dim]
   [bold]↔️ 背離[/bold]：股價與權利金方向相反。[dim]例：股價上漲，但買權權利金反而下跌（常見於 IV 壓縮）→ 方向不一致，留意。[/dim]
   [bold]🎯 建倉/價格波動[/bold]：單一合約的未平倉量大增減或權利金大幅漲跌。
• [bold]⚠️ 區間含財報[/bold]：若比較區間內有財報，IV/權利金劇變多屬「財報預期反應」（如財報後 IV 崩跌），會標註降權以免誤判。

[bold yellow]── 四、右欄：合約明細（左側買權 Call / 右側賣權 Put）──[/bold yellow]（同側依履約價由低到高排序）
• [bold]履約價[/bold]：合約的履約價格＋到期日（左欄全為買權、右欄全為賣權，不再混排）。
• [bold]價內外[/bold]：[green]ITM[/green] 價內（已有內含價值）／OTM 價外（純時間價值）。
   [dim]例：股價 $30 時，$28 買權為 ITM、$32 買權為 OTM。[/dim]
• [bold]DTE[/bold]：距到期天數（Days To Expiry）。越少，時間價值流失越快。
• [bold]未平倉[/bold]：該合約目前未平倉口數(OI)，越大代表越多人持有、流動性通常越好。
• [bold]IV[/bold]：該合約的隱含波動率。
• [bold]Δ Delta[/bold]：標的每動 $1，權利金約動多少（買權 0~1、賣權 −1~0）；也約略等於「到期價內的機率」。
   [dim]例：Delta 0.60 的買權，標的漲 $1，權利金約漲 $0.60，且大約 60% 機率到期價內。[/dim]
• [bold]Γ Gamma[/bold]：標的每動 $1，Delta 會變多少（Delta 的變化速度）。接近價平、接近到期時最大。
• [bold]Θ Theta[/bold]：每過一天，權利金因時間價值流失多少（通常為負，買方付、賣方收）。
   [dim]例：Theta −0.05 代表其他不變下，每天權利金約少 $0.05。[/dim]
• [bold]損益兩平[/bold]：到期時不賺不賠的標的價位。買權＝履約價+權利金；賣權＝履約價−權利金。
   [dim]例：$30 買權、權利金 $1.6 → 損益兩平 $31.6，標的到期需站上 $31.6 才開始獲利。[/dim]

[bold yellow]── 五、快速鍵 ──[/bold yellow]
[bold]a[/bold] 新增標的　[bold]d[/bold] 刪除自訂標的　[bold]k[/bold] 訊號回測校準狀態（看訊號歷史命中率是否可信）　[bold]h[/bold] 本說明　[bold]c[/bold] 重抓今日快照（只刷新今天、保留歷史累積）　[bold]Esc[/bold] 返回

[bold yellow]── 六、資料來源與限制（重要）──[/bold yellow]
所有數字 100% 來自每日真實抓取並累積的期權鏈快照，[bold]不回填、不捏造[/bold]；資料不足時會誠實顯示「資料收集中」。
限制：yfinance 的權利金常是過時成交價、OI 一天才更新一次、冷門履約價的 IV 可能不準——因此流動性差的標的，上述訊號可信度較低。這也是為什麼有「[bold]k 校準狀態[/bold]」讓你隨時檢視訊號到底準不準。

[dim]按 Esc 或 q 返回期權觀察清單。[/dim]
"""


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
        height: 4;
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

    #top-row {
        height: auto;
        padding: 0 1;
    }

    #broker-dist {
        width: 62;
        height: auto;
    }

    #metrics-row {
        width: 1fr;
        height: auto;
        margin-left: 1;
    }

    #holdings-label {
        height: 1;
        padding: 0 2;
        margin-top: 1;
    }

    #holdings-row {
        height: 1fr;
        padding: 0 1;
    }

    #holdings-scroll {
        width: 1fr;
        height: 100%;
        padding: 0 1;
        border: solid #21262d;
    }

    #recent-events-panel {
        width: 50;
        height: auto;
        margin-left: 1;
    }

    #cross-model-panel {
        height: auto;
        margin: 1 0;
        padding: 0 1;
        border: round #888888;
    }

    #sector-consensus-panel {
        height: auto;
        padding: 0 1;
    }

    #options-flow-panel {
        height: auto;
        padding: 0 1;
    }

    #etf-conclusions-panel {
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
            self.notify("✅ 已為您成功建立 AAPL (50股) 與 TSLA (10股) 預設範例部位！")
            self._start_dashboard(user, sample_positions, [])
        elif choice == "manual":
            self.push_screen(AddPositionModal(), lambda pos: self._handle_first_position(pos, user))
        else:
            self._start_dashboard(user, [], [])

    def _handle_first_position(self, result: Optional[list[Position]], user: str) -> None:
        if result:
            save_manual_positions(result, [], user=user)
            n = len(result)
            self.notify(f"✅ 已新增 {n} 筆持倉！" if n > 1 else "✅ 新增持倉成功！")
            self._start_dashboard(user, result, [])
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

        # bug#00096: 使用者成功登入後「立即」開始抓取分析資料（ETF/期權/類股快照），
        # 不必等第一個 30 分鐘週期；狀態列會持續顯示目前正在抓什麼。
        self._background_data_refresh()

        self.push_screen(DashboardScreen(user, positions, self._cash_positions, self._rate), self._handle_dashboard_exit)

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

            all_symbols = US_ACTIVE_TICKERS  # bug#00091：僅美股主動式 ETF
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
            if load_sector_groups(self._user) and sector_cache_needs_refresh(self._user):
                self._set_fetch_active('sector', '類股板塊成分股')
                _fetch_and_cache_sector_groups(self._user)
                self._clear_fetch_active('sector')

            # bug#00095 接線：抓完最新快照後，到期就產生需確認的校準提案（不自動套用）。
            self._maybe_recalibrate()
        except Exception:
            pass
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

    def _maybe_recalibrate(self) -> None:
        """bug#00095 接線：到期（每雙週/週）就用最新快照重跑回測、產生需確認的校準
        提案（不自動套用）；非到期近乎零成本（只比日期）。在背景 worker 執行緒呼叫。"""
        try:
            state = _run_calibration_cycle(self._user, force=False)
            _n = len((state.get('pending') or {}).get('changes', []))
            if _n:
                self.call_from_thread(
                    self.notify, f'⚙️ 有 {_n} 項投資建議校準待你確認（按 k 查看）')
        except Exception:
            pass

    def _handle_dashboard_exit(self, should_logout: bool) -> None:
        if should_logout:
            self.notify("🚪 已安全登出！")
            self.push_screen(LoginScreen(default_user=self.default_user), self._handle_login_complete)
        else:
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
