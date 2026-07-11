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
from .analysis import compute_symbol_trends, rank_symbol_trends, generate_etf_conclusions
from .options_analysis import compute_options_flow, generate_options_conclusions

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
    """5-panel metrics row as a Rich Table (Portfolio Value, PnL, Pos, Brokers, Beta)."""
    total_usd = 0.0
    total_cost_usd = 0.0
    has_cost = False
    broker_set: set[str] = set()
    has_quotes = any(p.market_price is not None or p.market_value is not None for p in positions)

    for p in positions:
        v = p.value if p.currency == "USD" else p.value / rate
        total_usd += v
        bk = f"{p.broker} ({p.account})" if p.account else p.broker
        broker_set.add(bk)
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
    for ratio in (3, 3, 2, 2, 2):
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

    # Panel 3 – Positions count
    p3 = Panel(
        f"[bold white]{len(positions)}[/bold white]\n[dim]Active Holdings[/dim]",
        title="📂 Positions",
        border_style="dim",
    )

    # Panel 4 – Brokers count
    p4 = Panel(
        f"[bold white]{len(broker_set)}[/bold white]\n[dim]Accounts Tracked[/dim]",
        title="🏦 Brokers",
        border_style="dim",
    )

    # Panel 5 – Portfolio Beta
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

    tbl.add_row(p1, p2, p3, p4, p5)
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


def _build_pnl_panel(positions: list[Position], rate: float) -> Panel:
    has_quotes = any(p.market_price is not None or p.market_value is not None for p in positions)
    if not has_quotes:
        return Panel("\n [yellow]⏳ 載入中...[/yellow]", title="📊 損益排行", border_style="yellow")
    ranked = []
    for p in positions:
        if p.unrealized_pnl is None:
            continue
        pnl_usd = p.unrealized_pnl if p.currency == "USD" else p.unrealized_pnl / rate
        ranked.append((p, p.unrealized_pnl, p.unrealized_pnl_pct, pnl_usd))
    ranked.sort(key=lambda x: x[3], reverse=True)

    lines = []
    if ranked:
        medals = ["🥇", "🥈", "🥉"]
        lines.append("[bold dim]▲ 最大獲利:[/bold dim]")
        for i, (p, pnl, pct, _) in enumerate(ranked[:3]):
            c   = "green" if pnl >= 0 else "red"
            s   = "+" if pnl >= 0 else ""
            med = medals[i] if i < 3 else "  "
            ccy = "" if p.currency == "USD" else f" {p.currency}"
            ps  = f"{s}{pct:.1f}%" if pct is not None else ""
            lines.append(
                f"{med} [bold white]{p.symbol[:12]:<12}[/bold white] "
                f"[{c}]{s}{pnl:,.0f}{ccy} ({ps})[/{c}]"
            )
        losers = [(p, pnl, pct, pu) for p, pnl, pct, pu in ranked if pu < 0]
        if losers:
            lines += ["", "[bold dim]▼ 最大虧損:[/bold dim]"]
            for p, pnl, pct, _ in losers[-2:]:
                ccy = "" if p.currency == "USD" else f" {p.currency}"
                ps  = f"{pct:.1f}%" if pct is not None else ""
                lines.append(
                    f"🔴 [bold white]{p.symbol[:12]:<12}[/bold white] "
                    f"[red]{pnl:,.0f}{ccy} ({ps})[/red]"
                )
    else:
        lines.append("[dim]無損益資料（請填寫平均成本）[/dim]")
    return Panel("\n".join(lines), title="📊 損益排行", border_style="yellow")


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


