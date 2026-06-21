"""
AssetTrack Textual TUI — 全螢幕事件驅動即時看板
bug#00017 方案三實作 (Textual v8.x)

架構說明：
  - AssetTrackApp (App) 為主應用，啟動後推入 DashboardScreen。
  - 所有子操作（部位調整、歷史、快照）透過 app.suspend() 暫停 TUI，
    執行現有 cli.py 的 Rich/Prompt 互動邏輯，完成後恢復全螢幕。
  - 自動每 60 秒背景刷新報價（獨立 worker thread）。
  - 支援鍵盤快速鍵 1-5 / r / q，以及方向鍵捲動 Holdings。
"""
from __future__ import annotations

import time
import zoneinfo
from datetime import datetime
import calendar
from types import SimpleNamespace
from typing import Optional
from pathlib import Path
import subprocess
import keyring

from rich.box import Box as RichBox
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Static, DataTable, OptionList, Input, Select, Label
from textual.widgets.option_list import Option

from .models import Position
from .quotes import (
    enrich_positions_with_quotes, fetch_usdtwd_rate, fetch_beta,
    draw_bar, nearest_price, is_market_open,
    SOX_TICKERS, group_positions_by_broker, fetch_earnings_calendar,
)
from .storage import load_manual_positions, save_manual_positions, KEYCHAIN_SERVICE

# Console used for suspended-mode (normal terminal) output
_console = Console()

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