def _render_monthly_calendar(year: int, month: int, month_events: list, today) -> Table:
    import calendar
    day_to_events = {}
    for d, label in month_events:
        ev_type = _get_event_type(label)
        day_to_events.setdefault(d.day, []).append((label, ev_type))
        
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
                    if "PORTFOLIO_SOX" in types or ("PORTFOLIO" in types and "SOX" in types):
                        color = "green"
                    elif "PORTFOLIO" in types:
                        color = "green"
                    elif "MACRO" in types:
                        color = "cyan"
                    else:
                        color = "yellow"
                    week_str.append(f"[{color} reverse]{day:2d}[/{color} reverse]")
                else:
                    week_str.append(f"{day:2d}")
        grid_lines.append(" ".join(week_str))
        
    grid_content = "\n".join(grid_lines)
    
    event_lines = []
    for d, label in sorted(month_events, key=lambda x: x[0]):
        ev_type = _get_event_type(label)
        color = "cyan"
        if ev_type == "PORTFOLIO":
            color = "green"
        elif ev_type == "SOX":
            color = "yellow"
        elif ev_type == "PORTFOLIO_SOX":
            color = "green"
        days_away = (d - today).days
        if days_away == 0:
            days_str = "今天"
        elif days_away > 0:
            days_str = f"{days_away}天後"
        else:
            days_str = f"{-days_away}天前"
        event_lines.append(f"[{color}]• {d.strftime('%m-%d')} ({days_str:^4})[/{color}] │ {label}")
    events_content = "\n".join(event_lines) if event_lines else "[dim]無重要事件[/dim]"
    
    month_name = datetime(year, month, 1).strftime("%Y-%m (%B)")
    tbl = Table(title=f"\n[bold magenta]📅 {month_name}[/bold magenta]", show_header=False, box=None, padding=(0, 1), expand=True)
    tbl.title_align = "left"
    tbl.add_column("Grid", width=24)
    tbl.add_column("Events")
    
    tbl.add_row(
        Panel(grid_content, border_style="dim", title="月曆圖", expand=False),
        Panel(events_content, border_style="dim", title="事件清單", expand=True)
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


class AddPositionModal(ModalScreen[Optional[Position]]):
    """手動新增/修改持股對話框 — 支援上下鍵欄位導航與必填標注，並在選擇選擇權時動態顯示特定欄位。"""

    # Ordered list of all focusable field IDs (Inputs + Selects)
    _FIELD_IDS: list[str] = [
        "add-broker", "add-account", "add-symbol", "add-type",
        "add-underlying", "add-strike", "add-expiry", "add-option-type", "add-multiplier",
        "add-side", "add-qty", "add-cost", "add-market", "add-exch",
        "add-curr", "add-notes", "add-sector",
    ]

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
    #option-fields-container {
        height: auto;
        layout: vertical;
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

        title = "✏️ [bold]修改持倉部位[/bold]" if p else "➕ [bold]手動新增持倉部位[/bold]"
        btn_label = "確認修改" if p else "確認新增"

        with Vertical(id="add-dialog"):
            yield Label(title, id="add-title")
            yield Label(
                "💡 [dim]↑↓ 切換欄位　Enter 移至下一欄　[red]★[/red] 必填　[dim]✦ 建議填寫[/dim]",
                id="add-hint"
            )

            with Horizontal(classes="form-row"):
                yield Label("券商 [dim](Broker)[/dim]:", classes="form-label")
                yield Select(brokers, value=b_val, id="add-broker")

            with Horizontal(classes="form-row"):
                yield Label("帳戶 [dim](Account)[/dim]:", classes="form-label")
                yield Input(value=acct_val, placeholder="例如 default 或子帳戶", id="add-account",
                            classes="form-input")

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
        if is_opt:
            self.query_one("#add-underlying", Input).focus()
        else:
            self.query_one("#add-symbol", Input).focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "add-type":
            is_opt = (event.value == "option")
            self.query_one("#option-fields-container").display = is_opt
            self.query_one("#symbol-field-row").display = not is_opt

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._submit()
        elif event.button.id == "cancel":
            self.dismiss(None)

    def on_key(self, event) -> None:
        key = event.key
        if key == "escape":
            self.dismiss(None)
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
                if isinstance(focused, TxSelect):
                    return
                event.prevent_default()

            step = -1 if key in ("up", "shift+tab") else 1
            next_idx = (current_idx + step) % len(visible_fids)
            next_fid = visible_fids[next_idx]
            try:
                self.query_one(f"#{next_fid}").focus()
            except Exception:
                pass

    def _submit(self) -> None:
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
            self.dismiss(pos)
        except Exception as e:
            error_lbl.update(f"❌ 資料驗證失敗: {e}")


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



class AdjustPositionsModal(ModalScreen[Optional[str]]):
    """Adjust Positions choices overlay modal in TUI."""
    DEFAULT_CSS = """
    AdjustPositionsModal {
        align: center middle;
    }
    #adjust-dialog {
        width: 44;
        height: auto;
        border: thick $primary;
        background: $panel;
        padding: 1 2;
    }
    #adjust-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #adjust-list {
        height: auto;
        margin-bottom: 1;
        border: solid $accent;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="adjust-dialog"):
            yield Label("[bold cyan]部位調整 (Adjust Positions)[/]", id="adjust-title")
            yield OptionList(
                Option("➕ 新增部位 (Add Position)", id="add"),
                Option("✏️ 更改部位 (Edit Position)", id="edit"),
                Option("🗑️ 刪除部位 (Delete Position)", id="delete"),
                Option("💡 提示: 表格內可用上下左右選取格子 + Enter 編輯", id="tip"),
                Option("❌ 返回", id="cancel"),
                id="adjust-list"
            )

    def on_mount(self) -> None:
        self.query_one("#adjust-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ChoosePositionModal(ModalScreen[Optional[Position]]):
    """Modal to choose an existing position for editing or deletion."""
    DEFAULT_CSS = """
    ChoosePositionModal {
        align: center middle;
    }
    #choose-dialog {
        width: 65;
        height: auto;
        border: thick $primary;
        background: $panel;
        padding: 1 2;
    }
    #choose-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #choose-list {
        height: 12;
        margin-bottom: 1;
        border: solid $accent;
    }
    """

    def __init__(self, positions: list[Position], action_type: str) -> None:
        super().__init__()
        self.positions = positions
        self.action_type = action_type # "edit" or "delete"

    def compose(self) -> ComposeResult:
        action_label = "✏️ 選擇要修改的部位" if self.action_type == "edit" else "🗑️ 選擇要刪除的部位"
        options = []
        for i, p in enumerate(self.positions):
            desc = f"{p.broker.upper()} - {p.account or 'default'} - {p.symbol} ({p.instrument_type}, {p.quantity:,.2f}股)"
            options.append(Option(desc, id=str(i)))
        options.append(Option("❌ 取消 (Cancel)", id="cancel"))

        with Vertical(id="choose-dialog"):
            yield Label(f"[bold cyan]{action_label}[/]", id="choose-title")
            yield OptionList(*options, id="choose-list")

    def on_mount(self) -> None:
        self.query_one("#choose-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        val = event.option.id
        if val == "cancel":
            self.dismiss(None)
        else:
            idx = int(val)
            self.dismiss(self.positions[idx])

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class DeleteConfirmModal(ModalScreen[bool]):
    """Confirmation dialog for deleting a position."""
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

    def __init__(self, position: Position) -> None:
        super().__init__()
        self.position = position

    def compose(self) -> ComposeResult:
        desc = f"{self.position.broker.upper()} - {self.position.account or 'default'} - {self.position.symbol} ({self.position.instrument_type})"
        with Vertical(id="delete-confirm-dialog"):
            yield Label("⚠️ 刪除確認 (Confirm Deletion)", id="delete-confirm-title")
            yield Label(
                f"您確定要[bold red]完整刪除[/bold red]以下部位嗎？此操作無法復原：\n\n[cyan]{desc}[/]",
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
# Upcoming Events Screen
# ─────────────────────────────────────────────────────────────────────────────

class UpcomingEventsScreen(Screen):
    """重要日曆事件 Screen (持倉財報、SOX 十大財報、總經重大事件)。"""

    BINDINGS = [
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
        height: 35%;
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
        height: 55%;
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
    """

    def __init__(self, user: str, positions: list[Position], rate: float, cached_events: list[tuple] = None, events_fetched: bool = False) -> None:
        super().__init__()
        self.user = user
        self.positions = positions
        self.rate = rate
        self.cached_events = cached_events or []
        self.events_fetched = events_fetched

    def compose(self) -> ComposeResult:
        yield Static("", id="events-header")
        with Vertical(id="events-holdings-container"):
            yield Static("[bold dim] Holdings (持有部位)[/bold dim]", id="events-holdings-label")
            yield DataTable(id="events-holdings-table")
        with Vertical(id="events-calendar-container"):
            yield Static("[bold dim] Events Calendar (重大事件日曆)[/bold dim]", id="events-calendar-label")
            with ScrollableContainer(id="events-right-panel"):
                yield Static("", id="events-static")
        yield Footer()

    def _update_header(self, status: str) -> None:
        from rich.panel import Panel
        self.query_one("#events-header", Static).update(
            Panel(
                f"[bold cyan]📅 近期重大事件[/bold cyan]  "
                f"[dim]│[/dim]  "
                f"{status}",
                border_style="cyan",
                padding=(0, 1),
            )
        )

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
        if self.events_fetched:
            self._on_fetch_complete(self.cached_events, datetime.utcnow().date())
        else:
            self.run_calendar_fetch()

    def action_go_back(self) -> None:
        self.dismiss()

    @work(thread=True)
    def run_calendar_fetch(self) -> None:
        from datetime import datetime as dt_cls, timedelta
        import concurrent.futures
        import yfinance as yf
        from .quotes import _normalize_symbol_for_yf
        from .shared import get_upcoming_macro_events

        # 1. Gather unique symbols
        portfolio_tickers = set()
        for p in self.positions:
            sym = p.underlying if p.instrument_type == "option" else p.symbol
            norm_sym = _normalize_symbol_for_yf(sym, "stock", p.currency)
            portfolio_tickers.add(norm_sym)

        unique_tickers = list(portfolio_tickers.union(SOX_TICKERS))

        ticker_to_data = fetch_earnings_calendar(unique_tickers)

        today = datetime.utcnow().date()
        start_date = today  # 只顯示今天(含)以後的事件，過去事件不再列出
        cutoff = today + timedelta(days=90)

        events = []

        # Add earnings dates
        for sym, (dates_list, info_date, time_str, period_str) in ticker_to_data.items():
            is_user = any(
                _normalize_symbol_for_yf(p.underlying if p.instrument_type == "option" else p.symbol, "stock", p.currency) == sym
                for p in self.positions
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

        # Add macro events
        macro_list = get_upcoming_macro_events(days=90, start_days_ago=0)
        for ev_date, ev_label, time_str in macro_list:
            if start_date <= ev_date <= cutoff:
                from .shared import MACRO_EVENT_NAMES
                event_name = MACRO_EVENT_NAMES.get(ev_label, ev_label)
                events.append((ev_date, f"{event_name} ({time_str})"))

        # Update UI back on the event loop
        self.app.call_from_thread(self._on_fetch_complete, events, today)

    def _on_fetch_complete(self, events: list[tuple], today) -> None:
        from rich.console import Group
        from rich.panel import Panel

        self._update_header("[green]✅ 行事曆資料更新成功！[/green]")
        
        if not events:
            self.query_one("#events-static", Static).update(
                Panel("[dim]近期 90 天內無重大事件與財報日期[/dim]", title="📅 行情日曆", border_style="dim")
            )
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

        self.query_one("#events-static", Static).update(Group(*month_views))


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard Screen
# ─────────────────────────────────────────────────────────────────────────────

class DashboardScreen(Screen):
    """AssetTrack 主看板畫面。支援鍵盤快速鍵與 Holdings 捲動。"""

    BINDINGS = [
        Binding("1",   "adjust_positions",     "部位調整"),
        Binding("2",   "refresh_now",          "立即重整"),
        Binding("3",   "logout",               "安全登出"),
        Binding("4",   "upcoming_events",      "近期重大事件"),
        Binding("5",   "save_snapshot",        "儲存快照"),
        Binding("6",   "active_etfs",          "主動式 ETF 排行"),
        Binding("7",   "options_watchlist",    "期權觀察清單"),
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
        self._upcoming_events: list[tuple] = []
        self._events_fetched: bool = False
        self._fetching_events: bool = False

    # ── Layout ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("", id="tui-header")
        with Horizontal(id="main-layout"):
            with Vertical(id="sidebar"):
                yield Static("[bold cyan] ✨ AssetTrack[/bold cyan]", id="sidebar-logo")
                yield OptionList(
                    Option("📝 部位調整", id="adjust"),
                    Option("🔄 立即重整", id="refresh"),
                    Option("📅 近期重大事件", id="upcoming_events"),
                    Option("📈 主動式 ETF 排行", id="active_etfs"),
                    Option("🎯 期權觀察清單", id="options_watchlist"),
                    Option("📸 儲存快照", id="snapshot"),
                    Option("🚪 安全登出", id="logout"),
                    id="sidebar-nav"
                )
                yield Static("", id="sidebar-footer")
            with Vertical(id="content-area"):
                yield Static("", id="metrics-row")
                yield Static("[bold dim] Holdings[/bold dim]", id="holdings-label")
                with ScrollableContainer(id="holdings-scroll"):
                    yield DataTable(id="holdings-table")
                with Horizontal(id="side-panels"):
                    yield Static("", id="broker-dist")
                    yield Static("", id="pnl-leaderboard")
                    yield Static("", id="recent-events-panel")
                with Horizontal(id="strategy-panels"):
                    yield Static("", id="etf-conclusions-panel")
                    yield Static("", id="options-flow-panel")
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
        self.query_one("#sidebar-nav").focus()

    def on_key(self, event) -> None:
        if event.key == "right":
            if self.focused == self.query_one("#sidebar-nav"):
                table = self.query_one("#holdings-table")
                if len(self.row_data) > 0:
                    table.focus()
                    event.prevent_default()
        elif event.key == "left":
            if self.focused == self.query_one("#holdings-table"):
                table = self.query_one("#holdings-table", DataTable)
                if table.cursor_coordinate.column == 0:
                    self.query_one("#sidebar-nav").focus()
                    event.prevent_default()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        action = event.option.id
        if action == "adjust":
            self.action_adjust_positions()
        elif action == "refresh":
            self.action_refresh_now()
        elif action == "snapshot":
            self.action_save_snapshot()
        elif action == "upcoming_events":
            self.action_upcoming_events()
        elif action == "active_etfs":
            self.action_active_etfs()
        elif action == "options_watchlist":
            self.action_options_watchlist()
        elif action == "logout":
            self.action_logout()

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

    # ── Full render ───────────────────────────────────────────────────────────

    def _render_all(self) -> None:
        """Render all dashboard widgets from current in-memory data."""
        self._render_sidebar()

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
                "[yellow]⚠️ 尚無任何持倉。請選擇 [bold]部位調整[/bold] 新增持倉。[/yellow]",
                "", "", "", "", "", "", "", "", "", ""
            )
            self.row_data = [None]
            self.query_one("#broker-dist",     Static).update("")
            self.query_one("#pnl-leaderboard", Static).update("")
            self.query_one("#recent-events-panel", Static).update(
                self._build_recent_events_panel()
            )
            self.query_one("#etf-conclusions-panel", Static).update(
                self._build_etf_conclusions_panel()
            )
            self.query_one("#options-flow-panel", Static).update(
                self._build_options_flow_panel()
            )

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
                self.row_data.append(p)

        self.query_one("#broker-dist", Static).update(
            _build_broker_panel(self._positions, self._rate)
        )
        self.query_one("#pnl-leaderboard", Static).update(
            _build_pnl_panel(self._positions, self._rate)
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

        # Restore coordinate and focus state
        if len(self.row_data) > 0:
            old_row, old_col = old_coordinate
            new_row = min(old_row, len(self.row_data) - 1)
            new_col = min(old_col, 10)
            table.cursor_coordinate = (max(0, new_row), max(0, new_col))
        if had_focus:
            table.focus()

    def _render_sidebar(self) -> None:
        pos_count = len(self._positions)
        self.query_one("#sidebar-footer", Static).update(
            f"\n [dim]────────────────[/dim]\n"
            f" [bold cyan]📂 {pos_count} 個持倉[/bold cyan]"
        )

    # ── Background refresh worker (thread) ───────────────────────────────────

    @work(thread=True)
    def _do_refresh_worker(self, load_from_disk: bool = True) -> None:
        """Background thread: fetch rate + positions + live quotes."""
        if self._loading:
            return  # skip if already refreshing
        self._loading = True
        try:
            self._rate      = _get_cached_usdtwd_rate()
            if load_from_disk:
                self._positions, self._cash_positions = load_manual_positions(user=self._user)
            if self._positions:
                self._positions = enrich_positions_with_quotes(self._positions)
        except Exception:
            pass
        finally:
            self._loading = False
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

    def _build_etf_conclusions_panel(self) -> Panel:
        """bug#00061: 首頁「交易策略建議」卡片之一 —— 主動式ETF跨基金持股趨勢結論。
        100% 離線本機運算（讀取 etf_cache/history/*.jsonl 真實累積快照），無網路請求；
        與「主動式ETF排行」頁面的進階分析畫面共用同一份 generate_etf_conclusions()
        輸出，兩處文字保證一致。資料不足時誠實顯示收集進度，不生成假結論。
        """
        from rich.panel import Panel

        all_symbols = US_ACTIVE_TICKERS + TWD_ACTIVE_TICKERS
        snapshots_by_etf = {sym: load_etf_daily_snapshots(sym) for sym in all_symbols}
        report = compute_symbol_trends(snapshots_by_etf, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS)
        bullets = generate_etf_conclusions(report, top_n=2, positions=self._positions)

        if not bullets:
            body = (
                f"[dim]資料收集中：{report['etfs_ready_count']}/{report['etfs_total_count']} "
                f"檔 ETF 已有足夠真實快照\n尚無法產生趨勢結論，持續使用系統會逐日累積資料\n"
                f"按 [bold]6[/bold] 查看主動式ETF排行[/dim]"
            )
        else:
            body = "\n".join(bullets) + "\n\n[dim]按 [bold]6[/bold] 查看完整報告[/dim]"

        return Panel(body, title="📊 ETF趨勢結論", border_style="magenta")

    def _build_options_flow_panel(self) -> Panel:
        """bug#00061: 首頁「交易策略建議」卡片之二 —— 依使用者實際持倉標的的期權
        建倉/價格波動結論。100% 離線本機運算，與「期權觀察清單」頁面共用同一份
        generate_options_conclusions() 輸出。資料不足時誠實顯示收集進度。
        """
        from rich.panel import Panel

        underlyings = _underlyings_from_positions(self._positions)
        if not underlyings:
            return Panel(
                "[dim]尚無持倉，無法建立期權觀察清單[/dim]",
                title="🎯 期權觀察結論", border_style="magenta",
            )

        snapshots_by_underlying = {u: load_options_daily_snapshots(u) for u in underlyings}
        report = compute_options_flow(snapshots_by_underlying, window_days=OPTIONS_FLOW_WINDOW_DAYS)
        bullets = generate_options_conclusions(report, top_n=2, positions=self._positions)

        if not bullets:
            body = (
                f"[dim]資料收集中：{report['ready_count']}/{report['total_count']} "
                f"檔標的已有足夠真實快照\n尚無法產生訊號，持續使用系統會逐日累積資料\n"
                f"按 [bold]7[/bold] 查看期權觀察清單[/dim]"
            )
        else:
            body = "\n".join(bullets) + "\n\n[dim]按 [bold]7[/bold] 查看完整清單[/dim]"

        return Panel(body, title="🎯 期權觀察結論", border_style="magenta")

    # ── Action handlers ───────────────────────────────────────────────────────

    def action_adjust_positions(self) -> None:
        modal = AdjustPositionsModal()
        self.app.push_screen(modal, self._handle_adjust_choice)

    def _handle_adjust_choice(self, choice: Optional[str]) -> None:
        if choice == "add":
            self.app.push_screen(AddPositionModal(), self._handle_add_position_result)
        elif choice == "edit":
            positions, _ = load_manual_positions(self._user)
            if not positions:
                self.app.notify("⚠️ 目前沒有任何持倉部位可以修改！", severity="warning")
                return
            self.app.push_screen(ChoosePositionModal(positions, "edit"), self._handle_edit_choice_result)
        elif choice == "delete":
            positions, _ = load_manual_positions(self._user)
            if not positions:
                self.app.notify("⚠️ 目前沒有任何持倉部位可以刪除！", severity="warning")
                return
            self.app.push_screen(ChoosePositionModal(positions, "delete"), self._handle_delete_choice_result)

    def _handle_edit_choice_result(self, pos: Optional[Position]) -> None:
        if pos:
            self.app.push_screen(
                AddPositionModal(pos),
                lambda updated_pos: self._handle_edit_position_result(pos, updated_pos)
            )

    def _handle_edit_position_result(self, old_pos: Position, updated_pos: Optional[Position]) -> None:
        if updated_pos:
            positions, cash_positions = load_manual_positions(self._user)
            for idx, p in enumerate(positions):
                if (p.broker.lower() == old_pos.broker.lower() and 
                    (p.account or "").lower() == (old_pos.account or "").lower() and 
                    p.symbol.upper() == old_pos.symbol.upper() and
                    p.instrument_type == old_pos.instrument_type):
                    positions[idx] = updated_pos
                    break
            save_manual_positions(positions, cash_positions=cash_positions, user=self._user)
            self.app.notify("✅ 修改持倉成功！")
            self._positions = positions
            self._do_refresh_worker()

    def _handle_delete_choice_result(self, pos: Optional[Position]) -> None:
        if pos:
            self.app.push_screen(
                DeleteConfirmModal(pos),
                lambda confirmed: self._handle_delete_confirm_result(pos, confirmed)
            )

    def _handle_delete_confirm_result(self, pos: Position, confirmed: bool | None) -> None:
        if confirmed:
            positions, cash_positions = load_manual_positions(self._user)
            new_positions = []
            for p in positions:
                if (p.broker.lower() == pos.broker.lower() and 
                    (p.account or "").lower() == (pos.account or "").lower() and 
                    p.symbol.upper() == pos.symbol.upper() and
                    p.instrument_type == pos.instrument_type):
                    continue
                new_positions.append(p)
            save_manual_positions(new_positions, cash_positions=cash_positions, user=self._user)
            self.app.notify("🗑️ 部位已刪除成功！")
            self._positions = new_positions
            self._do_refresh_worker()

    def _handle_add_position_result(self, pos: Optional[Position]) -> None:
        if pos:
            positions, cash_positions = load_manual_positions(self._user)
            matched = False
            for p in positions:
                if (p.broker.lower() == pos.broker.lower() and
                        (p.account or "").lower() == (pos.account or "").lower() and
                        p.symbol.upper() == pos.symbol.upper() and
                        p.instrument_type == pos.instrument_type):
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
                    matched = True
                    break
            if not matched:
                positions.append(pos)
            save_manual_positions(positions, cash_positions=cash_positions, user=self._user)
            self.app.notify("✅ 新增持倉成功！")
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
        self.app.push_screen(UpcomingEventsScreen(self._user, self._positions, self._rate, self._upcoming_events, self._events_fetched))

    def action_active_etfs(self) -> None:
        """[6] 主動式 ETF 排行：推入 ActiveETFsScreen，不 suspend。"""
        self.app.push_screen(ActiveETFsScreen(self._user, self._rate))

    def action_options_watchlist(self) -> None:
        """[7] 期權觀察清單：推入 OptionsWatchlistScreen，不 suspend。"""
        self.app.push_screen(OptionsWatchlistScreen(self._user, self._positions))


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


class ActiveETFsScreen(Screen):
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
        height: 1fr;
        layout: horizontal;
        margin: 1 2;
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
    #etf-us-table, #etf-twd-table {
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
    """

    def __init__(self, user: str, rate: float) -> None:
        super().__init__()
        self.user = user
        self.rate = rate
        self.etf_cache: dict[str, dict] = {}
        self.performance_data: dict = {}
        self.realtime_aums: dict[str, float] = {}
        self.us_symbols: list[str] = []
        self.twd_symbols: list[str] = []
        self.selected_symbol: str | None = None

    # ── Compose ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("", id="etf-header")
        with Horizontal(id="etf-body"):
            # Left half: ranking
            with Vertical(id="etf-left-col"):
                with TabbedContent(id="etf-left-tabbed"):
                    with TabPane("🇺🇸 美股主動型", id="tab-us-active"):
                        yield DataTable(id="etf-us-table")
                    with TabPane("🇹🇼 台股主動型", id="tab-twd-active"):
                        yield DataTable(id="etf-twd-table")
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

        twd_t = self.query_one("#etf-twd-table", DataTable)
        twd_t.cursor_type = "row"
        twd_t.add_columns("Symbol", "AUM", "YTD", "1Y", "最大持股")

        h_t = self.query_one("#etf-holdings-table", DataTable)
        h_t.cursor_type = "row"
        h_t.add_columns("Symbol", "名稱", "權重", "股數", "市值")

        tr_t = self.query_one("#etf-history-table", DataTable)
        tr_t.cursor_type = "row"
        tr_t.add_columns("日期", "操作", "Symbol", "股數", "價格", "權重△")

        self._set_header("⏳ 確認快取並載入資料...")
        self._set_mid_status("[dim]← 選取左欄 ETF 以查看持股[/dim]")
        self._set_right_status("[dim]← 選取左欄 ETF 以查看歷史[/dim]")
        us_t.focus()

        # Run 2-week cleanup in background (non-blocking)
        cleanup_old_etf_caches(max_age_days=14)

        # Load whatever is already cached for immediate display
        all_symbols = US_ACTIVE_TICKERS + TWD_ACTIVE_TICKERS

        # Trim each symbol's real daily-snapshot history log to the trailing
        # ~65 days (comfortably covers the 60-day 進階分析 trend window without
        # growing unbounded).
        for sym in all_symbols:
            prune_etf_history(sym, max_age_days=65)

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
                    )

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

    # ── Status helpers ─────────────────────────────────────────────────────────

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
                if focused.id in ("etf-us-table", "etf-twd-table"):
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
                    active_tab = self.query_one(TabbedContent).active
                    if active_tab == "tab-us-active":
                        self.query_one("#etf-us-table", DataTable).focus()
                    else:
                        self.query_one("#etf-twd-table", DataTable).focus()
                    event.prevent_default()
                    event.stop()
                elif focused.id == "etf-history-table":
                    self.query_one("#etf-holdings-table", DataTable).focus()
                    event.prevent_default()
                    event.stop()
        # Up arrow at the very top of lists to jump focus to the tab headers
        elif event.key == "up":
            focused = self.focused
            if isinstance(focused, DataTable) and focused.id in ("etf-us-table", "etf-twd-table"):
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
                    active = self.query_one(TabbedContent).active
                    if active == "tab-us-active":
                        self.query_one("#etf-us-table", DataTable).focus()
                    else:
                        self.query_one("#etf-twd-table", DataTable).focus()
                    event.prevent_default()
                    event.stop()
                except Exception:
                    pass

    # ── Background worker ──────────────────────────────────────────────────────

    @work(thread=True)
    def run_background_fetch(self) -> None:
        """Parallel fetch of AUM, performance, and holdings for all ETFs."""
        from concurrent.futures import ThreadPoolExecutor
        import yfinance as _yf
        from .storage import (
            load_etf_symbol_cache, save_etf_symbol_cache,
            etf_symbol_cache_fresh,
        )
        from .quotes import (
            fetch_active_etf_performance, fetch_etf_holdings,
            fetch_prices_batch, estimate_shares,
        )

        all_symbols = US_ACTIVE_TICKERS + TWD_ACTIVE_TICKERS

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

        # ── 2. Batch fetch performance for stale symbols ──────────────────────
        stale_perf = fetch_active_etf_performance(stale_symbols) if stale_symbols else {}

        # ── 3. Fetch AUM, Name, Holdings for each stale symbol ────────────────
        aums = dict(self.realtime_aums)
        perf = dict(self.performance_data)
        etf_cache = dict(self.etf_cache)

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
                append_etf_daily_snapshot(sym, cached["holdings"], cached.get("aum"))

            # No real trade-history source exists (yfinance does not expose it);
            # `history` stays whatever is already cached (usually empty), and the
            # detail panel shows an explicit "no data" status instead of fabricating one.

            # Save cache file
            save_etf_symbol_cache(sym, cached)
            etf_cache[sym] = cached

        self.app.call_from_thread(
            self._on_fetch_complete, aums, perf, etf_cache, perf_fail_count, len(stale_symbols)
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

        new_twd: list[str] = []
        self._render_one_tab("#etf-twd-table", TWD_ACTIVE_TICKERS, new_twd)
        self.twd_symbols = new_twd

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
            self._refresh_detail_panels(self.us_symbols[row_idx])
        elif table_id == "etf-twd-table" and 0 <= row_idx < len(self.twd_symbols):
            self._refresh_detail_panels(self.twd_symbols[row_idx])

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
        self.twd_symbols.clear()
        self.selected_symbol = None
        
        # Clear UI tables
        self.query_one("#etf-us-table", DataTable).clear(columns=False)
        self.query_one("#etf-twd-table", DataTable).clear(columns=False)
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
        self.app.push_screen(AdvancedAnalysisScreen())


# ─────────────────────────────────────────────────────────────────────────────
# Advanced Analysis Screen (進階分析)
# ─────────────────────────────────────────────────────────────────────────────
# bug#00060: 100% 離線運算 —— 只讀取 storage.py 已在背景刷新時逐日真實累積下來的
# per-ETF 快照（etf_cache/history/*.jsonl），不打任何網路請求，也不對缺資料的日子
# 做任何估計或回填。60 天視窗內若某檔 ETF 累積不足 2 筆真實快照，就不會被納入計算，
# 並誠實在畫面上顯示目前的資料收集進度。

ADVANCED_ANALYSIS_WINDOW_DAYS = 60


class AdvancedAnalysisScreen(Screen):
    """跨主動式ETF持股趨勢共識報告 —— 純本機離線運算，無網路請求。"""

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
        all_symbols = US_ACTIVE_TICKERS + TWD_ACTIVE_TICKERS
        snapshots_by_etf = {sym: load_etf_daily_snapshots(sym) for sym in all_symbols}

        report = compute_symbol_trends(snapshots_by_etf, window_days=ADVANCED_ANALYSIS_WINDOW_DAYS)
        ranked = rank_symbol_trends(report)

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

        # bug#00061: 結論區塊 — 多數性 + 規模性 bullets，與 Dashboard 首頁卡片共用同一份
        # generate_etf_conclusions() 輸出，確保兩處文字永遠一致。
        bullets = generate_etf_conclusions(report)
        if bullets:
            conclusion_body = "\n".join(f"• {b}" for b in bullets)
        else:
            conclusion_body = "[dim]目前尚無足夠真實資料可生成結論（需要更多 ETF 累積 ≥2 天快照）。[/dim]"
        self.query_one("#aa-conclusions", Static).update(
            _Panel(conclusion_body, title="[bold]📝 結論[/bold]", border_style="magenta", padding=(0, 1))
        )

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
# bug#00061: 依使用者實際持有的標的建立期權觀察清單，每日真實累積 28-60 天到期、
# 價平 ±15% 履約價合約的快照（quotes.fetch_options_snapshot），離線比對偵測大量
# 建倉（未平倉量變化）與大幅價格波動（options_analysis.py）。100% 離線運算，
# 資料不足時誠實顯示收集進度，絕不回填或捏造。

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


class OptionsWatchlistScreen(Screen):
    """期權建倉與價格波動觀察清單 —— 依真實持倉標的追蹤，離線運算。"""

    BINDINGS = [
        Binding("escape", "go_back", "返回看板"),
        Binding("c",      "clear_cache", "清除快取並重新載入"),
        Binding("q",      "go_back", "返回看板", show=False),
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
    #ow-conclusions {
        height: auto;
        padding: 0 1;
        margin: 1 2 0 2;
    }
    #ow-body {
        height: 1fr;
        layout: horizontal;
        margin: 1 2;
    }
    #ow-left-col {
        width: 40%;
        height: 1fr;
        border: tall #334155;
        margin-right: 1;
    }
    #ow-left-col:focus-within { border: tall $accent; }
    #ow-list-table { height: 1fr; border: none; }
    #ow-right-col {
        width: 60%;
        height: 1fr;
        border: tall #334155;
    }
    #ow-right-col:focus-within { border: tall $accent; }
    #ow-events-table { height: 1fr; border: none; }
    """

    def __init__(self, user: str, positions: list[Position]) -> None:
        super().__init__()
        self.user = user
        self.positions = positions
        self.underlyings = _underlyings_from_positions(positions)
        self.report: dict = {}
        self.selected_underlying: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield Static("", id="ow-header")
        yield Static("", id="ow-conclusions")
        with Horizontal(id="ow-body"):
            with Container(id="ow-left-col"):
                yield DataTable(id="ow-list-table")
            with Container(id="ow-right-col"):
                yield DataTable(id="ow-events-table")
        yield Footer()

    def on_mount(self) -> None:
        list_t = self.query_one("#ow-list-table", DataTable)
        list_t.cursor_type = "row"
        list_t.add_columns("標的", "訊號數", "偏向")

        ev_t = self.query_one("#ow-events-table", DataTable)
        ev_t.cursor_type = "row"
        ev_t.add_columns("合約", "類型", "未平倉變化", "價格變化", "期間")

        if not self.underlyings:
            self._set_header("[yellow]⚠️ 目前沒有任何持倉可供建立期權觀察清單[/yellow]")
            list_t.focus()
            return

        self._set_header(f"⏳ 確認快取並載入 {len(self.underlyings)} 檔標的資料...")
        list_t.focus()

        for u in self.underlyings:
            prune_options_history(u, max_age_days=65)

        self._run_analysis()
        self.run_background_fetch()

    def _set_header(self, status: str) -> None:
        from rich.panel import Panel as _Panel
        self.query_one("#ow-header", Static).update(
            _Panel(
                f"[bold cyan]🎯 期權觀察清單 — 建倉與價格波動偵測[/bold cyan]  [dim]│[/dim]  {status}",
                border_style="cyan", padding=(0, 1),
            )
        )

    # ── Background worker ──────────────────────────────────────────────────────

    @work(thread=True)
    def run_background_fetch(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        import time as _time
        from .quotes import fetch_options_snapshot
        from .storage import options_symbol_fresh, append_options_daily_snapshot

        stale = [u for u in self.underlyings if not options_symbol_fresh(u)]
        if not stale:
            self.app.call_from_thread(self._set_header, "[green]✅ 快取皆為今日最新，已直接載入[/green]")
            return

        self.app.call_from_thread(self._set_header, f"⏳ 正在背景更新 {len(stale)} 檔標的的期權資料...")

        def _fetch_one(u: str):
            _time.sleep(0.35)  # pacing to avoid Yahoo rate limiting, same pattern as ETF fetch
            try:
                return u, fetch_options_snapshot(u)
            except Exception:
                return u, {"spot_price": None, "contracts": []}

        with ThreadPoolExecutor(max_workers=2) as ex:
            for u, res in ex.map(_fetch_one, stale):
                if res.get("contracts"):
                    append_options_daily_snapshot(u, res["contracts"], res.get("spot_price"))

        self.app.call_from_thread(self._on_fetch_complete)

    def _on_fetch_complete(self) -> None:
        self._set_header("[green]✅ 期權資料載入完成[/green]")
        self._run_analysis()

    # ── Analysis + render ────────────────────────────────────────────────────

    def _run_analysis(self) -> None:
        snapshots_by_underlying = {u: load_options_daily_snapshots(u) for u in self.underlyings}
        self.report = compute_options_flow(snapshots_by_underlying, window_days=OPTIONS_FLOW_WINDOW_DAYS)
        report = self.report

        from rich.panel import Panel as _Panel
        bullets = generate_options_conclusions(report, positions=self.positions)
        if bullets:
            body = "\n".join(f"• {b}" for b in bullets)
        else:
            body = "[dim]目前尚無足夠真實資料可生成結論（需要更多標的累積 ≥2 天快照）。[/dim]"
        self.query_one("#ow-conclusions", Static).update(
            _Panel(body, title="[bold]📝 結論[/bold]", border_style="magenta", padding=(0, 1))
        )

        self._set_header(
            f"資料收集進度：{report['ready_count']}/{report['total_count']} 檔標的已有 ≥2 天真實快照 "
            f"({report['ready_pct']:.0f}%)　視窗 {report['window_days']} 天　更新於 {report['as_of']}"
        )

        list_t = self.query_one("#ow-list-table", DataTable)
        list_t.clear(columns=False)

        events_by_underlying: dict[str, list[dict]] = {}
        for e in report.get("events", []):
            events_by_underlying.setdefault(e["underlying"], []).append(e)

        for u in self.underlyings:
            evs = events_by_underlying.get(u, [])
            skew = report.get("underlying_skew", {}).get(u)
            if skew:
                if skew["call_pct"] >= 70:
                    bias = "[green]偏多[/green]"
                elif skew["call_pct"] <= 30:
                    bias = "[red]偏空[/red]"
                else:
                    bias = "[dim]中性[/dim]"
            else:
                bias = "[dim]—[/dim]"
            list_t.add_row(f"[bold white]{u}[/bold white]", str(len(evs)), bias)

        if self.underlyings:
            self.selected_underlying = self.underlyings[0]
            self._render_events(self.selected_underlying)

    def _render_events(self, underlying: str) -> None:
        table = self.query_one("#ow-events-table", DataTable)
        table.clear(columns=False)

        evs = [e for e in self.report.get("events", []) if e["underlying"] == underlying]
        if not evs:
            cov = self.report.get("coverage", {}).get(underlying, {})
            if not cov.get("ready"):
                table.add_row("[dim]資料收集中，尚不足 2 天真實快照[/dim]", "", "", "", "")
            else:
                table.add_row("[dim]此標的近期無明顯建倉或價格波動訊號[/dim]", "", "", "", "")
            return

        for e in evs:
            cp = "[green]買權[/green]" if e["type"] == "call" else "[red]賣權[/red]"
            oi_s = "—"
            if e["oi_delta"] is not None:
                sign = "+" if e["oi_delta"] >= 0 else ""
                pct_s = f" ({sign}{e['oi_pct']:.0f}%)" if e["oi_pct"] is not None else ""
                oi_s = f"{sign}{int(e['oi_delta']):,} 口{pct_s}"
            price_s = "—"
            if e["price_delta_pct"] is not None:
                sign = "+" if e["price_delta_pct"] >= 0 else ""
                price_s = f"{sign}{e['price_delta_pct']:.0f}%"
            table.add_row(
                f"[bold white]${e['strike']:g} {e['expiry']}[/bold white]",
                cp,
                oi_s,
                price_s,
                f"[dim]{e['first_date']}~{e['last_date']}[/dim]",
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "ow-list-table" and 0 <= event.cursor_row < len(self.underlyings):
            self.selected_underlying = self.underlyings[event.cursor_row]
            self._render_events(self.selected_underlying)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.on_data_table_row_highlighted(event)

    def action_go_back(self) -> None:
        self.dismiss()

    def action_clear_cache(self) -> None:
        """清除快取：刪除本觀察清單的期權歷史記錄並重新真實抓取。"""
        from .storage import get_options_history_dir
        cache_dir = get_options_history_dir()
        if cache_dir.exists():
            for f in cache_dir.glob("*.jsonl"):
                try:
                    f.unlink()
                except Exception:
                    pass
        self._set_header("⏳ 快取已清除，正在重新抓取全部新數據...")
        self.run_background_fetch()


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

    #sidebar {
        width: 24;
        background: #0d1117;
    }

    #sidebar-nav {
        height: 100%;
    }

    #content-area {
        background: #0d1117;
        layout: vertical;
    }

    #metrics-row {
        height: auto;
        padding: 0 1;
    }

    #holdings-label {
        height: 1;
        padding: 0 2;
        margin-top: 1;
    }

    #holdings-scroll {
        height: 1fr;
        padding: 0 1;
        border: solid #21262d;
    }

    #side-panels {
        height: 14;
        padding: 0 1;
    }

    #broker-dist {
        width: 2fr;
    }

    #pnl-leaderboard {
        width: 1.5fr;
    }

    #recent-events-panel {
        width: 1.5fr;
    }

    #strategy-panels {
        height: 12;
        padding: 0 1;
    }

    #etf-conclusions-panel {
        width: 1fr;
    }

    #options-flow-panel {
        width: 1fr;
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

    def _handle_first_position(self, pos: Optional[Position], user: str) -> None:
        if pos:
            save_manual_positions([pos], [], user=user)
            self.notify("✅ 新增持倉成功！")
            self._start_dashboard(user, [pos], [])
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
        self.push_screen(DashboardScreen(user, positions, self._cash_positions, self._rate), self._handle_dashboard_exit)

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


def main() -> None:
    """
    套件命令列進入點（取代已移除的 cli.py/Typer 層）。

    整個系統只有單一功能（啟動 TUI 看板）與單一選項（--user/-u），
    不需要 Typer 的子指令框架，改用標準函式庫 argparse 即可。
    對應 `pyproject.toml` 的 `[project.scripts]` 與 `entrypoint.py`。
    """
    import argparse

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