def _build_sector_panel(positions: list[Position], rate: float) -> Optional[Panel]:
    has_quotes = any(p.market_price is not None or p.market_value is not None for p in positions)
    if not has_quotes:
        return Panel("\n [yellow]⏳ 載入中...[/yellow]", title="🏷️ Sector", border_style="magenta")
    sector_vals: dict[str, float] = {}
    for p in positions:
        if p.sector:
            v = p.value if p.currency == "USD" else p.value / rate
            sector_vals[p.sector] = sector_vals.get(p.sector, 0.0) + v
    if not sector_vals:
        return None
    total   = sum(sector_vals.values())
    max_sv  = max(sector_vals.values())
    lines   = []
    for sec, sv in sorted(sector_vals.items(), key=lambda x: -x[1]):
        bar = draw_bar(sv, max_sv, 8)
        pct = (sv / total * 100) if total > 0 else 0.0
        lines.append(
            f"[magenta]{sec:<12}[/magenta] [yellow]{bar}[/yellow] [dim]({pct:.1f}%)[/dim]"
        )
    return Panel("\n".join(lines), title="🏷️ Sector", border_style="magenta")


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
        positions = load_manual_positions(user=user)
        self.dismiss((user, positions))


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
    """手動新增持股對話框 — 支援上下鍵欄位導航與必填標注。"""

    # Ordered list of all focusable field IDs (Inputs + Selects)
    _FIELD_IDS: list[str] = [
        "add-broker", "add-account", "add-symbol", "add-type",
        "add-qty", "add-cost", "add-market", "add-exch",
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

    def compose(self) -> ComposeResult:
        brokers = [("manual", "manual"), ("FT", "FT"), ("IBKR", "IBKR")]
        types   = [("stock", "stock"), ("etf", "etf"), ("option", "option")]
        markets = [("US", "US"), ("TW", "TW"), ("HK", "HK"), ("other", "other")]

        with Vertical(id="add-dialog"):
            yield Label("➕ [bold]手動新增持倉部位[/bold]", id="add-title")
            yield Label(
                "💡 [dim]↑↓ 切換欄位　Enter 移至下一欄　[red]★[/red] 必填　[dim]✦ 建議填寫[/dim]",
                id="add-hint"
            )

            with Horizontal(classes="form-row"):
                yield Label("券商 [dim](Broker)[/dim]:", classes="form-label")
                yield Select(brokers, value="manual", id="add-broker")

            with Horizontal(classes="form-row"):
                yield Label("帳戶 [dim](Account)[/dim]:", classes="form-label")
                yield Input(placeholder="例如 default 或子帳戶", id="add-account",
                            classes="form-input")

            with Horizontal(classes="form-row"):
                yield Label("[red]★[/red] 代碼 [dim](Symbol)[/dim]:", classes="form-label")
                yield Input(placeholder="例如 AAPL 或 2330.TW", id="add-symbol",
                            classes="form-input")

            with Horizontal(classes="form-row"):
                yield Label("商品類型 [dim](Type)[/dim]:", classes="form-label")
                yield Select(types, value="stock", id="add-type")

            with Horizontal(classes="form-row"):
                yield Label("[red]★[/red] 數量 [dim](Qty)[/dim]:", classes="form-label")
                yield Input(placeholder="正數，例如 100", id="add-qty",
                            classes="form-input")

            with Horizontal(classes="form-row"):
                yield Label("[yellow]✦[/yellow] 成本 [dim](Cost)[/dim]:", classes="form-label")
                yield Input(placeholder="正數，例如 150.5（建議填寫）", id="add-cost",
                            classes="form-input")

            with Horizontal(classes="form-row"):
                yield Label("市場 [dim](Market)[/dim]:", classes="form-label")
                yield Select(markets, value="US", id="add-market")

            with Horizontal(classes="form-row"):
                yield Label("交易所 [dim](Exch)[/dim]:", classes="form-label")
                yield Input(placeholder="例如 NYSE, TSE [dim](選填)[/dim]", id="add-exch",
                            classes="form-input")

            with Horizontal(classes="form-row"):
                yield Label("貨幣 [dim](Currency)[/dim]:", classes="form-label")
                yield Input(value="USD", placeholder="例如 USD 或 TWD", id="add-curr",
                            classes="form-input")

            with Horizontal(classes="form-row"):
                yield Label("備註 [dim](Notes)[/dim]:", classes="form-label")
                yield Input(placeholder="自訂備註 [dim](選填)[/dim]", id="add-notes",
                            classes="form-input")

            with Horizontal(classes="form-row"):
                yield Label("板塊 [dim](Sector)[/dim]:", classes="form-label")
                yield Input(placeholder="例如 Technology [dim](選填)[/dim]", id="add-sector",
                            classes="form-input")

            yield Label("", id="add-error")
            with Horizontal(id="add-buttons"):
                yield Button("確認新增", variant="primary", id="confirm")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self) -> None:
        # Start focus on first meaningful field
        self.query_one("#add-symbol", Input).focus()

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

        # ── Arrow key / Enter navigation between fields ──────────────────
        if key in ("down", "tab", "enter") or key in ("up", "shift+tab"):
            focused = self.focused
            if focused is None:
                return

            # Find which field index we're currently on
            current_idx = None
            for i, fid in enumerate(self._FIELD_IDS):
                try:
                    widget = self.query_one(f"#{fid}")
                    if widget is focused:
                        current_idx = i
                        break
                except Exception:
                    pass

            if current_idx is None:
                return

            # Prevent Enter from moving focus on Selects (they need Enter to open)
            if key == "enter":
                from textual.widgets import Select as TxSelect
                if isinstance(focused, TxSelect):
                    return   # let Select handle Enter itself
                event.prevent_default()

            step = -1 if key in ("up", "shift+tab") else 1
            next_idx = (current_idx + step) % len(self._FIELD_IDS)
            next_fid = self._FIELD_IDS[next_idx]
            try:
                self.query_one(f"#{next_fid}").focus()
            except Exception:
                pass

    def _submit(self) -> None:
        broker   = self.query_one("#add-broker", Select).value
        account  = self.query_one("#add-account", Input).value.strip()
        symbol   = self.query_one("#add-symbol", Input).value.strip().upper()
        inst_type = self.query_one("#add-type", Select).value
        qty_str  = self.query_one("#add-qty", Input).value.strip()
        cost_str = self.query_one("#add-cost", Input).value.strip()
        market   = self.query_one("#add-market", Select).value
        exch     = self.query_one("#add-exch", Input).value.strip()
        curr     = self.query_one("#add-curr", Input).value.strip().upper()
        notes    = self.query_one("#add-notes", Input).value.strip()
        sector   = self.query_one("#add-sector", Input).value.strip()

        error_lbl = self.query_one("#add-error", Label)

        # ── Required field validation ─────────────────────────────────────
        if not symbol:
            error_lbl.update("❌ [red]★ 商品代碼[/red] 為必填，請輸入代碼（例如 AAPL）")
            self.query_one("#add-symbol", Input).focus()
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

        if market == "TW" and not symbol.endswith(".TW") and not symbol.endswith(".TWO"):
            symbol = symbol + ".TW"

        try:
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
            Position.model_validate(pos)
            self.dismiss(pos)
        except Exception as e:
            error_lbl.update(f"❌ 資料驗證失敗: {e}")


class PerformanceHistoryScreen(Screen):
    """統一風格的績效歷史回測與大盤對比 Screen (TUI 完全內置，不 suspend)。"""

    BINDINGS = [
        Binding("escape", "go_back", "返回看板"),
        Binding("q", "go_back", "返回看板", show=False),
    ]

    DEFAULT_CSS = """
    PerformanceHistoryScreen {
        background: #0d1117;
        layout: horizontal;
    }
    #perf-left-panel {
        width: 30;
        background: #161b22;
        border-right: solid #21262d;
        padding: 1 2;
        layout: vertical;
    }
    #perf-left-title {
        text-style: bold;
        color: #58a6ff;
        margin-bottom: 2;
    }
    .perf-select-label {
        color: #8b949e;
        margin-top: 1;
        margin-bottom: 1;
    }
    .perf-select {
        margin-bottom: 2;
        border: solid #30363d;
        background: #0d1117;
    }
    #perf-run-btn {
        margin-top: 2;
        width: 100%;
    }
    #perf-right-panel {
        width: 1fr;
        padding: 1 2;
    }
    #perf-status {
        color: #8b949e;
        margin-bottom: 1;
    }
    #perf-summary {
        height: auto;
        margin-bottom: 1;
    }
    #perf-chart {
        height: 16;
        color: #58a6ff;
        border: solid #21262d;
        padding: 1 2;
        margin-bottom: 1;
    }
    #perf-table-title {
        text-style: bold;
        color: #f0f6fc;
        margin-top: 1;
        margin-bottom: 1;
    }
    #perf-table {
        height: 12;
        border: solid #21262d;
    }
    #perf-events-title {
        text-style: bold;
        color: #f0f6fc;
        margin-top: 1;
        margin-bottom: 1;
    }
    #perf-events-table {
        height: 8;
        border: solid #21262d;
    }
    """

    def __init__(self, user: str, positions: list[Position]) -> None:
        super().__init__()
        self.user = user
        self.positions = positions

    def compose(self) -> ComposeResult:
        periods = [("近 60 天", "1"), ("近 180 天", "2"), ("YTD", "3")]
        benchmarks = [("SPY", "1"), ("QQQ", "2"), ("^GSPC", "3"), ("停用", "4")]
        
        with Container(id="perf-left-panel"):
            yield Label("📊 績效歷史回測", id="perf-left-title")
            yield Label("選擇比較期間:", classes="perf-select-label")
            yield Select(periods, value="1", id="perf-period")
            yield Label("選擇比較基準:", classes="perf-select-label")
            yield Select(benchmarks, value="1", id="perf-benchmark")
            yield Button("開始分析", variant="primary", id="perf-run-btn")
            
        with ScrollableContainer(id="perf-right-panel"):
            yield Label("💡 請選擇左側參數後點擊「開始分析」", id="perf-status")
            yield Static("", id="perf-summary")
            yield Static("", id="perf-chart")
            yield Label("每週績效明細", id="perf-table-title")
            yield DataTable(id="perf-table")
            yield Label("📅 近期重大總經事件 (未來 90 天)", id="perf-events-title")
            yield DataTable(id="perf-events-table")
            yield Footer()

    def on_mount(self) -> None:
        self.query_one("#perf-run-btn", Button).focus()
        self.query_one("#perf-table", DataTable).add_columns("週節點", "組合市值 (USD)", "週變動", "基準指數", "超額")
        self.query_one("#perf-events-table", DataTable).add_columns("日期", "事件", "距今")

    def action_go_back(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "perf-run-btn":
            self._start_backtest()

    def _start_backtest(self) -> None:
        period_idx = self.query_one("#perf-period", Select).value
        bm_idx = self.query_one("#perf-benchmark", Select).value
        
        self.query_one("#perf-status", Label).update("🔍 正在下載行情與計算績效，請稍候...")
        self.query_one("#perf-run-btn", Button).disabled = True
        self.run_backtest_calc(period_idx, bm_idx)

    @work(thread=True)
    def run_backtest_calc(self, period_idx: str, bm_idx: str) -> None:
        from datetime import datetime as dt_cls, timedelta, date as date_type
        import math
        from .cli import get_upcoming_macro_events, draw_history_chart
        from .quotes import fetch_historical_prices_weekly
        
        tradeable = [
            p for p in self.positions
            if p.instrument_type in ("stock", "etf", "other")
            and p.quantity and p.quantity > 0
            and p.currency.upper() == "USD"
        ]
        
        if not tradeable:
            self.app.call_from_thread(self._on_calc_error, "找不到可回測的 USD 計價持倉 (stock/ETF)。")
            return
            
        today = datetime.utcnow().date()
        if period_idx == "1":
            days = 60
            start_date = today - timedelta(days=60)
            period_label = "近 60 天"
        elif period_idx == "2":
            days = 180
            start_date = today - timedelta(days=180)
            period_label = "近 180 天"
        else:
            start_date = date_type(today.year, 1, 1)
            days = (today - start_date).days
            period_label = f"YTD ({today.year} 年初至今)"

        bm_map = {"1": "SPY", "2": "QQQ", "3": "^GSPC", "4": "none"}
        bm_symbol = bm_map[bm_idx]
        use_benchmark = bm_symbol != "none"

        week_dates = []
        cursor = start_date
        while cursor <= today:
            week_dates.append(cursor)
            cursor += timedelta(days=7)
        if not week_dates or week_dates[-1] < today:
            week_dates.append(today)

        start_dt = dt_cls.combine(start_date, dt_cls.min.time())
        end_dt = dt_cls.combine(today, dt_cls.min.time())
        
        symbols = [p.symbol for p in tradeable]
        unique_syms = list(dict.fromkeys(symbols))
        
        try:
            price_data = fetch_historical_prices_weekly(unique_syms, start_dt, end_dt)
        except Exception as e:
            self.app.call_from_thread(self._on_calc_error, f"下載歷史股價失敗: {e}")
            return
            
        has_any_data = any(bool(v) for v in price_data.values())
        if not has_any_data:
            self.app.call_from_thread(self._on_calc_error, "無法從網路下載歷史股價。")
            return


        port_weekly = []
        broker_set = sorted(set(p.broker for p in tradeable))
        broker_weekly = {b: [] for b in broker_set}

        for wd in week_dates:
            week_total = 0.0
            broker_totals = {b: 0.0 for b in broker_set}
            for pos in tradeable:
                pm = price_data.get(pos.symbol, {})
                p_price = nearest_price(pm, wd)
                if p_price is not None and not math.isnan(p_price):
                    val = p_price * pos.quantity
                    week_total += val
                    broker_totals[pos.broker] = broker_totals.get(pos.broker, 0.0) + val
            port_weekly.append(week_total)
            for b in broker_set:
                broker_weekly[b].append(broker_totals.get(b, 0.0))

        valid_mask = [v > 0 for v in port_weekly]
        if sum(valid_mask) < 2:
            self.app.call_from_thread(self._on_calc_error, "有效歷史資料節點不足 2 個。")
            return

        filt_dates = [week_dates[i] for i in range(len(week_dates)) if valid_mask[i]]
        filt_port = [port_weekly[i] for i in range(len(port_weekly)) if valid_mask[i]]
        filt_broker = {b: [broker_weekly[b][i] for i in range(len(week_dates)) if valid_mask[i]] for b in broker_set}

        bm_weekly = [None] * len(filt_dates)
        bm_return = None
        alpha = None
        if use_benchmark:
            try:
                bm_price_data = fetch_historical_prices_weekly([bm_symbol], start_dt, end_dt)
                bm_pm = bm_price_data.get(bm_symbol, {})
                if bm_pm:
                    bm_price_0 = nearest_price(bm_pm, filt_dates[0])
                    port_val_0 = filt_port[0]
                    if bm_price_0 and port_val_0 > 0:
                        bm_shares = port_val_0 / bm_price_0
                        for i, wd in enumerate(filt_dates):
                            bm_p = nearest_price(bm_pm, wd)
                            if bm_p is not None:
                                bm_weekly[i] = bm_shares * bm_p

                        bm_first = next((v for v in bm_weekly if v is not None), None)
                        bm_last = next((v for v in reversed(bm_weekly) if v is not None), None)
                        if bm_first and bm_last and bm_first > 0:
                            bm_return = (bm_last / bm_first) - 1.0
                else:
                    use_benchmark = False
            except Exception:
                use_benchmark = False

        port_return = None
        if filt_port[0] > 0:
            port_return = (filt_port[-1] / filt_port[0]) - 1.0
            if bm_return is not None:
                alpha = port_return - bm_return

        chart_str = ""
        try:
            chart_str = draw_history_chart(
                filt_dates, filt_port,
                bm_vals=bm_weekly if use_benchmark else None,
                broker_weekly=filt_broker if len(broker_set) > 1 else None,
                bm_label=bm_symbol,
                width=66, height=12
            )
        except Exception as e:
            chart_str = f"圖表產生失敗: {e}"

        summary_str = ""
        if port_return is not None:
            pr_c = "green" if port_return >= 0 else "red"
            pr_s = "+" if port_return >= 0 else ""
            summary_str += f"組合回報: [bold {pr_c}]{pr_s}{port_return * 100:.2f}%[/]   "
        if use_benchmark and bm_return is not None:
            bm_c = "green" if bm_return >= 0 else "red"
            bm_s = "+" if bm_return >= 0 else ""
            summary_str += f"{bm_symbol}: [bold {bm_c}]{bm_s}{bm_return * 100:.2f}%[/]   "
        if alpha is not None:
            al_c = "green" if alpha >= 0 else "red"
            al_s = "+" if alpha >= 0 else ""
            summary_str += f"Alpha: [bold {al_c}]{al_s}{alpha * 100:.2f}%[/]   "
        if filt_port:
            summary_str += f"\n當前市值: [bold white]${filt_port[-1]:,.0f}[/bold white]"

        table_rows = []
        prev_pv = None
        for i, wd in enumerate(filt_dates):
            pv = filt_port[i]
            chg_str = "—"
            if prev_pv is not None and prev_pv > 0:
                diff = pv - prev_pv
                pct = diff / prev_pv * 100
                cc = "green" if diff >= 0 else "red"
                cs = "+" if diff >= 0 else ""
                chg_str = f"[{cc}]{cs}${diff:,.0f} ({cs}{pct:.1f}%)[/{cc}]"
                
            bm_str = "—"
            rel_str = "—"
            if use_benchmark:
                bm_v = bm_weekly[i]
                bm_str = f"${bm_v:,.0f}" if bm_v is not None else "—"
                if bm_v is not None and bm_v > 0:
                    rel_pct = (pv / bm_v - 1) * 100
                    rc = "green" if rel_pct >= 0 else "red"
                    rs = "+" if rel_pct >= 0 else ""
                    rel_str = f"[{rc}]{rs}{rel_pct:.1f}%[/{rc}]"
            table_rows.append((wd.strftime("%Y-%m-%d"), f"${pv:,.0f}", chg_str, bm_str, rel_str))
            prev_pv = pv

        events = []
        try:
            raw_events = get_upcoming_macro_events()
            for ev_date, ev_label, time_str in raw_events:
                days_away = (ev_date - today).days
                event_name = {
                    "▼FED": "FED FOMC 利率決議",
                    "★NFP": "非農就業報告 (NFP)",
                    "◆CPI": "CPI 通膨指數公佈",
                }.get(ev_label, ev_label)
                events.append((ev_date.strftime("%Y-%m-%d"), f"{ev_label} {event_name} ({time_str})", f"{days_away} 天後"))
        except Exception:
            pass

        result = {
            "period_label": period_label,
            "summary": summary_str,
            "chart": chart_str,
            "table_rows": table_rows,
            "events": events
        }
        self.app.call_from_thread(self._on_calc_success, result)

    def _on_calc_error(self, err_msg: str) -> None:
        self.query_one("#perf-status", Label).update(f"❌ 錯誤: {err_msg}")
        self.query_one("#perf-run-btn", Button).disabled = False

    def _on_calc_success(self, res: dict) -> None:
        self.query_one("#perf-status", Label).update(f"✅ 績效歷史分析完成 ({res['period_label']})")
        self.query_one("#perf-run-btn", Button).disabled = False
        
        self.query_one("#perf-summary", Static).update(Panel(res["summary"], border_style="yellow", title="📊 績效摘要"))
        self.query_one("#perf-chart", Static).update(res["chart"])
        
        table = self.query_one("#perf-table", DataTable)
        table.clear()
        for row in res["table_rows"]:
            table.add_row(*row)
            
        evt_table = self.query_one("#perf-events-table", DataTable)
        evt_table.clear()
        for r in res["events"]:
            evt_table.add_row(*r)


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
    """確認刪除部位對話框 (TUI Modal)。"""
    DEFAULT_CSS = """
    DeleteConfirmModal {
        align: center middle;
    }
    #delete-dialog {
        width: 44;
        height: auto;
        border: thick $error;
        background: $panel;
        padding: 1 2;
    }
    #delete-msg {
        margin-bottom: 1;
    }
    #delete-buttons {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    #delete-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, pos: Position) -> None:
        super().__init__()
        self.pos = pos

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            yield Static(f"[bold red]⚠️ 確定要移除此持倉部位？[/bold red]\n\n[cyan]{self.pos.broker}[/] - [bold white]{self.pos.symbol}[/]", id="delete-msg")
            with Horizontal(id="delete-buttons"):
                yield Button("確定刪除", variant="error", id="confirm")
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
        from .cli import get_upcoming_macro_events

        # 1. Gather unique symbols
        portfolio_tickers = set()
        for p in self.positions:
            sym = p.underlying if p.instrument_type == "option" else p.symbol
            norm_sym = _normalize_symbol_for_yf(sym, "stock", p.currency)
            portfolio_tickers.add(norm_sym)

        unique_tickers = list(portfolio_tickers.union(SOX_TICKERS))

        ticker_to_data = fetch_earnings_calendar(unique_tickers)

        today = datetime.utcnow().date()
        start_date = today - timedelta(days=30)
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
        macro_list = get_upcoming_macro_events(days=90, start_days_ago=30)
        for ev_date, ev_label, time_str in macro_list:
            if start_date <= ev_date <= cutoff:
                from .cli import MACRO_EVENT_NAMES
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
        Binding("4",   "performance_history",  "歷史績效"),
        Binding("5",   "upcoming_events",      "近期重大事件"),
        Binding("6",   "save_snapshot",        "儲存快照"),
        Binding("r",   "refresh_now",          "重整",   show=False),
        Binding("q",   "logout",               "登出",   show=False),
        Binding("ctrl+c", "logout",            "強制登出", show=False),
    ]

    def __init__(self, user: str, positions: list[Position], rate: float) -> None:
        super().__init__()
        self._user: str              = user
        self._positions: list[Position] = positions
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
                    Option("📊 歷史績效", id="history"),
                    Option("📅 近期重大事件", id="upcoming_events"),
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
        elif action == "history":
            self.action_performance_history()
        elif action == "snapshot":
            self.action_save_snapshot()
        elif action == "upcoming_events":
            self.action_upcoming_events()
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

        positions = load_manual_positions(user=self._user)
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
            dup.last_updated = datetime.utcnow()
            positions.remove(target)

        save_manual_positions(positions, user=self._user)
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

        positions = load_manual_positions(user=self._user)
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

        save_manual_positions(positions, user=self._user)
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

        positions = load_manual_positions(user=self._user)
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
            dup.last_updated = datetime.utcnow()
            positions.remove(target)

        save_manual_positions(positions, user=self._user)
        self._do_refresh_worker()

    def _handle_delete_confirm(self, pos: Position, confirmed: Optional[bool]) -> None:
        if not confirmed:
            return
        positions = load_manual_positions(user=self._user)
        target = next((p for p in positions if p.broker == pos.broker and (p.account or "") == (pos.account or "") and p.symbol == pos.symbol), None)
        if target:
            positions.remove(target)
            save_manual_positions(positions, user=self._user)
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
                self._positions = load_manual_positions(user=self._user)
            if self._positions:
                self._positions = enrich_positions_with_quotes(self._positions, delay=0.1)
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
        from .cli import get_upcoming_macro_events

        try:
            portfolio_tickers = set()
            for p in self._positions:
                sym = p.underlying if p.instrument_type == "option" else p.symbol
                norm_sym = _normalize_symbol_for_yf(sym, "stock", p.currency)
                portfolio_tickers.add(norm_sym)

            unique_tickers = list(portfolio_tickers.union(SOX_TICKERS))

            ticker_to_data = fetch_earnings_calendar(unique_tickers)

            today = datetime.utcnow().date()
            start_date = today - timedelta(days=30)
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

            from .cli import MACRO_EVENT_NAMES
            macro_list = get_upcoming_macro_events(days=90, start_days_ago=30)
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
            lines.append(f"[dim]... 還有 {len(recent) - 8} 個事件 (按 [bold]5[/bold] 詳情)[/dim]")
            
        return Panel("\n".join(lines), title="📅 近期重大事件", border_style="cyan")

    # ── Action handlers ───────────────────────────────────────────────────────

    def action_adjust_positions(self) -> None:
        modal = AdjustPositionsModal()
        self.app.push_screen(modal, self._handle_adjust_choice)

    def _handle_adjust_choice(self, choice: Optional[str]) -> None:
        if choice == "add":
            self.app.push_screen(AddPositionModal(), self._handle_add_position_result)

    def _handle_add_position_result(self, pos: Optional[Position]) -> None:
        if pos:
            positions = load_manual_positions(self._user)
            matched = False
            for p in positions:
                if p.broker.lower() == pos.broker.lower() and (p.account or "").lower() == (pos.account or "").lower() and p.symbol.upper() == pos.symbol.upper():
                    old_qty = p.quantity
                    new_qty = old_qty + pos.quantity
                    if new_qty > 0:
                        if p.avg_cost is not None and pos.avg_cost is not None:
                            p.avg_cost = (p.avg_cost * old_qty + pos.avg_cost * pos.quantity) / new_qty
                        else:
                            p.avg_cost = pos.avg_cost or p.avg_cost
                    p.quantity = new_qty
                    p.last_updated = datetime.utcnow()
                    matched = True
                    break
            if not matched:
                positions.append(pos)
            save_manual_positions(positions, self._user)
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

    def action_performance_history(self) -> None:
        """[4] 歷史績效：推入 PerformanceHistoryScreen，不 suspend。"""
        latest_positions = load_manual_positions(self._user)
        self.app.push_screen(PerformanceHistoryScreen(self._user, latest_positions))

    def action_save_snapshot(self) -> None:
        """[6] 儲存快照：背景非阻塞執行，不 suspend。"""
        self.app.notify("⚙️ 正在儲存市值快照...")
        self.run_save_snapshot()

    @work(thread=True)
    def run_save_snapshot(self) -> None:
        from .cli import refresh_snapshot as cli_snapshot
        ctx = SimpleNamespace(obj=self._user)
        try:
            cli_snapshot(ctx, save=True)
            self.app.call_from_thread(self.app.notify, "✅ 市值快照儲存成功！", title="快照")
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"❌ 儲存快照失敗: {e}", title="快照", severity="error")

    def action_upcoming_events(self) -> None:
        """[5] 近期重大事件：推入 UpcomingEventsScreen，不 suspend。"""
        self.app.push_screen(UpcomingEventsScreen(self._user, self._positions, self._rate, self._upcoming_events, self._events_fetched))


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
    """

    def __init__(self, user: str = "default", positions: Optional[list[Position]] = None, rate: float = 32.5) -> None:
        super().__init__()
        self.default_user = user
        self._user = user
        self._positions = positions if positions is not None else []
        self._rate = rate

    def on_mount(self) -> None:
        if self._positions:
            self._start_dashboard(self._user, self._positions)
        else:
            self.push_screen(LoginScreen(default_user=self.default_user), self._handle_login_complete)

    def _handle_login_complete(self, result: Optional[tuple[str, list[Position]]]) -> None:
        if result is None:
            self.exit()
            return
            
        user, positions = result
        self._user = user
        self._positions = positions
        
        if not positions:
            self.push_screen(OnboardingModal(), lambda choice: self._handle_onboarding_choice(choice, user))
        else:
            self._start_dashboard(user, positions)

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
            save_manual_positions(sample_positions, user=user)
            self.notify("✅ 已為您成功建立 AAPL (50股) 與 TSLA (10股) 預設範例部位！")
            self._start_dashboard(user, sample_positions)
        elif choice == "manual":
            self.push_screen(AddPositionModal(), lambda pos: self._handle_first_position(pos, user))
        else:
            self._start_dashboard(user, [])

    def _handle_first_position(self, pos: Optional[Position], user: str) -> None:
        if pos:
            save_manual_positions([pos], user=user)
            self.notify("✅ 新增持倉成功！")
            self._start_dashboard(user, [pos])
        else:
            self._start_dashboard(user, [])

    def _start_dashboard(self, user: str, positions: list[Position]) -> None:
        rate = 32.0
        try:
            rate = _get_cached_usdtwd_rate()
        except Exception:
            pass
        self._rate = rate
        self._positions = positions
        self.push_screen(DashboardScreen(user, positions, rate), self._handle_dashboard_exit)

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
