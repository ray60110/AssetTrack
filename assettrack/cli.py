from __future__ import annotations

import re
import sys
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import zoneinfo

import typer
import keyring
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt
from rich.table import Table
from .models import PortfolioSnapshot, Position
from .quotes import (
    current_portfolio_value, enrich_positions_with_quotes, fetch_usdtwd_rate,
    fetch_beta, fetch_benchmark_history, fetch_historical_prices_weekly,
    draw_bar, nearest_price, is_market_open,
    SOX_TICKERS, group_positions_by_broker, fetch_earnings_calendar,
)
from .storage import Storage, get_positions_path, load_manual_positions, save_manual_positions, KEYCHAIN_SERVICE

console = Console()
app = typer.Typer(
    name="assettrack",
    help="Track your portfolio current value (stocks + options) across brokers.",
    add_completion=False,
)


# ── Macro event display name mapping (shared with TUI via import) ─────────
MACRO_EVENT_NAMES: dict[str, str] = {
    "▼FED": "▼ FED 利率決議",
    "★NFP": "★ NFP 非農就業 / 失業率",
    "◆CPI": "◆ CPI 通膨指數公佈",
}


def authenticate_user(user: str) -> bool:
    """Register or validate user via system keychain and native Touch ID helper."""
    pwd = keyring.get_password(KEYCHAIN_SERVICE, user)
    
    if pwd is None:
        console.print(Panel(
            f"👤 [bold yellow]註冊新使用者: {user}[/bold yellow]\n\n"
            "系統偵測到您是第一次使用此 ID。\n"
            "請設定一組密碼，系統會將密碼安全儲存於系統 Keychain 中。\n"
            "日後可透過系統密碼或 Touch ID 快速登入。",
            title="✨ 新使用者初始化 ✨",
            border_style="yellow"
        ))
        while True:
            p1 = Prompt.ask("請輸入登入密碼", password=True)
            p2 = Prompt.ask("請再次確認密碼", password=True)
            if p1 == p2:
                keyring.set_password(KEYCHAIN_SERVICE, user, p1)
                console.print("[green]註冊成功！密碼已安全儲存。[/green]")
                time.sleep(1)
                return True
            else:
                console.print("[red]密碼輸入不一致，請重新輸入。[/red]")
                
    # Returning User - Attempt Touch ID authentication first
    console.print("[cyan]正在呼叫系統 Touch ID 模組進行辨識...[/cyan]")
    touchid_helper_path = Path(__file__).parent / "touchid_helper"
    if touchid_helper_path.exists():
        try:
            res = subprocess.run([str(touchid_helper_path)], capture_output=True)
            if res.returncode == 0:
                console.print("[green]Touch ID 驗證成功！自動登入系統。[/green]")
                time.sleep(1)
                return True
            elif res.returncode == 1:
                console.print("[yellow]⚠️ 指紋驗證失敗或已被取消。將改為使用密碼驗證。[/yellow]")
            else:
                console.print("[yellow]⚠️ 生物辨識模組不可用。將改為使用密碼驗證。[/yellow]")
        except Exception:
            console.print("[yellow]⚠️ 無法執行指紋辨識輔助程式。將改為使用密碼驗證。[/yellow]")
    else:
        console.print("[yellow]⚠️ 指紋辨識程式未配置。將改為使用密碼驗證。[/yellow]")
        
    # Password Validation (3 attempts)
    attempts = 3
    while attempts > 0:
        entered_pwd = Prompt.ask("請輸入您的登入密碼", password=True)
        if entered_pwd == pwd:
            console.print("[green]密碼驗證成功！[/green]")
            time.sleep(1)
            return True
        attempts -= 1
        if attempts > 0:
            console.print(f"[red]密碼錯誤！還剩 {attempts} 次機會。[/red]")
        else:
            console.print("[bold red]❌ 驗證失敗次數過多，登入程序已終止！[/bold red]")
            sys.exit(1)
            
    return False


def show_onboarding_menu(user: str, ctx: typer.Context) -> list[Position]:
    """Onboarding menu wizard for empty position users."""
    console.print(Panel(
        "⚠️ [bold yellow]偵測到您目前尚無 any 持倉部位！[/bold yellow]\n\n"
        "請選擇以下任一操作來開始使用您的 AssetTrack 看板：\n"
        "1️⃣  建立預設範例部位 (AAPL, TSLA，快速體驗看板效果)\n"
        "2️⃣  手動新增持倉部位 (一個個輸入您的持股商品與成本)\n"
        "3️⃣  保持空白並直接進入看板 (待日後再新增)",
        title="✨ 新使用者初始化引導 ✨",
        border_style="cyan"
    ))
    
    choice = Prompt.ask("請輸入您的選擇 [1/2/3]", choices=["1", "2", "3"], default="1")
    if choice == "1":
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
        console.print("[green]已為您成功建立 AAPL (50股) 與 TSLA (10股) 預設範例部位！[/green]")
        time.sleep(1.5)
        return sample_positions
    elif choice == "2":
        try:
            add(ctx, broker=None)
        except (typer.Exit, Exception) as e:
            console.print(f"[red]手動新增持倉已結束或異常: {e}[/red]")
        return load_manual_positions(user=user)
    else:
        return []



def _build_broker_holdings(
    positions: list[Position],
    rate: float,
    show_pnl: bool = True,
    weights: Optional[dict] = None,
) -> None:
    """Render holdings grouped by broker, each group sorted by current market value descending."""

    sorted_brokers = group_positions_by_broker(positions, rate)

    # ── Single shared table — guarantees global column-width alignment ──
    from rich import box as rich_box
    from rich.box import Box as RichBox

    # Custom box: visible ─ line for both header underline (row 3) and
    # end_section separator (row 5). All other borders are empty/invisible.
    # Row order: top / head / head_row / mid_head / row(=end_section) / mid_foot / foot / bottom
    _SECTION_BOX = RichBox(
        "    \n"   # top       — empty
        "    \n"   # head      — empty
        " \u2500\u2500 \n"   # head_row  — header column underline
        "    \n"   # mid_head  — empty
        " \u2500\u2500 \n"   # row       — end_section separator (THE FIX)
        "    \n"   # mid_foot  — empty
        "    \n"   # foot      — empty
        "    \n"   # bottom    — empty
    )

    table = Table(
        box=_SECTION_BOX,
        padding=(0, 2, 0, 1),
        show_header=True,
        header_style="bold dim",
        expand=False,
    )

    table.add_column("Symbol",       style="bold white",  min_width=8,  no_wrap=True)
    table.add_column("Type",         style="dim",          min_width=6,  no_wrap=True)
    table.add_column("Qty",          justify="right",      min_width=6)
    table.add_column("Avg Cost",     justify="right",      min_width=9)
    table.add_column("Price",        justify="right",      min_width=9)
    table.add_column("Market Value", justify="right",      style="bold", min_width=13)
    if weights is not None:
        table.add_column("Wt%",      justify="right",      style="dim",  min_width=5)
    table.add_column("今日%",        justify="right",      min_width=8)
    table.add_column("今日漲跌",     justify="right",      min_width=11)
    table.add_column("市場",         justify="center",     min_width=6)
    if show_pnl:
        table.add_column("Unrealized P&L", justify="right", min_width=18)

    n_cols = (
        6
        + (1 if weights is not None else 0)
        + 3   # 今日% + 今日漲跌 + 市場
        + (1 if show_pnl else 0)
    )

    for broker_idx, (bk, bk_positions) in enumerate(sorted_brokers):
        bk_total_usd = sum(
            p.value if p.currency == "USD" else p.value / rate
            for p in bk_positions
        )
        # ── Blank gap row between broker groups ──
        if broker_idx > 0:
            table.add_row(*[""] * n_cols, end_section=False)

        # ── Broker header row ──
        # end_section=True → divider line drawn immediately below this row (robust vs data row wrapping)
        bk_label = f"[bold cyan]▐  {bk.upper()}[/bold cyan]"
        bk_subtotal = f"[bold white]${bk_total_usd:,.0f}[/bold white] [dim]USD[/dim]"

        header_cells = [bk_label] + [""] * (n_cols - 2) + [bk_subtotal]
        table.add_row(*header_cells, style="cyan", end_section=True)

        for p in bk_positions:
            qty_str   = f"{p.quantity:,.2f}" if p.quantity % 1 != 0 else f"{int(p.quantity):,}"
            cost_str  = f"${p.avg_cost:,.2f}" if p.avg_cost is not None else "[dim]—[/dim]"
            price_str = f"${p.market_price:,.2f}" if p.market_price is not None else "[dim]—[/dim]"
            val_str   = f"${p.value:,.2f}"
            status_str = "[green]開市[/green]" if is_market_open(p) else "[dim]休市[/dim]"

            # Daily change
            d_chg = p.daily_change
            d_pct = p.daily_change_pct
            if d_chg is not None and d_pct is not None:
                d_color = "green" if d_chg >= 0 else "red"
                d_sign  = "+" if d_chg >= 0 else ""
                ccy_tag = "" if p.currency == "USD" else f" {p.currency}"
                daily_pct_str = f"[{d_color}]{d_sign}{d_pct:.2f}%[/{d_color}]"
                daily_chg_str = f"[{d_color}]{d_sign}{d_chg:,.0f}{ccy_tag}[/{d_color}]"
            else:
                daily_pct_str = "[dim]—[/dim]"
                daily_chg_str = "[dim]—[/dim]"

            row_cells = [p.symbol, p.instrument_type, qty_str, cost_str, price_str, val_str]

            if weights is not None:
                key = (p.broker, p.account or "", p.symbol)
                w = weights.get(key, 0.0)
                row_cells.append(f"{w:.1f}%")

            row_cells.extend([daily_pct_str, daily_chg_str, status_str])

            if show_pnl:
                pnl = p.unrealized_pnl
                pct = p.unrealized_pnl_pct
                if pnl is not None and pct is not None:
                    color = "green" if pnl >= 0 else "red"
                    sign  = "+" if pnl >= 0 else ""
                    row_cells.append(
                        f"[{color}]{sign}${pnl:,.2f}[/{color}] "
                        f"[dim]({sign}{pct:.2f}%)[/dim]"
                    )
                else:
                    row_cells.append("[dim]—[/dim]")

            # All data rows: end_section=False
            table.add_row(*row_cells, end_section=False)

    console.print(table)


def _build_positions_table(
    positions: list[Position],
    show_pnl: bool = True,
    weights: Optional[dict] = None,
) -> Table:
    """Format holdings details into a rich terminal table (flat, ungrouped — used outside dashboard)."""
    table = Table(box=None, padding=(0, 1, 0, 1))
    table.add_column("Broker", style="cyan")
    table.add_column("Symbol", style="bold white")
    table.add_column("Type", style="dim")
    table.add_column("Qty", justify="right")
    table.add_column("Avg Cost", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Market Value", justify="right", style="bold")
    if weights is not None:
        table.add_column("Weight", justify="right", style="dim")
    table.add_column("市場狀態", justify="center")
    if show_pnl:
        table.add_column("Unrealized P&L", justify="right")

    for p in positions:
        qty_str = f"{p.quantity:,.2f}" if p.quantity % 1 != 0 else f"{int(p.quantity):,}"
        cost_str = f"${p.avg_cost:,.2f}" if p.avg_cost is not None else "—"
        price_str = f"${p.market_price:,.2f}" if p.market_price is not None else "等待行情"
        val_str = f"${p.value:,.2f}"

        status_str = "[green]開市[/green]" if is_market_open(p) else "[red]未開市[/red]"

        broker_display = f"{p.broker} ({p.account})" if p.account else p.broker
        row_cells = [
            broker_display,
            p.symbol,
            p.instrument_type,
            qty_str,
            cost_str,
            price_str,
            val_str,
        ]

        if weights is not None:
            key = (p.broker, p.account or "", p.symbol)
            w = weights.get(key, 0.0)
            row_cells.append(f"{w:.1f}%")

        row_cells.append(status_str)

        if show_pnl:
            pnl = p.unrealized_pnl
            pct = p.unrealized_pnl_pct
            if pnl is not None and pct is not None:
                color = "green" if pnl >= 0 else "red"
                sign = "+" if pnl >= 0 else ""
                row_cells.append(f"[{color}]{sign}${pnl:,.2f} ({sign}{pct:.2f}%)[/{color}]")
            else:
                row_cells.append("—")

        table.add_row(*row_cells)

    return table



def render_dashboard_once(user: str, positions: list[Position], rate: float):
    """Render a single frame of the live dashboard panel."""
    from rich.columns import Columns
    console.clear()

    now_str = datetime.now().strftime("%H:%M:%S")
    header_text = (
        f"👤 使用者帳戶 (User Profile): [bold cyan]{user}[/bold cyan] | "
        f"🕒 最後更新 (Last Update): [bold]{now_str}[/bold]"
    )
    console.print(Panel(header_text, title="✨ AssetTrack 即時監控看板 ✨", border_style="cyan"))

    if not positions:
        console.print("沒有持倉部位。")
    else:
        # ── Pre-calculate totals, broker breakdown, weights ──
        total_usd = 0.0
        total_cost_usd = 0.0
        has_cost = False
        broker_vals: dict[str, float] = {}
        weights: dict[tuple, float] = {}

        for p in positions:
            val_usd = p.value if p.currency == "USD" else p.value / rate
            total_usd += val_usd
            bk = f"{p.broker} ({p.account})" if p.account else p.broker
            broker_vals[bk] = broker_vals.get(bk, 0.0) + val_usd
            if p.total_cost is not None:
                cost_usd = p.total_cost if p.currency == "USD" else p.total_cost / rate
                total_cost_usd += cost_usd
                has_cost = True

        for p in positions:
            val_usd = p.value if p.currency == "USD" else p.value / rate
            key = (p.broker, p.account or "", p.symbol)
            weights[key] = (val_usd / total_usd * 100) if total_usd > 0 else 0.0

        total_twd = total_usd * rate
        total_pnl_usd = (total_usd - total_cost_usd) if has_cost else None
        total_pnl_pct = (
            (total_pnl_usd / total_cost_usd * 100)
            if (total_pnl_usd is not None and total_cost_usd > 0) else None
        )

        # ── Portfolio Beta (weighted by USD market value) ──
        beta_numerator = 0.0
        beta_denominator = 0.0
        for p in positions:
            beta = fetch_beta(
                symbol=p.symbol,
                instrument_type=p.instrument_type,
                underlying=p.underlying,
                currency=p.currency,
            )
            if beta is not None:
                val_usd = p.value if p.currency == "USD" else p.value / rate
                beta_numerator += beta * val_usd
                beta_denominator += val_usd
        portfolio_beta = (beta_numerator / beta_denominator) if beta_denominator > 0 else None

        # ── Metrics Row (5 panels) ──
        metrics_tbl = Table(box=None, padding=(0, 1, 0, 1), show_header=False, expand=True)
        metrics_tbl.add_column(justify="center", ratio=3)
        metrics_tbl.add_column(justify="center", ratio=3)
        metrics_tbl.add_column(justify="center", ratio=2)
        metrics_tbl.add_column(justify="center", ratio=2)
        metrics_tbl.add_column(justify="center", ratio=2)

        val_panel_text = (
            f"[bold green]${total_usd:,.2f} USD[/bold green]\n"
            f"[dim]NT${total_twd:,.2f} TWD[/dim]\n"
            f"[dim]USDTWD: {rate:.2f}[/dim]"
        )

        if total_pnl_usd is not None and total_pnl_pct is not None:
            pnl_color = "green" if total_pnl_usd >= 0 else "red"
            pnl_sign = "+" if total_pnl_usd >= 0 else ""
            pnl_panel_text = (
                f"[{pnl_color} bold]{pnl_sign}${total_pnl_usd:,.2f}[/{pnl_color} bold]\n"
                f"[{pnl_color}]{pnl_sign}{total_pnl_pct:.2f}%[/{pnl_color}]"
            )
            pnl_border = "green" if total_pnl_usd >= 0 else "red"
        else:
            pnl_panel_text = "[dim]—[/dim]\n[dim]無成本資料[/dim]"
            pnl_border = "dim"

        if portfolio_beta is not None:
            if portfolio_beta <= 0.8:
                beta_color = "green"
            elif portfolio_beta <= 1.2:
                beta_color = "yellow"
            else:
                beta_color = "red"
            beta_panel_text = (
                f"[{beta_color} bold]{portfolio_beta:.2f}[/{beta_color} bold]\n"
                f"[dim]vs SPY[/dim]"
            )
            beta_border = beta_color
        else:
            beta_panel_text = "[dim]—[/dim]\n[dim]資料不足[/dim]"
            beta_border = "dim"

        metrics_tbl.add_row(
            Panel(val_panel_text, title="📊 Total Portfolio Value", border_style="green"),
            Panel(pnl_panel_text, title="📈 Unrealized P&L", border_style=pnl_border),
            Panel(
                f"[bold white]{len(positions)}[/bold white]\n[dim]Active Holdings[/dim]",
                title="📂 Positions", border_style="dim"
            ),
            Panel(
                f"[bold white]{len(broker_vals)}[/bold white]\n[dim]Accounts Tracked[/dim]",
                title="🏦 Brokers", border_style="dim"
            ),
            Panel(beta_panel_text, title="⚡ Portfolio Beta", border_style=beta_border),
        )
        console.print(metrics_tbl)

        # ── Broker Breakdown (ASCII bar) ──
        max_bv = max(broker_vals.values()) if broker_vals else 1.0
        broker_lines = []
        for bk, bv in sorted(broker_vals.items(), key=lambda x: -x[1]):
            bar = draw_bar(bv, max_bv, 12)
            pct = (bv / total_usd * 100) if total_usd > 0 else 0.0
            broker_lines.append(
                f"[cyan]{bk:<22}[/cyan] [green]{bar}[/green]  "
                f"[bold]${bv:,.0f}[/bold] [dim]({pct:.1f}%)[/dim]"
            )
        broker_panel = Panel(
            "\n".join(broker_lines),
            title="🏦 券商資產分布",
            border_style="cyan"
        )

        # ── P&L Leaderboard (sort by USD-equivalent to handle multi-currency fairly) ──
        pnl_ranked = []
        for p in positions:
            if p.unrealized_pnl is None or p.unrealized_pnl_pct is None:
                continue
            # Convert to USD for comparison; TWD positions divide by rate
            pnl_usd = p.unrealized_pnl if p.currency == "USD" else p.unrealized_pnl / rate
            pnl_ranked.append((p, p.unrealized_pnl, p.unrealized_pnl_pct, pnl_usd))

        pnl_ranked.sort(key=lambda x: x[3], reverse=True)  # sort by USD-equivalent

        perf_lines = []
        if pnl_ranked:
            medals = ["🥇", "🥈", "🥉"]
            perf_lines.append("[bold dim]▲ 最大獲利:[/bold dim]")
            for i, (p, pnl, pct, _) in enumerate(pnl_ranked[:3]):
                color = "green" if pnl >= 0 else "red"
                sign = "+" if pnl >= 0 else ""
                medal = medals[i] if i < len(medals) else "  "
                sym = p.symbol[:12].ljust(12)
                # Show original currency amount with currency tag
                ccy = "" if p.currency == "USD" else f" {p.currency}"
                perf_lines.append(
                    f"{medal} [bold white]{sym}[/bold white] "
                    f"[{color}]{sign}{pnl:,.0f}{ccy} ({sign}{pct:.1f}%)[/{color}]"
                )
            losers = [(p, pnl, pct, pu) for p, pnl, pct, pu in pnl_ranked if pu < 0]
            if losers:
                perf_lines.append("")
                perf_lines.append("[bold dim]▼ 最大虧損:[/bold dim]")
                for p, pnl, pct, _ in losers[-2:]:
                    sym = p.symbol[:12].ljust(12)
                    ccy = "" if p.currency == "USD" else f" {p.currency}"
                    perf_lines.append(
                        f"🔴 [bold white]{sym}[/bold white] "
                        f"[red]{pnl:,.0f}{ccy} ({pct:.1f}%)[/red]"
                    )
        else:
            perf_lines.append("[dim]無損益資料（請填寫平均成本）[/dim]")

        perf_panel = Panel(
            "\n".join(perf_lines),
            title="📊 損益排行",
            border_style="yellow"
        )

        # ── Sector Breakdown (only if data exists) ──
        sector_vals: dict[str, float] = {}
        for p in positions:
            if p.sector:
                val_usd = p.value if p.currency == "USD" else p.value / rate
                sector_vals[p.sector] = sector_vals.get(p.sector, 0.0) + val_usd

        side_panels: list = [broker_panel, perf_panel]
        if sector_vals:
            max_sv = max(sector_vals.values())
            sec_lines = []
            for sec, sv in sorted(sector_vals.items(), key=lambda x: -x[1]):
                bar = draw_bar(sv, max_sv, 8)
                pct = (sv / total_usd * 100) if total_usd > 0 else 0.0
                sec_lines.append(
                    f"[magenta]{sec:<12}[/magenta] [yellow]{bar}[/yellow] [dim]({pct:.1f}%)[/dim]"
                )
            side_panels.append(Panel(
                "\n".join(sec_lines),
                title="🏷️ Sector",
                border_style="magenta"
            ))

        console.print(Columns(side_panels, equal=False, expand=True))

        # ── Holdings Table (grouped by broker) ──
        console.print("[bold]Holdings[/bold]")
        _build_broker_holdings(positions, rate=rate, show_pnl=True, weights=weights)

    # ── Action Menu ──
    menu_text = (
        "[bold cyan]功能選單 (Action Menu):[/bold cyan] "
        "[bold]1[/bold]-部位調整 | "
        "[bold]2[/bold]-立即重整 | "
        "[bold]3[/bold]-安全登出 | "
        "[bold]4[/bold]-績效歷史 | "
        "[bold]5[/bold]-儲存快照"
    )
    console.print(Panel(menu_text, border_style="cyan"))
    console.print("[dim]請輸入編號選擇操作，或等待系統每分鐘自動重整：[/dim] ", end="", highlight=False)


def _prompt_broker_account(
    current_broker: Optional[str] = None,
    current_account: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """
    Single combined prompt for broker + account pair.
    Returns (broker, account).
    Preset pairs:
      1 → firstrade / FT
      2 → ibkr / IBKR
      3 → manual / None
      4 → custom input
    When current_broker/account are provided (edit mode), Enter keeps existing values.
    """
    PRESETS: list[tuple[str, Optional[str], str]] = [
        ("firstrade", "FT",   "Firstrade (FT)"),
        ("ibkr",      "IBKR", "Interactive Brokers (IBKR)"),
        ("manual",    None,   "手動 / 無特定帳戶"),
    ]

    is_edit = current_broker is not None
    current_display = (
        f"{current_broker} ({current_account})" if current_account else current_broker
    ) if is_edit else None

    EMOJI_DIGITS = ["1️⃣", "2️⃣", "3️⃣"]
    lines = []
    for i, (_, _, label) in enumerate(PRESETS):
        lines.append(f"  {EMOJI_DIGITS[i]}  {label}")
    suffix = "  4️⃣  自訂輸入  Enter=保留" if is_edit else "  4️⃣  自訂輸入"
    lines.append(suffix)
    lines.append("  q️⃣  取消並返回")

    if is_edit:
        console.print(f"[dim]券商/帳戶 (Broker/Account)  目前: {current_display}[/dim]")
    else:
        console.print("請選擇券商/帳戶：")
    console.print("\n".join(lines))

    choice = Prompt.ask(
        "請選擇 [1/2/3/4/q" + ("/Enter]" if is_edit else "]"),
        default="" if is_edit else "1",
    ).strip()

    if choice.lower() == "q":
        console.print("[yellow]已取消並返回。[/yellow]")
        raise typer.Exit()

    if is_edit and choice == "":
        # Keep existing
        return (current_broker or "manual", current_account)

    idx = None
    try:
        idx = int(choice) - 1
    except ValueError:
        pass

    if idx is not None and 0 <= idx < len(PRESETS):
        broker, account, _ = PRESETS[idx]
        return (broker, account)

    # Custom input
    custom_broker = Prompt.ask("請輸入券商名稱 (例如 td, webull)").strip().lower() or "manual"
    custom_account = Prompt.ask("請輸入帳戶代號 (例如 MAIN, 留空=無)").strip().upper() or None
    return (custom_broker, custom_account)


def _interactive_add_one(default_broker: Optional[str] = None) -> Position:
    """Helper flow to prompt details of a single position interactively."""
    console.print(f"\n[bold cyan]── 新增持倉部位 ──[/bold cyan]")

    # ── Broker / Account ──
    if default_broker:
        broker = default_broker
        # Infer account from known preset brokers
        account: Optional[str] = {"firstrade": "FT", "ibkr": "IBKR"}.get(default_broker)
    else:
        broker, account = _prompt_broker_account()

    # ── Instrument Type ──
    inst_type = Prompt.ask(
        "持倉類型 (Asset Type)",
        choices=["stock", "etf", "option"],
        default="stock"
    ).strip().lower()

    # ── Market ──
    console.print("請選擇交易市場：\n  1️⃣  US (美股)\n  2️⃣  TW (台股)\n  3️⃣  HK (港股)\n  4️⃣  自訂輸入")
    mkt_choice = Prompt.ask("請選擇 [1/2/3/4]", choices=["1", "2", "3", "4"], default="1")
    if mkt_choice == "1":
        market = "US"
    elif mkt_choice == "2":
        market = "TW"
    elif mkt_choice == "3":
        market = "HK"
    else:
        market = Prompt.ask("請輸入自訂市場代碼").strip().upper() or None

    # ── Exchange ──
    if market == "US":
        exchange_choices = ["NYSE", "NASDAQ", "CBOE", "OTC", "自訂輸入"]
        console.print(f"請選擇交易所：\n  1️⃣  NYSE\n  2️⃣  NASDAQ\n  3️⃣  CBOE\n  4️⃣  OTC\n  5️⃣  自訂輸入")
        exc_choice = Prompt.ask("請選擇 [1/2/3/4/5]", choices=["1","2","3","4","5"], default="2")
        exc_map = {"1": "NYSE", "2": "NASDAQ", "3": "CBOE", "4": "OTC"}
        if exc_choice in exc_map:
            exchange = exc_map[exc_choice]
        else:
            exchange = Prompt.ask("請輸入自訂交易所").strip().upper() or None
    elif market == "TW":
        console.print("請選擇交易所：\n  1️⃣  TSE (上市)\n  2️⃣  OTC (上櫃)\n  3️⃣  自訂輸入")
        exc_choice = Prompt.ask("請選擇 [1/2/3]", choices=["1","2","3"], default="1")
        exchange = {"1": "TSE", "2": "OTC"}.get(exc_choice) or Prompt.ask("請輸入自訂交易所").strip().upper() or None
    elif market == "HK":
        exchange = "HKEX"
    else:
        exchange = Prompt.ask("請輸入交易所代碼 (可留空跳過)", default="").strip().upper() or None

    # ── Currency ──
    currency = Prompt.ask(
        "計價幣別 (Holding Currency)",
        choices=["USD", "TWD", "HKD", "EUR", "JPY"],
        default="USD"
    ).strip().upper()

    # ── Side ──
    side = Prompt.ask(
        "持倉方向 (Side)",
        choices=["long", "short"],
        default="long"
    ).strip().lower()

    # ── Option-specific fields ──
    underlying = None
    expiry = None
    strike = None
    option_type = None
    multiplier = None

    if inst_type == "option":
        underlying = Prompt.ask("請輸入標的股票代碼 (例如 AAPL, TSLA, 台指期=FITX)").strip().upper()

        default_exp = (datetime.today() + timedelta(days=30)).strftime("%Y-%m-%d")
        while True:
            expiry = Prompt.ask("請輸入選擇權到期日 (YYYY-MM-DD)", default=default_exp).strip()
            try:
                datetime.strptime(expiry, "%Y-%m-%d")
                break
            except ValueError:
                console.print("[red]日期格式不正確，請使用 YYYY-MM-DD。[/red]")

        strike = FloatPrompt.ask("請輸入行權價 (Strike Price)")
        option_type_choice = Prompt.ask("請選擇選擇權類型 [1: Call, 2: Put]", choices=["1", "2"], default="1")
        option_type = "call" if option_type_choice == "1" else "put"

        # Multiplier
        default_mult = 50.0 if market == "TW" else 100.0
        multiplier = FloatPrompt.ask(f"合約乘數 (Multiplier，美股選擇權=100，台灣選擇權=50)", default=default_mult)

        # Auto-generate OCC option symbol (US) or manual symbol (TW)
        if market == "US":
            exp_dt = datetime.strptime(expiry, "%Y-%m-%d")
            formatted_date = exp_dt.strftime("%y%m%d")
            formatted_strike = f"{int(strike * 1000):08d}"
            opt_char = "C" if option_type == "call" else "P"
            symbol = f"{underlying}{formatted_date}{opt_char}{formatted_strike}"
            console.print(f"[green]已自動生成選擇權標準 OCC 代碼: {symbol}[/green]")
        else:
            symbol = Prompt.ask(
                f"請輸入合約代碼 (台指期/台選可手動填入，例如 TXFB26, TXOB26C21000)",
                default=f"{underlying}{datetime.strptime(expiry,'%Y-%m-%d').strftime('%y%m%d')}{'C' if option_type=='call' else 'P'}{int(strike):05d}"
            ).strip().upper()
    else:
        while True:
            symbol = Prompt.ask("請輸入商品代碼 (例如 AAPL, NVDA, 2330.TW)").strip().upper()
            if symbol:
                break
            console.print("[red]商品代碼不能為空。[/red]")
        # Auto-append .TW for Taiwan stocks without suffix
        if market == "TW" and not symbol.endswith(".TW") and not symbol.endswith(".TWO"):
            auto_append = Confirm.ask(f"偵測到台股，是否自動為代碼加上 .TW 後綴？ ({symbol} → {symbol}.TW)", default=True)
            if auto_append:
                symbol = symbol + ".TW"

    # ── Quantity & Cost ──
    qty = FloatPrompt.ask("請輸入持有數量 (股數 / 口數，請填正數)")
    cost = FloatPrompt.ask("請輸入平均持倉成本價 (原始幣別計，留 0 表示不填)", default=0.0)

    # ── Cost Currency ──
    cost_currency = None
    if cost > 0:
        cost_currency_input = Prompt.ask(
            f"成本的計價幣別 (Cost Currency，若與持倉幣別 {currency} 相同請直接 Enter)",
            default=currency
        ).strip().upper()
        cost_currency = cost_currency_input if cost_currency_input != currency else None

    quantity = qty if side == "long" else -qty
    avg_cost = cost if cost > 0 else None

    # ── Sector ──
    console.print("請選擇持倉分類 (Sector)：\n  1️⃣  科技 (Technology)\n  2️⃣  半導體 (Semiconductor)\n  3️⃣  金融 (Financial)\n  4️⃣  醫療 (Healthcare)\n  5️⃣  能源 (Energy)\n  6️⃣  消費 (Consumer)\n  7️⃣  ETF\n  8️⃣  自訂輸入\n  9️⃣  略過不填")
    sec_choice = Prompt.ask("請選擇 [1-9]", choices=["1","2","3","4","5","6","7","8","9"], default="9")
    sector_map = {"1":"科技","2":"半導體","3":"金融","4":"醫療","5":"能源","6":"消費","7":"ETF"}
    if sec_choice in sector_map:
        sector = sector_map[sec_choice]
    elif sec_choice == "8":
        sector = Prompt.ask("請輸入自訂分類").strip() or None
    else:
        sector = None

    # ── Notes ──
    notes_input = Prompt.ask("備註 (Notes，可留空)", default="").strip()
    notes = notes_input if notes_input else None

    return Position(
        broker=broker,
        account=account,
        symbol=symbol,
        instrument_type=inst_type,  # type: ignore
        quantity=quantity,
        avg_cost=avg_cost,
        currency=currency,
        market=market,
        exchange=exchange,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,  # type: ignore
        multiplier=multiplier,
        cost_currency=cost_currency,
        sector=sector,
        notes=notes,
        source="interactive",
        last_updated=datetime.utcnow()
    )


@app.command(name="init")
def init_setup(
    ctx: typer.Context,
):
    """初始化並引導手動新增持倉部位。"""
    user = ctx.obj or "default"
    console.print(Panel(
        "[bold cyan]歡迎使用 AssetTrack 投資組合追蹤器！[/bold cyan]\n\n"
        "這是您的首次使用初始化設定流程。\n"
        "本系統支援純手動持倉管理，您可以逐筆輸入各券商的證券與選擇權部位。\n"
        "我們現在將引導您進入手動新增持倉的互動流程。",
        title="✨ 首次啟動引導 (Onboarding Setup) ✨",
        border_style="cyan"
    ))
    # Manual add positions
    add(ctx, broker=None)


@app.command(name="add")
def add(
    ctx: typer.Context,
    broker: Optional[str] = typer.Option(None, "--broker", "-b", help="指定經紀商帳戶"),
):
    """手動新增持倉部位。"""
    user = ctx.obj or "default"
    console.print(Panel.fit(
        "[bold]手動新增持倉流程[/bold]\n"
        "系統將引導您一步步輸入必要資訊。\n"
        "如果不知道平均成本，可以留空，之後損益會顯示「—」。",
        title="Interactive Add",
        border_style="cyan"
    ))
    
    positions = load_manual_positions(user=user)
    
    while True:
        new_pos = _interactive_add_one(broker)
        
        # Check duplicate
        dup = next((p for p in positions if p.broker.lower() == new_pos.broker.lower() and (p.account or "").lower() == (new_pos.account or "").lower() and p.symbol.upper() == new_pos.symbol.upper()), None)
        if dup:
            merge = Confirm.ask("偵測到已有舊持倉設定，是否將新持倉合併（Merge）至現有持倉？ (選否將會完全覆蓋/Replace)", default=True)
            if merge:
                # Weighted average cost merge
                old_qty = dup.quantity
                new_qty = old_qty + new_pos.quantity
                
                # Check weighting cost
                if dup.avg_cost is not None and new_pos.avg_cost is not None:
                    if old_qty > 0 and new_pos.quantity > 0:
                        new_cost = (old_qty * dup.avg_cost + new_pos.quantity * new_pos.avg_cost) / new_qty
                    else:
                        new_cost = new_pos.avg_cost
                else:
                    new_cost = new_pos.avg_cost or dup.avg_cost
                    
                dup.quantity = new_qty
                dup.avg_cost = new_cost
                dup.last_updated = datetime.utcnow()
                console.print(f"[green]已合併至 {new_pos.symbol}。新數量: {new_qty}, 新成本: {new_cost}[/green]")
            else:
                positions.remove(dup)
                positions.append(new_pos)
                console.print(f"[green]已完全覆蓋 {new_pos.symbol} 部位。[/green]")
        else:
            positions.append(new_pos)
            console.print(f"[green]成功新增 {new_pos.symbol}。[/green]")
            
        if not Confirm.ask("是否繼續新增下一筆持倉？", default=True):
            break
            
    save_manual_positions(positions, user=user)
    console.print("[green]持倉設定已成功儲存至系統中！[/green]")


@app.command(name="edit")
def edit(
    ctx: typer.Context,
):
    """互動式修改現有持倉部位（全欄位編輯）。"""
    user = ctx.obj or "default"
    positions = load_manual_positions(user=user)
    if not positions:
        console.print("[yellow]目前沒有任何持倉部位可以修改。[/yellow]")
        return

    console.print(Panel(
        "[bold cyan]修改持倉部位（全欄位編輯模式）[/bold cyan]\n"
        "請從列表中選擇您要修改的部位，系統將顯示所有欄位的現有值，\n"
        "直接按 Enter 保留原值，輸入新值則覆蓋。",
        title="📝 修改持倉 (Modify Position) 📝",
        border_style="cyan"
    ))

    # List positions with indices
    table = Table(box=None, padding=(0, 1, 0, 1))
    table.add_column("編號", style="bold yellow")
    table.add_column("Broker/Account", style="cyan")
    table.add_column("Symbol", style="bold white")
    table.add_column("Type", style="dim")
    table.add_column("Qty", justify="right")
    table.add_column("Avg Cost", justify="right")
    table.add_column("Currency")
    table.add_column("Market")
    table.add_column("Sector")

    for idx, p in enumerate(positions, 1):
        qty_str = f"{p.quantity:,.2f}" if p.quantity % 1 != 0 else f"{int(p.quantity):,}"
        cost_str = f"${p.avg_cost:,.2f}" if p.avg_cost is not None else "—"
        broker_display = f"{p.broker} ({p.account})" if p.account else p.broker
        table.add_row(
            str(idx), broker_display, p.symbol, p.instrument_type,
            qty_str, cost_str, p.currency,
            p.market or "—", p.sector or "—"
        )

    console.print(table)

    while True:
        choice = Prompt.ask("請輸入要修改的部位編號 (或輸入 q 取消)", default="q").strip()
        if choice.lower() == "q":
            return
        try:
            val = int(choice)
            if 1 <= val <= len(positions):
                selected_pos = positions[val - 1]
                break
            console.print(f"[red]請輸入 1 至 {len(positions)} 之間的數字。[/red]")
        except ValueError:
            console.print("[red]請輸入有效的編號。[/red]")

    broker_display = f"{selected_pos.broker} ({selected_pos.account})" if selected_pos.account else selected_pos.broker
    console.print(f"\n[bold cyan]══ 修改部位: {broker_display} - {selected_pos.symbol} ══[/bold cyan]")
    console.print("[dim]直接按 Enter 保留現有值，輸入新值即覆蓋。[/dim]\n")

    # ── Broker / Account ──
    broker, account = _prompt_broker_account(
        current_broker=selected_pos.broker,
        current_account=selected_pos.account,
    )
    selected_pos.broker = broker
    selected_pos.account = account

    # ── Symbol (for non-option; option symbol is regenerated from fields below) ──
    if selected_pos.instrument_type != "option":
        console.print(f"[dim]商品代碼 (Symbol)  目前: {selected_pos.symbol}[/dim]")
        new_sym = Prompt.ask("新代碼 (Enter=保留)", default="").strip().upper()
        if new_sym:
            selected_pos.symbol = new_sym

    # ── Instrument Type ──
    console.print(f"[dim]持倉類型 (Type)  目前: {selected_pos.instrument_type}[/dim]")
    new_type = Prompt.ask("新類型 [stock/etf/option/Enter=保留]", default="").strip().lower()
    if new_type in ["stock", "etf", "option"]:
        selected_pos.instrument_type = new_type  # type: ignore

    # ── Market ──
    console.print(f"[dim]交易市場 (Market)  目前: {selected_pos.market or '未填'}[/dim]")
    console.print("  1️⃣  US  2️⃣  TW  3️⃣  HK  4️⃣  自訂輸入  Enter=保留")
    mkt_choice = Prompt.ask("請選擇 [1/2/3/4/Enter]", default="").strip()
    if mkt_choice == "1":
        selected_pos.market = "US"
    elif mkt_choice == "2":
        selected_pos.market = "TW"
    elif mkt_choice == "3":
        selected_pos.market = "HK"
    elif mkt_choice == "4":
        m = Prompt.ask("請輸入自訂市場代碼").strip().upper()
        if m:
            selected_pos.market = m

    # ── Exchange ──
    console.print(f"[dim]交易所 (Exchange)  目前: {selected_pos.exchange or '未填'}[/dim]")
    new_exc = Prompt.ask("新交易所 (例如 NYSE/NASDAQ/TSE，Enter=保留)", default="").strip().upper()
    if new_exc:
        selected_pos.exchange = new_exc

    # ── Currency ──
    console.print(f"[dim]計價幣別 (Currency)  目前: {selected_pos.currency}[/dim]")
    new_curr = Prompt.ask("新幣別 [USD/TWD/HKD/EUR/JPY/Enter=保留]", default="").strip().upper()
    if new_curr in ["USD", "TWD", "HKD", "EUR", "JPY"]:
        selected_pos.currency = new_curr

    # ── Side & Quantity ──
    side_str = "long" if selected_pos.quantity >= 0 else "short"
    qty_abs = abs(selected_pos.quantity)
    console.print(f"[dim]持倉方向 (Side)  目前: {side_str}[/dim]")
    new_side = Prompt.ask("新方向 [long/short/Enter=保留]", default="").strip().lower()
    if new_side in ["long", "short"]:
        side_str = new_side

    console.print(f"[dim]持有數量 (Quantity)  目前: {qty_abs:g}[/dim]")
    qty_input = Prompt.ask("新數量 (正數，Enter=保留)", default="").strip()
    if qty_input:
        try:
            qty_abs = float(qty_input)
        except ValueError:
            console.print("[yellow]數量格式無效，保留原值。[/yellow]")
    selected_pos.quantity = qty_abs if side_str == "long" else -qty_abs

    # ── Avg Cost ──
    console.print(f"[dim]平均成本 (Avg Cost)  目前: {selected_pos.avg_cost or '未填'}[/dim]")
    cost_input = Prompt.ask("新成本 (0=清除，Enter=保留)", default="").strip()
    if cost_input:
        try:
            c = float(cost_input)
            selected_pos.avg_cost = c if c > 0 else None
        except ValueError:
            console.print("[yellow]成本格式無效，保留原值。[/yellow]")

    # ── Cost Currency ──
    console.print(f"[dim]成本幣別 (Cost Currency)  目前: {selected_pos.cost_currency or '同持倉幣別'}[/dim]")
    new_cc = Prompt.ask("新成本幣別 (Enter=保留，輸入 clear 清除)", default="").strip().upper()
    if new_cc == "CLEAR":
        selected_pos.cost_currency = None
    elif new_cc:
        selected_pos.cost_currency = new_cc

    # ── Option-specific fields ──
    if selected_pos.instrument_type == "option":
        console.print(f"\n[bold cyan]── 選擇權明細 (Option Details) ──[/bold cyan]")

        console.print(f"[dim]標的代碼 (Underlying)  目前: {selected_pos.underlying or '未填'}[/dim]")
        new_und = Prompt.ask("新標的代碼 (Enter=保留)", default="").strip().upper()
        if new_und:
            selected_pos.underlying = new_und

        default_exp = selected_pos.expiry or (datetime.today() + timedelta(days=30)).strftime("%Y-%m-%d")
        console.print(f"[dim]到期日 (Expiry)  目前: {selected_pos.expiry or '未填'}[/dim]")
        while True:
            new_exp = Prompt.ask("新到期日 (YYYY-MM-DD，Enter=保留)", default="").strip()
            if not new_exp:
                break
            try:
                datetime.strptime(new_exp, "%Y-%m-%d")
                selected_pos.expiry = new_exp
                break
            except ValueError:
                console.print("[red]日期格式不正確，請使用 YYYY-MM-DD。[/red]")

        console.print(f"[dim]行權價 (Strike)  目前: {selected_pos.strike or '未填'}[/dim]")
        strike_input = Prompt.ask("新行權價 (Enter=保留)", default="").strip()
        if strike_input:
            try:
                selected_pos.strike = float(strike_input)
            except ValueError:
                console.print("[yellow]行權價格式無效，保留原值。[/yellow]")

        console.print(f"[dim]買賣權 (Option Type)  目前: {selected_pos.option_type or '未填'}[/dim]")
        opt_choice = Prompt.ask("新類型 [1: Call, 2: Put, Enter=保留]", default="").strip()
        if opt_choice == "1":
            selected_pos.option_type = "call"  # type: ignore
        elif opt_choice == "2":
            selected_pos.option_type = "put"  # type: ignore

        console.print(f"[dim]合約乘數 (Multiplier)  目前: {selected_pos.multiplier or '未填'}[/dim]")
        mult_input = Prompt.ask("新乘數 (美股=100, 台灣=50，Enter=保留)", default="").strip()
        if mult_input:
            try:
                selected_pos.multiplier = float(mult_input)
            except ValueError:
                console.print("[yellow]乘數格式無效，保留原值。[/yellow]")

        # Regenerate symbol if underlying/expiry/strike/type all set and market is US
        if all([selected_pos.underlying, selected_pos.expiry, selected_pos.strike, selected_pos.option_type]):
            if (selected_pos.market or "US") == "US":
                exp_dt = datetime.strptime(selected_pos.expiry, "%Y-%m-%d")
                formatted_date = exp_dt.strftime("%y%m%d")
                formatted_strike = f"{int(selected_pos.strike * 1000):08d}"
                opt_char = "C" if selected_pos.option_type == "call" else "P"
                new_symbol = f"{selected_pos.underlying}{formatted_date}{opt_char}{formatted_strike}"
                if new_symbol != selected_pos.symbol:
                    console.print(f"[green]選擇權 OCC 代碼已重新生成: {new_symbol}[/green]")
                    selected_pos.symbol = new_symbol
            else:
                console.print(f"[dim]合約代碼 (Symbol)  目前: {selected_pos.symbol}[/dim]")
                new_sym = Prompt.ask("新合約代碼 (Enter=保留)", default="").strip().upper()
                if new_sym:
                    selected_pos.symbol = new_sym

    # ── Sector ──
    console.print(f"[dim]持倉分類 (Sector)  目前: {selected_pos.sector or '未填'}[/dim]")
    console.print("  1️⃣  科技  2️⃣  半導體  3️⃣  金融  4️⃣  醫療  5️⃣  能源  6️⃣  消費  7️⃣  ETF  8️⃣  自訂  9️⃣  清除  Enter=保留")
    sec_choice = Prompt.ask("請選擇 [1-9/Enter]", default="").strip()
    sector_map = {"1": "科技", "2": "半導體", "3": "金融", "4": "醫療", "5": "能源", "6": "消費", "7": "ETF"}
    if sec_choice in sector_map:
        selected_pos.sector = sector_map[sec_choice]
    elif sec_choice == "8":
        s = Prompt.ask("請輸入自訂分類").strip()
        if s:
            selected_pos.sector = s
    elif sec_choice == "9":
        selected_pos.sector = None

    # ── Notes ──
    console.print(f"[dim]備註 (Notes)  目前: {selected_pos.notes or '未填'}[/dim]")
    notes_input = Prompt.ask("新備註 (Enter=保留，輸入 clear 清除)", default="").strip()
    if notes_input.lower() == "clear":
        selected_pos.notes = None
    elif notes_input:
        selected_pos.notes = notes_input

    # ── Duplicate check after editing ──
    dup = next((
        p for p in positions
        if p is not selected_pos
        and p.broker.lower() == selected_pos.broker.lower()
        and (p.account or "").lower() == (selected_pos.account or "").lower()
        and p.symbol.upper() == selected_pos.symbol.upper()
    ), None)
    if dup:
        merge = Confirm.ask("偵測到目標券商帳戶已有相同標的之持倉，是否將兩筆部位合併？", default=True)
        if merge:
            old_qty = dup.quantity
            new_qty = old_qty + selected_pos.quantity
            if dup.avg_cost is not None and selected_pos.avg_cost is not None:
                if old_qty > 0 and selected_pos.quantity > 0:
                    new_cost = (old_qty * dup.avg_cost + selected_pos.quantity * selected_pos.avg_cost) / new_qty
                else:
                    new_cost = selected_pos.avg_cost
            else:
                new_cost = selected_pos.avg_cost or dup.avg_cost
            dup.quantity = new_qty
            dup.avg_cost = new_cost
            dup.last_updated = datetime.utcnow()
            positions.remove(selected_pos)
            console.print(f"[green]已合併至現有部位。新數量: {new_qty}, 新成本: {new_cost}[/green]")
        else:
            positions.remove(dup)
            console.print("[green]已完全覆蓋舊部位。[/green]")

    selected_pos.last_updated = datetime.utcnow()
    save_manual_positions(positions, user=user)
    console.print("[green]✅ 成功更新部位資料！[/green]")
    time.sleep(1.5)


@app.command(name="remove")
def remove_position(
    ctx: typer.Context,
):
    """互動式移除持倉部位（從持倉清單刪除）。"""
    user = ctx.obj or "default"
    positions = load_manual_positions(user=user)
    if not positions:
        console.print("[yellow]目前沒有任何持倉部位可以移除。[/yellow]")
        return

    console.print(Panel(
        "[bold cyan]移除持倉部位[/bold cyan]\n"
        "請從列表中選擇您要移除的部位。",
        title="🗑️ 移除持倉 (Remove Position) 🗑️",
        border_style="red"
    ))

    table = Table(box=None, padding=(0, 1, 0, 1))
    table.add_column("編號", style="bold yellow")
    table.add_column("Broker/Account", style="cyan")
    table.add_column("Symbol", style="bold white")
    table.add_column("Type", style="dim")
    table.add_column("Qty", justify="right")
    table.add_column("Avg Cost", justify="right")

    for idx, p in enumerate(positions, 1):
        qty_str = f"{p.quantity:,.2f}" if p.quantity % 1 != 0 else f"{int(p.quantity):,}"
        cost_str = f"${p.avg_cost:,.2f}" if p.avg_cost is not None else "—"
        broker_display = f"{p.broker} ({p.account})" if p.account else p.broker
        table.add_row(str(idx), broker_display, p.symbol, p.instrument_type, qty_str, cost_str)

    console.print(table)

    while True:
        choice = Prompt.ask("請輸入要移除的部位編號 (或輸入 q 取消)", default="q").strip()
        if choice.lower() == "q":
            return
        try:
            val = int(choice)
            if 1 <= val <= len(positions):
                selected_pos = positions[val - 1]
                break
            console.print(f"[red]請輸入 1 至 {len(positions)} 之間的數字。[/red]")
        except ValueError:
            console.print("[red]請輸入有效的編號。[/red]")

    broker_display = f"{selected_pos.broker} ({selected_pos.account})" if selected_pos.account else selected_pos.broker
    confirmed = Confirm.ask(
        f"⚠️ 確定要從持倉中移除 [bold red]{broker_display} - {selected_pos.symbol}[/bold red]？",
        default=False
    )
    if confirmed:
        positions.remove(selected_pos)
        save_manual_positions(positions, user=user)
        console.print(f"[green]✅ 已成功移除 {selected_pos.symbol} 部位。[/green]")
    else:
        console.print("[dim]已取消，部位未變動。[/dim]")
    time.sleep(1.5)


@app.command(name="log-trade")
def log_trade(
    ctx: typer.Context,
    broker: Optional[str] = typer.Option(None, "--broker", "-b", help="經紀商 (manual, ibkr, firstrade)"),
    symbol: Optional[str] = typer.Option(None, "--symbol", "-s", help="商品代碼 (例如 AAPL, TSLA)"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="券商帳戶 (例如 FT, IBKR)"),
):
    """手動登錄交易日誌，並連動更新現有持倉部位與計算實現損益。"""
    user = ctx.obj or "default"
    storage = Storage(user=user)
    
    console.print(Panel(
        "[bold cyan]交易日誌登錄系統[/bold cyan]\n"
        "登錄買入、賣出或平倉交易，系統會自動更新持倉並計算已實現損益。",
        title="📝 登錄交易 (Log Trade) 📝",
        border_style="cyan"
    ))
    
    if not broker:
        broker = Prompt.ask("請選擇經紀商 / 管道", choices=["manual", "firstrade", "ibkr"], default="manual")
    if broker == "manual" and not account:
        console.print("請選擇此持倉之券商帳戶/部位 (Account)：\n  1️⃣  FT (Firstrade)\n  2️⃣  IBKR (Interactive Brokers)\n  3️⃣  manual (無特定帳戶)\n  4️⃣  自訂輸入")
        acc_choice = Prompt.ask("請選擇 [1/2/3/4]", choices=["1", "2", "3", "4"], default="3")
        if acc_choice == "1":
            account = "FT"
        elif acc_choice == "2":
            account = "IBKR"
        elif acc_choice == "3":
            account = None
        else:
            account = Prompt.ask("請輸入自訂券商部位/帳戶").strip().upper()
            if not account:
                account = None

    if not symbol:
        symbol = Prompt.ask("請輸入商品代碼 (Symbol)").strip().upper()
        
    action = Prompt.ask("交易動作 (Action)", choices=["BUY", "SELL"]).upper()
    quantity = FloatPrompt.ask("交易數量 (Quantity)")
    price = FloatPrompt.ask("交易價格 (Price)")
    currency = Prompt.ask("計價貨幣 (Currency)", choices=["USD", "TWD"], default="USD")
    
    positions = load_manual_positions(user=user)
    existing_pos = next((p for p in positions if p.broker.lower() == broker.lower() and (p.account or "").lower() == (account or "").lower() and p.symbol.upper() == symbol.upper()), None)
    
    realized_pnl = 0.0
    
    if action == "BUY":
        if existing_pos:
            old_qty = existing_pos.quantity
            if old_qty >= 0:
                # Adding to long
                new_qty = old_qty + quantity
                old_avg = existing_pos.avg_cost or price
                new_avg = (old_qty * old_avg + quantity * price) / new_qty
                existing_pos.quantity = new_qty
                existing_pos.avg_cost = new_avg
            else:
                # Covering short
                cover_qty = min(quantity, abs(old_qty))
                realized_pnl = (existing_pos.avg_cost - price) * cover_qty
                new_qty = old_qty + quantity
                existing_pos.quantity = new_qty
                if new_qty == 0:
                    positions.remove(existing_pos)
                elif new_qty > 0:
                    existing_pos.avg_cost = price
            existing_pos.last_updated = datetime.utcnow()
        else:
            # New Long
            inst_type = "stock"
            if len(symbol) > 9 and any(c in symbol for c in ["C", "P"]):
                if re.match(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", symbol):
                    inst_type = "option"
            new_pos = Position(
                broker=broker,
                account=account,
                symbol=symbol,
                quantity=quantity,
                avg_cost=price,
                currency=currency,
                instrument_type=inst_type,  # type: ignore
                source="trade-log",
                last_updated=datetime.utcnow()
            )
            positions.append(new_pos)
    else:
        # SELL
        if existing_pos:
            old_qty = existing_pos.quantity
            if old_qty > 0:
                # Selling long
                sell_qty = min(quantity, old_qty)
                realized_pnl = (price - (existing_pos.avg_cost or price)) * sell_qty
                new_qty = old_qty - quantity
                existing_pos.quantity = new_qty
                if new_qty == 0:
                    positions.remove(existing_pos)
                elif new_qty < 0:
                    existing_pos.avg_cost = price
            else:
                # Selling to short
                new_qty = old_qty - quantity
                old_avg = existing_pos.avg_cost or price
                new_avg = (abs(old_qty) * old_avg + quantity * price) / abs(new_qty)
                existing_pos.quantity = new_qty
                existing_pos.avg_cost = new_avg
            existing_pos.last_updated = datetime.utcnow()
        else:
            # New Short
            inst_type = "stock"
            if len(symbol) > 9 and any(c in symbol for c in ["C", "P"]):
                if re.match(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", symbol):
                    inst_type = "option"
            new_pos = Position(
                broker=broker,
                account=account,
                symbol=symbol,
                quantity=-quantity,
                avg_cost=price,
                currency=currency,
                instrument_type=inst_type,  # type: ignore
                source="trade-log",
                last_updated=datetime.utcnow()
            )
            positions.append(new_pos)
            
    save_manual_positions(positions, user=user)
    
    # Save transaction history
    storage.save_transaction(
        timestamp=datetime.utcnow(),
        broker=broker,
        symbol=symbol,
        action=action.lower(),
        quantity=quantity,
        price=price,
        currency=currency,
        realized_pnl=realized_pnl,
        notes="logged from cli"
    )
    
    console.print(Panel.fit(
        f"[green]交易登錄成功！已實現損益: ${realized_pnl:,.2f} USD[/green]\n"
        f"持倉部位已連動更新並存檔。",
        title="交易成功",
        border_style="green"
    ))
    time.sleep(1.5)


@app.command(name="perf")
def perf(
    ctx: typer.Context,
    currency: str = typer.Option("USD", "--currency", "-c", help="基準貨幣 (USD, TWD)"),
):
    """分析投資組合之績效指標與各標的佔比、未實現損益排行榜。"""
    user = ctx.obj or "default"
    positions = load_manual_positions(user=user)
    if not positions:
        console.print("[yellow]沒有持倉部位，無法進行績效分析。[/yellow]")
        return
        
    rate = fetch_usdtwd_rate()
    with console.status("[cyan]正在載入最新行情進行分析...[/cyan]"):
        enriched = enrich_positions_with_quotes(positions, delay=0.1)
        
    console.print(Panel(
        f"📊 [bold cyan]投資組合績效分析 ({currency.upper()})[/bold cyan]\n"
        f"基準匯率 USDTWD = {rate:.2f}",
        title="Performance Analytics",
        border_style="cyan"
    ))
    
    total_val = 0.0
    total_cost = 0.0
    has_cost = False
    
    df_rows = []
    for p in enriched:
        val_converted = p.value if p.currency == currency else (p.value * rate if currency == "TWD" else p.value / rate)
        total_val += val_converted
        
        cost_converted = None
        if p.total_cost is not None:
            cost_converted = p.total_cost if p.currency == currency else (p.total_cost * rate if currency == "TWD" else p.total_cost / rate)
            total_cost += cost_converted
            has_cost = True
            
        pnl = val_converted - cost_converted if cost_converted is not None else None
        pct = (pnl / cost_converted * 100) if (cost_converted and cost_converted != 0) else None
        
        df_rows.append({
            "symbol": p.symbol,
            "broker": p.broker,
            "value": val_converted,
            "pnl": pnl,
            "pct": pct
        })
        
    table = Table(title="持倉佔比與損益排行 (Holdings Allocation & P&L)")
    table.add_column("Symbol", style="bold")
    table.add_column("Broker", style="cyan")
    table.add_column("Market Value", justify="right")
    table.add_column("Weight", justify="right")
    table.add_column("PnL", justify="right")
    table.add_column("PnL %", justify="right")
    
    df_rows.sort(key=lambda x: x["value"], reverse=True)
    
    for r in df_rows:
        weight = (r["value"] / total_val * 100) if total_val > 0 else 0.0
        val_str = f"${r['value']:,.2f}" if currency == "USD" else f"NT${r['value']:,.2f}"
        weight_str = f"{weight:.2f}%"
        
        pnl_str = "—"
        pct_str = "—"
        if r["pnl"] is not None:
            color = "green" if r["pnl"] >= 0 else "red"
            sign = "+" if r["pnl"] >= 0 else ""
            pnl_val_str = f"${r['pnl']:,.2f}" if currency == "USD" else f"NT${r['pnl']:,.2f}"
            pnl_str = f"[{color}]{sign}{pnl_val_str}[/{color}]"
            pct_str = f"[{color}]{sign}{r['pct']:.2f}%[/{color}]"
            
        table.add_row(r["symbol"], r["broker"], val_str, weight_str, pnl_str, pct_str)
        
    console.print(table)
    
    pnl_total_str = "—"
    if has_cost:
        total_pnl = total_val - total_cost
        total_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
        color = "green" if total_pnl >= 0 else "red"
        sign = "+" if total_pnl >= 0 else ""
        pnl_val_str = f"${total_pnl:,.2f}" if currency == "USD" else f"NT${total_pnl:,.2f}"
        pnl_total_str = f"[{color}]{sign}{pnl_val_str} ({sign}{total_pct:.2f}%)[/{color}]"
        
    summary_text = (
        f"總資產價值: [bold green]{f'${total_val:,.2f}' if currency == 'USD' else f'NT${total_val:,.2f}'}[/bold green]\n"
        f"總未實現損益: {pnl_total_str}"
    )
    console.print(Panel(summary_text, title="Summary", border_style="green"))


@app.command(name="value")
def value_command(
    ctx: typer.Context,
    currency: str = typer.Option("USD", "--currency", "-c", help="基準貨幣 (USD, TWD)"),
    refresh: bool = typer.Option(False, "--refresh", "-r", help="是否立即更新行情報價"),
    live_ibkr: bool = typer.Option(False, "--live-ibkr", help="是否連接 live IBKR API"),
):
    """查閱目前持有部位市值、佔比與損益排行榜。"""
    user = ctx.obj or "default"
    positions = load_manual_positions(user=user)
    if not positions:
        console.print("[yellow]沒有持倉部位。[/yellow]")
        return
        
    rate = fetch_usdtwd_rate()
    if refresh:
        with console.status("[cyan]正在載入最新市場報價...[/cyan]"):
            positions = enrich_positions_with_quotes(positions, delay=0.1)
            
    console.print(Panel(
        f"📊 [bold cyan]資產持倉明細 ({currency.upper()})[/bold cyan]\n"
        f"計價匯率 USDTWD = {rate:.2f}",
        title="Asset Holdings",
        border_style="cyan"
    ))
    
    converted_positions = []
    total_val = 0.0
    for p in positions:
        p_copy = p.model_copy(deep=True)
        if p.currency != currency:
            r = rate if currency == "TWD" else 1.0 / rate
            if p.avg_cost is not None:
                p_copy.avg_cost = p.avg_cost * r
            if p_copy.market_price is not None:
                p_copy.market_price = p_copy.market_price * r
            if p_copy.market_value is not None:
                p_copy.market_value = p_copy.market_value * r
            p_copy.currency = currency
        converted_positions.append(p_copy)
        total_val += p_copy.value
        
    console.print(_build_positions_table(converted_positions, show_pnl=True))
    
    val_str = f"${total_val:,.2f} USD" if currency == "USD" else f"NT${total_val:,.2f} TWD"
    console.print(f"\n總持倉市值: [bold green]{val_str}[/bold green]")



@app.command(name="refresh")
def refresh_snapshot(
    ctx: typer.Context,
    save: bool = typer.Option(False, "--save", help="是否儲存市值快照至歷史庫"),
):
    """更新當前持倉報價，並可選擇將當前市值快照存檔。"""
    user = ctx.obj or "default"
    positions = load_manual_positions(user=user)
    if not positions:
        console.print("[yellow]沒有持倉部位可以重整。[/yellow]")
        return
        
    rate = fetch_usdtwd_rate()
    with console.status("[cyan]正在載入最新行情價格...[/cyan]"):
        enriched = enrich_positions_with_quotes(positions, delay=0.1)
        
    total_val = current_portfolio_value(enriched)
    console.print(f"[green]已更新最新報價！總資產市值: ${total_val:,.2f} USD (USDTWD: {rate:.2f})[/green]")
    
    if save:
        if not Confirm.ask("確定要儲存目前的市值快照至歷史庫？", default=True):
            console.print("[yellow]已取消儲存快照。[/yellow]")
            return
        storage = Storage(user=user)
        by_broker = {}
        for p in enriched:
            by_broker[p.broker] = by_broker.get(p.broker, 0.0) + p.value
        snap = PortfolioSnapshot(
            timestamp=datetime.utcnow(),
            total_value=total_val,
            by_broker=by_broker,
            positions=enriched,
            notes="cli_refresh"
        )
        storage.save_snapshot(snap)
        console.print("[green]成功儲存資產快照至歷史庫！[/green]")


def draw_ascii_chart(values: list[float], dates: list[datetime], width: int = 50, height: int = 12) -> str:
    """Generate an ASCII line graph of asset values over time."""
    if not values:
        return "無歷史資料"
    n = len(values)
    if n < 2:
        return f"單一快照節點價值: {values[0]:,.2f}"

    min_v = min(values)
    max_v = max(values)
    range_v = max_v - min_v if max_v != min_v else 1.0

    grid = [[" " for _ in range(width)] for _ in range(height)]

    for i in range(width):
        idx = int(i * (n - 1) / (width - 1))
        val = values[idx]
        y = int((height - 1) * (max_v - val) / range_v)
        grid[y][i] = "•"

    for i in range(width - 1):
        y1 = int((height - 1) * (max_v - values[int(i * (n - 1) / (width - 1))]) / range_v)
        y2 = int((height - 1) * (max_v - values[int((i + 1) * (n - 1) / (width - 1))]) / range_v)
        step_y = 1 if y2 > y1 else -1
        for y_mid in range(y1 + step_y, y2, step_y):
            if 0 <= y_mid < height:
                grid[y_mid][i] = "│"

    lines = []
    for y in range(height):
        val_at_y = max_v - (y * range_v / (height - 1))
        y_label = f"{val_at_y:,.2f}"
        lines.append(f"{y_label:>15} │ " + "".join(grid[y]))

    x_line = " " * 15 + " └" + "─" * width
    lines.append(x_line)

    d_label = " " * 16 + dates[0].strftime("%m/%d") + " " * (width - 10) + dates[-1].strftime("%m/%d")
    lines.append(d_label)
    return "\n".join(lines)


def draw_ascii_chart_with_benchmark(
    values: list[float],
    dates: list[datetime],
    bm_vals: Optional[list[Optional[float]]] = None,
    bm_label: str = "BM",
    width: int = 52,
    height: int = 14,
) -> str:
    """
    Generate an ASCII dual-line chart overlaying portfolio (•) and benchmark (○).
    Shared y-axis spans the combined min/max of both series.
    """
    if not values:
        return "無歷史資料"
    n = len(values)
    if n < 2:
        return f"單一快照節點價值: {values[0]:,.2f}"

    # Combine ranges
    all_vals = list(values)
    if bm_vals:
        all_vals += [v for v in bm_vals if v is not None]
    min_v = min(all_vals)
    max_v = max(all_vals)
    range_v = max_v - min_v if max_v != min_v else 1.0

    def to_y(val: float) -> int:
        return int((height - 1) * (max_v - val) / range_v)

    grid = [[" " for _ in range(width)] for _ in range(height)]

    # ── Portfolio line (•, │) ──
    for i in range(width):
        idx = int(i * (n - 1) / (width - 1))
        y = to_y(values[idx])
        if grid[y][i] == " ":
            grid[y][i] = "•"
    for i in range(width - 1):
        y1 = to_y(values[int(i * (n - 1) / (width - 1))])
        y2 = to_y(values[int((i + 1) * (n - 1) / (width - 1))])
        step_y = 1 if y2 > y1 else -1
        for y_mid in range(y1 + step_y, y2, step_y):
            if 0 <= y_mid < height and grid[y_mid][i] == " ":
                grid[y_mid][i] = "│"

    # ── Benchmark line (○, ¦) ──
    if bm_vals and len(bm_vals) == n:
        # Interpolate benchmark, skipping None slots
        def interp_bm(i: int) -> Optional[float]:
            idx = int(i * (n - 1) / (width - 1))
            # find nearest non-None
            lo, hi = idx, idx
            while lo >= 0 or hi < n:
                if lo >= 0 and bm_vals[lo] is not None:
                    return bm_vals[lo]
                if hi < n and bm_vals[hi] is not None:
                    return bm_vals[hi]
                lo -= 1
                hi += 1
            return None

        bm_screen = [interp_bm(i) for i in range(width)]
        for i in range(width):
            v = bm_screen[i]
            if v is not None:
                y = to_y(v)
                if grid[y][i] == " ":
                    grid[y][i] = "○"
                elif grid[y][i] == "•":
                    grid[y][i] = "⊗"  # overlap marker
        for i in range(width - 1):
            v1, v2 = bm_screen[i], bm_screen[i + 1]
            if v1 is not None and v2 is not None:
                y1, y2 = to_y(v1), to_y(v2)
                step_y = 1 if y2 > y1 else -1
                for y_mid in range(y1 + step_y, y2, step_y):
                    if 0 <= y_mid < height and grid[y_mid][i] == " ":
                        grid[y_mid][i] = "¦"

    lines = []
    for y in range(height):
        val_at_y = max_v - (y * range_v / (height - 1))
        y_label = f"{val_at_y:,.2f}"
        lines.append(f"{y_label:>15} │ " + "".join(grid[y]))

    x_line = " " * 15 + " └" + "─" * width
    lines.append(x_line)
    d_label = " " * 16 + dates[0].strftime("%m/%d") + " " * (width - 10) + dates[-1].strftime("%m/%d")
    lines.append(d_label)

    # Legend
    legend = f"  [dim]• 組合淨值   ○ {bm_label} 基準   ⊗ 重疊[/dim]" if bm_vals else ""
    if legend:
        lines.append(legend)

    return "\n".join(lines)



def get_upcoming_macro_events(days: int = 90, start_days_ago: int = 0) -> "list[tuple]":
    """
    Returns upcoming macro events within the next ~days days as (date, label) tuples.
    Hardcoded 2025-2027 schedule. Sorted ascending.
    """
    from datetime import date as date_type, timedelta

    # FED FOMC meeting dates (decision day)
    fed_dates = [
        date_type(2025, 7, 30),
        date_type(2025, 9, 17),
        date_type(2025, 10, 29),
        date_type(2025, 12, 10),
        date_type(2026, 1, 28),
        date_type(2026, 3, 18),
        date_type(2026, 4, 29),
        date_type(2026, 6, 17),
        date_type(2026, 7, 29),
        date_type(2026, 9, 16),
        date_type(2026, 11, 5),
        date_type(2026, 12, 16),
        date_type(2027, 1, 27),
        date_type(2027, 3, 17),
        date_type(2027, 4, 28),
        date_type(2027, 6, 16),
        date_type(2027, 7, 28),
        date_type(2027, 9, 22),
        date_type(2027, 11, 3),
        date_type(2027, 12, 15),
    ]

    # Non-Farm Payroll (NFP) — first Friday of each month
    nfp_dates = [
        date_type(2025, 7, 4),
        date_type(2025, 8, 1),
        date_type(2025, 9, 5),
        date_type(2025, 10, 3),
        date_type(2025, 11, 7),
        date_type(2025, 12, 5),
        date_type(2026, 1, 9),
        date_type(2026, 2, 6),
        date_type(2026, 3, 6),
        date_type(2026, 4, 3),
        date_type(2026, 5, 1),
        date_type(2026, 6, 5),
        date_type(2026, 7, 10),
        date_type(2026, 8, 7),
        date_type(2026, 9, 4),
        date_type(2026, 10, 2),
        date_type(2026, 11, 6),
        date_type(2026, 12, 4),
        date_type(2027, 1, 8),
        date_type(2027, 2, 5),
        date_type(2027, 3, 5),
        date_type(2027, 4, 2),
        date_type(2027, 5, 7),
        date_type(2027, 6, 4),
        date_type(2027, 7, 2),
        date_type(2027, 8, 6),
        date_type(2027, 9, 3),
        date_type(2027, 10, 8),
        date_type(2027, 11, 5),
        date_type(2027, 12, 3),
    ]

    # CPI release dates (approx mid-month)
    cpi_dates = [
        date_type(2025, 7, 15),
        date_type(2025, 8, 12),
        date_type(2025, 9, 10),
        date_type(2025, 10, 15),
        date_type(2025, 11, 13),
        date_type(2025, 12, 10),
        date_type(2026, 1, 14),
        date_type(2026, 2, 11),
        date_type(2026, 3, 11),
        date_type(2026, 4, 10),
        date_type(2026, 5, 13),
        date_type(2026, 6, 10),
        date_type(2026, 7, 14),
        date_type(2026, 8, 12),
        date_type(2026, 9, 11),
        date_type(2026, 10, 14),
        date_type(2026, 11, 12),
        date_type(2026, 12, 11),
        date_type(2027, 1, 13),
        date_type(2027, 2, 10),
        date_type(2027, 3, 12),
        date_type(2027, 4, 13),
        date_type(2027, 5, 12),
        date_type(2027, 6, 15),
        date_type(2027, 7, 13),
        date_type(2027, 8, 11),
        date_type(2027, 9, 14),
        date_type(2027, 10, 13),
        date_type(2027, 11, 10),
        date_type(2027, 12, 10),
    ]

    today = datetime.utcnow().date()
    start_date = today - timedelta(days=start_days_ago)
    cutoff = today + timedelta(days=days)

    events: list[tuple] = []
    import zoneinfo
    from datetime import datetime as dt_cls, time as time_cls, timezone as tz_cls

    tz_et = zoneinfo.ZoneInfo("America/New_York")
    tz_gmt8 = tz_cls(timedelta(hours=8))

    def to_gmt8(d, time_et):
        dt_et = dt_cls.combine(d, time_et).replace(tzinfo=tz_et)
        dt_local = dt_et.astimezone(tz_gmt8)
        return dt_local.date(), dt_local.strftime("%H:%M")

    for d in fed_dates:
        local_d, local_t = to_gmt8(d, time_cls(14, 0))
        if start_date <= local_d <= cutoff:
            events.append((local_d, "▼FED", local_t))
    for d in nfp_dates:
        local_d, local_t = to_gmt8(d, time_cls(8, 30))
        if start_date <= local_d <= cutoff:
            events.append((local_d, "★NFP", local_t))
    for d in cpi_dates:
        local_d, local_t = to_gmt8(d, time_cls(8, 30))
        if start_date <= local_d <= cutoff:
            events.append((local_d, "◆CPI", local_t))

    events.sort(key=lambda x: x[0])
    return events


def draw_history_chart(
    week_dates: "list",
    port_vals: "list[float]",
    bm_vals: "Optional[list[Optional[float]]]",
    broker_weekly: "Optional[dict[str, list[float]]]",
    bm_label: str = "SPY",
    width: int = 70,
    height: int = 16,
) -> str:
    """
    Draw a combined bar (portfolio, per-broker stacked) + line (benchmark) ASCII chart.
    Y-axis represents absolute USD values.

    Portfolio bars:   ▓ / █ / ▒  (stacked by broker, from bottom of grid)
    Benchmark line:   ○──○        (overlaid on bars)
    Start baseline:   ╌╌╌╌        (at portfolio start value)
    """
    n = len(week_dates)
    if n == 0:
        return "[yellow]無歷史資料可繪製[/yellow]"
    if n == 1:
        return f"[yellow]只有一個週節點 (${port_vals[0]:,.0f})，需至少兩個節點才能繪製趨勢。[/yellow]"

    # ── Establish Y-axis range ──
    all_vals = list(port_vals)
    if bm_vals:
        all_vals += [v for v in bm_vals if v is not None]
    
    min_v = min(all_vals)
    max_v = max(all_vals)
    
    if max_v == min_v:
        y_pad = max(1.0, min_v * 0.1)
    else:
        y_pad = (max_v - min_v) * 0.15
        
    y_min = max(0.0, min_v - y_pad)
    y_max = max_v + y_pad
    y_range = y_max - y_min if y_max != y_min else 1.0

    def to_row(val: float) -> int:
        return int((height - 1) * (y_max - val) / y_range)

    # ── Grid dimensions ──
    label_w = 11  # e.g., "$1,234,567"
    chart_w = width - label_w - 3  # "│ " prefix + 1 margin
    col_w = max(3, chart_w // n)
    actual_w = col_w * n

    grid = [[" " for _ in range(actual_w)] for _ in range(height)]

    # ── Start value baseline ──
    start_row = to_row(port_vals[0])
    if 0 <= start_row < height:
        for x in range(actual_w):
            grid[start_row][x] = "╌"

    # ── Broker setup ──
    broker_chars = ["█", "▓", "▒", "░", "▐", "▌"]
    broker_names_list = list(broker_weekly.keys()) if broker_weekly else []
    broker_color_map = {
        b: broker_chars[i % len(broker_chars)]
        for i, b in enumerate(broker_names_list)
    }

    # ── Draw portfolio bars (absolute USD from bottom of chart) ──
    bar_margin = max(1, col_w // 5)  # gap between adjacent bars
    for wi in range(n):
        val = port_vals[wi]
        x_lo = wi * col_w + bar_margin
        x_hi = (wi + 1) * col_w - bar_margin - 1
        x_lo = max(0, x_lo)
        x_hi = min(actual_w - 1, x_hi)

        row_top = to_row(val)
        row_top = max(0, min(height - 1, row_top))
        row_bottom = height - 1

        bar_height = row_bottom - row_top + 1
        if bar_height > 0:
            if broker_weekly and broker_names_list:
                # Fill each row proportionally by broker
                total_pv = port_vals[wi]
                broker_fracs = []
                for bname in broker_names_list:
                    bv_list = broker_weekly.get(bname, [])
                    bv = bv_list[wi] if wi < len(bv_list) else 0.0
                    frac = (bv / total_pv) if total_pv > 0 else 0.0
                    broker_fracs.append((bname, frac))

                row_cursor = row_bottom
                for idx, (bname, frac) in enumerate(broker_fracs):
                    if idx == len(broker_fracs) - 1:
                        n_rows = row_cursor - row_top + 1
                    else:
                        n_rows = round(frac * bar_height)
                    
                    n_rows = max(0, n_rows)
                    bc = broker_color_map.get(bname, "█")
                    for dy in range(n_rows):
                        row = row_cursor - dy
                        if row_top <= row <= row_bottom and 0 <= row < height:
                            for x in range(x_lo, x_hi + 1):
                                grid[row][x] = bc
                    row_cursor -= n_rows
            else:
                # Single color bar
                bc = "█"
                for y in range(row_top, row_bottom + 1):
                    for x in range(x_lo, x_hi + 1):
                        grid[y][x] = bc

    # ── Draw benchmark LINE (on top of bars) ──
    has_bm = False
    bm_points = []
    if bm_vals and any(v is not None for v in bm_vals):
        for wi in range(n):
            v = bm_vals[wi]
            if v is not None:
                bx = wi * col_w + col_w // 2
                by = to_row(v)
                by = max(0, min(height - 1, by))
                bm_points.append((bx, by))
        has_bm = len(bm_points) > 0

    if has_bm:
        # Connect with ─
        for i in range(len(bm_points) - 1):
            x1, y1 = bm_points[i]
            x2, y2 = bm_points[i + 1]
            dx = x2 - x1
            dy = y2 - y1
            steps = max(abs(dx), abs(dy), 1)
            for step in range(1, steps):
                ix = x1 + int(step * dx / steps)
                iy = y1 + int(step * dy / steps)
                if 0 <= iy < height and 0 <= ix < actual_w:
                    ch = grid[iy][ix]
                    if ch in (" ", "╌"):
                        grid[iy][ix] = "─"
                    elif ch in ("█", "▓", "▒", "░", "▐", "▌"):
                        grid[iy][ix] = "┼"  # line passing through bar

        # Draw circles
        for bx, by in bm_points:
            if 0 <= by < height and 0 <= bx < actual_w:
                grid[by][bx] = "○"

    # ── Render Y-axis labels ──
    lines = []
    for row in range(height):
        val_at_row = y_max - (row * y_range / (height - 1))
        y_lbl = f"${val_at_row:>9,.0f}"
        sep = "┤" if row == start_row else "│"
        lines.append(f"[dim]{y_lbl}[/dim] {sep} " + "".join(grid[row]))

    # ── X-axis ──
    lines.append(" " * (label_w + 1) + " └" + "─" * actual_w)

    # ── Date labels ──
    date_row = [" "] * (label_w + 3 + actual_w)
    offset = label_w + 3
    for wi, d in enumerate(week_dates):
        lbl = d.strftime("%m/%d")
        x = offset + wi * col_w + col_w // 2 - 2
        for ci, ch in enumerate(lbl):
            pos = x + ci
            if 0 <= pos < len(date_row):
                date_row[pos] = ch
    lines.append("".join(date_row))

    # ── Legend ──
    port_final = port_vals[-1]
    p0 = port_vals[0]
    port_pct_change = (port_final / p0 - 1.0) * 100.0 if p0 > 0 else 0.0
    port_sign = "+" if port_pct_change >= 0 else ""
    port_color = "green" if port_pct_change >= 0 else "red"
    legend = f"  [bold {port_color}]█ 組合  ${port_final:,.0f} ({port_sign}{port_pct_change:.2f}%)[/bold {port_color}]"

    if has_bm:
        bm_final = next((v for v in reversed(bm_vals) if v is not None), None)
        bm0 = next((v for v in bm_vals if v is not None), None)
        if bm_final is not None and bm0 is not None:
            bm_pct_change = (bm_final / bm0 - 1.0) * 100.0 if bm0 > 0 else 0.0
            bm_sign = "+" if bm_pct_change >= 0 else ""
            bm_color = "green" if bm_pct_change >= 0 else "red"
            alpha = port_pct_change - bm_pct_change
            al_sign = "+" if alpha >= 0 else ""
            al_color = "green" if alpha >= 0 else "red"
            al_arr = "▲" if alpha >= 0 else "▼"
            legend += (
                f"    [dim {bm_color}]○── {bm_label}  ${bm_final:,.0f} ({bm_sign}{bm_pct_change:.2f}%)[/dim {bm_color}]"
                f"    [{al_color} bold]Alpha {al_arr} {al_sign}{alpha:.2f}%[/{al_color} bold]"
            )
    lines.append(legend)
    lines.append("")

    # ── Broker proportion bar ──
    if broker_weekly and broker_names_list:
        last_broker_vals = {b: (broker_weekly[b][-1] if broker_weekly[b] else 0.0)
                            for b in broker_names_list}
        total_last = sum(last_broker_vals.values())
        if total_last > 0:
            bar_w = 28
            bar_chars_list = []
            legend_parts = []
            for i, bname in enumerate(broker_names_list):
                bv = last_broker_vals.get(bname, 0.0)
                frac = bv / total_last
                bc = broker_chars[i % len(broker_chars)]
                n_c = max(1, round(frac * bar_w))
                bar_chars_list += [bc] * n_c
                legend_parts.append(f"{bc} {bname} {frac * 100:.0f}%")
            bar_str = "".join(bar_chars_list[:bar_w])
            lines.append(
                f"  [dim]券商分佈 [{bar_str}]  " + "  ".join(legend_parts) + "[/dim]"
            )

    return "\n".join(lines)


@app.command(name="history")
def history(
    ctx: typer.Context,
    currency: str = typer.Option("USD", "--currency", "-c", help="基準計價貨幣 (USD, TWD)"),
    days: int = typer.Option(60, "--days", "-d", help="查看過去幾天 (60/180/YTD)"),
    benchmark: str = typer.Option("SPY", "--benchmark", "-b", help="對比指數 (SPY/QQQ/^GSPC，輸入 none 停用)"),
):
    """以當下持倉部位回推歷史績效，與大盤指數比較。"""
    from datetime import date as date_type

    user = ctx.obj or "default"
    positions = load_manual_positions(user=user)

    # ── 前置條件 1：必須有持倉 ──
    if not positions:
        console.print(Panel(
            "[yellow]⚠️  尚未建立任何持倉部位。\n"
            "請先透過功能選單 [bold]選項 1[/bold] 新增持倉後再查看績效歷史。[/yellow]",
            title="績效歷史", border_style="yellow"
        ))
        return

    # ── 前置條件 2：篩選可回測的部位（排除 Options）──
    tradeable = [
        p for p in positions
        if p.instrument_type in ("stock", "etf", "other")
        and p.quantity and p.quantity > 0
        and p.currency.upper() == "USD"   # 本版只支援 USD 計價
    ]
    options_excluded = [p for p in positions if p.instrument_type == "option"]
    tw_excluded = [p for p in positions if p.currency.upper() != "USD" and p.instrument_type != "option"]

    if len(tradeable) < 1:
        console.print(Panel(
            "[red]❌  找不到可回測的 USD 計價持倉（stock/ETF）。\n\n"
            f"  目前持倉：{len(positions)} 個\n"
            f"  選擇權（已排除）：{len(options_excluded)} 個\n"
            f"  非 USD 持倉（已排除）：{len(tw_excluded)} 個\n\n"
            "請新增至少一個美股或美股 ETF 部位後再試。[/red]",
            title="績效歷史 — 前置條件不足", border_style="red"
        ))
        return

    # ── 互動選單：選擇期間 ──
    console.print(Panel(
        "📈 [bold cyan]績效歷史分析[/bold cyan]\n"
        f"可回測持倉：[bold]{len(tradeable)}[/bold] 個"
        + (f"（已排除 [yellow]{len(options_excluded)}[/yellow] 個選擇權）" if options_excluded else "")
        + (f"（已排除 [yellow]{len(tw_excluded)}[/yellow] 個非 USD 持倉）" if tw_excluded else ""),
        title="績效歷史", border_style="cyan"
    ))

    period_choice = Prompt.ask(
        "選擇比較期間  [1] 近60天（預設） [2] 近180天 [3] YTD [q] 取消並返回",
        choices=["1", "2", "3", "q"],
        default="1"
    )
    if period_choice == "q":
        console.print("[yellow]已取消並返回。[/yellow]")
        raise typer.Exit()

    today = datetime.utcnow().date()
    if period_choice == "1":
        days = 60
        start_date = today - timedelta(days=60)
        period_label = "近 60 天"
    elif period_choice == "2":
        days = 180
        start_date = today - timedelta(days=180)
        period_label = "近 180 天"
    else:
        start_date = date_type(today.year, 1, 1)
        days = (today - start_date).days
        period_label = f"YTD ({today.year} 年初至今)"

    bm_choice = Prompt.ask(
        "選擇比較基準  [1] SPY（預設） [2] QQQ [3] ^GSPC [4] 停用 [q] 取消並返回",
        choices=["1", "2", "3", "4", "q"],
        default="1"
    )
    if bm_choice == "q":
        console.print("[yellow]已取消並返回。[/yellow]")
        raise typer.Exit()
    bm_map = {"1": "SPY", "2": "QQQ", "3": "^GSPC", "4": "none"}
    bm_symbol = bm_map[bm_choice]
    use_benchmark = bm_symbol != "none"

    # ── 產生週節點 ──
    from datetime import datetime as dt_cls
    week_dates: list[date_type] = []
    cursor = start_date
    while cursor <= today:
        week_dates.append(cursor)
        cursor += timedelta(days=7)
    if not week_dates or week_dates[-1] < today:
        week_dates.append(today)

    start_dt = dt_cls.combine(start_date, dt_cls.min.time())
    end_dt = dt_cls.combine(today, dt_cls.min.time())

    # ── 下載歷史股價（週頻） ──
    symbols = [p.symbol for p in tradeable]
    unique_syms = list(dict.fromkeys(symbols))  # deduplicate, preserve order

    with console.status(f"[cyan]正在下載 {len(unique_syms)} 個持倉的歷史週價格...[/cyan]"):
        price_data = fetch_historical_prices_weekly(unique_syms, start_dt, end_dt)

    # ── 前置條件 3：至少要有一個 symbol 有歷史資料 ──
    has_any_data = any(bool(v) for v in price_data.values())
    if not has_any_data:
        console.print(Panel(
            "[red]❌  無法從網路下載到歷史股價資料。\n"
            "請確認網路連線正常，或稍後再試。[/red]",
            title="績效歷史 — 資料下載失敗", border_style="red"
        ))
        return

    # ── 計算每個週節點的組合市值 ──
    # Portfolio weekly values + per-broker breakdown
    port_weekly: list[float] = []
    broker_set = sorted(set(p.broker for p in tradeable))
    broker_weekly: dict[str, list[float]] = {b: [] for b in broker_set}

    import math
    for wd in week_dates:
        week_total = 0.0
        broker_totals: dict[str, float] = {b: 0.0 for b in broker_set}
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

    # Filter out zero weeks (no data)
    valid_mask = [v > 0 for v in port_weekly]
    if sum(valid_mask) < 2:
        console.print(Panel(
            "[yellow]⚠️  下載到的歷史資料節點不足（需至少 2 個非零週節點）。\n"
            f"目前有效節點：{sum(valid_mask)} 個\n\n"
            "可能原因：部位為近期新增、選取期間過短，或股票在該期間未上市。[/yellow]",
            title="績效歷史 — 資料不足", border_style="yellow"
        ))
        return

    filt_dates = [week_dates[i] for i in range(len(week_dates)) if valid_mask[i]]
    filt_port = [port_weekly[i] for i in range(len(port_weekly)) if valid_mask[i]]
    filt_broker = {b: [broker_weekly[b][i] for i in range(len(week_dates)) if valid_mask[i]] for b in broker_set}

    # ── Benchmark 週頻數據 ──
    bm_weekly: list[Optional[float]] = [None] * len(filt_dates)
    bm_return: Optional[float] = None
    alpha: Optional[float] = None

    if use_benchmark:
        with console.status(f"[cyan]正在下載 {bm_symbol} 基準指數週頻資料...[/cyan]"):
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

    # ── 計算組合回報 ──
    port_return: Optional[float] = None
    if filt_port[0] > 0:
        port_return = (filt_port[-1] / filt_port[0]) - 1.0
        if bm_return is not None:
            alpha = port_return - bm_return

    # ── 標題 Banner ──
    console.print()
    console.print(Panel(
        f"📈 [bold cyan]績效歷史分析[/bold cyan]\n"
        f"[dim]期間：[/dim]{period_label}  "
        f"[dim]基準：[/dim]{bm_symbol if use_benchmark else '停用'}  "
        f"[dim]回測持倉：[/dim]{len(tradeable)} 個 (USD 股/ETF)"
        + (f"  [dim yellow]已排除 {len(options_excluded)} 個選擇權[/dim yellow]" if options_excluded else ""),
        title="📊 Asset Performance Backtest",
        border_style="cyan"
    ))

    # ── 績效摘要 Panel ──
    summary_parts = []
    if port_return is not None:
        pr_c = "green" if port_return >= 0 else "red"
        pr_s = "+" if port_return >= 0 else ""
        summary_parts.append(f"組合回報: [{pr_c} bold]{pr_s}{port_return * 100:.2f}%[/{pr_c} bold]")
    if use_benchmark and bm_return is not None:
        bm_c = "green" if bm_return >= 0 else "red"
        bm_s = "+" if bm_return >= 0 else ""
        summary_parts.append(f"{bm_symbol}: [{bm_c}]{bm_s}{bm_return * 100:.2f}%[/{bm_c}]")
    if alpha is not None:
        al_c = "green" if alpha >= 0 else "red"
        al_s = "+" if alpha >= 0 else ""
        al_arrow = "▲" if alpha >= 0 else "▼"
        summary_parts.append(f"Alpha: [{al_c} bold]{al_s}{alpha * 100:.2f}% {al_arrow}[/{al_c} bold]")
    if filt_port:
        hi_v = max(filt_port)
        lo_v = min(filt_port)
        summary_parts.append(f"最高: [cyan]${hi_v:,.0f}[/cyan]  最低: [cyan]${lo_v:,.0f}[/cyan]  當前: [bold]${filt_port[-1]:,.0f}[/bold]")

    if summary_parts:
        # Split into two rows for readability
        row1 = "   ".join(summary_parts[:3])
        row2 = "   ".join(summary_parts[3:])
        console.print(Panel(
            row1 + ("\n" + row2 if row2 else ""),
            title="📊 績效摘要", border_style="yellow"
        ))

    # ── ASCII 混合圖表 ──
    chart_str = draw_history_chart(
        filt_dates, filt_port,
        bm_vals=bm_weekly if use_benchmark else None,
        broker_weekly=filt_broker if len(broker_set) > 1 else None,
        bm_label=bm_symbol,
        width=60, height=14,
    )
    console.print(chart_str)
    console.print()

    # ── 各週明細表 ──
    table = Table(title="每週績效明細", show_lines=False)
    table.add_column("週節點", style="dim")
    table.add_column("組合市值 (USD)", justify="right")
    table.add_column("週變動", justify="right")
    if use_benchmark and any(v is not None for v in bm_weekly):
        table.add_column(bm_symbol, justify="right", style="dim")
        table.add_column("超額", justify="right")

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
        row = [wd.strftime("%Y-%m-%d"), f"${pv:,.0f}", chg_str]
        if use_benchmark and any(v is not None for v in bm_weekly):
            bm_v = bm_weekly[i]
            bm_str = f"${bm_v:,.0f}" if bm_v is not None else "—"
            rel_str = "—"
            if bm_v is not None and bm_v > 0:
                rel_pct = (pv / bm_v - 1) * 100
                rc = "green" if rel_pct >= 0 else "red"
                rs = "+" if rel_pct >= 0 else ""
                rel_str = f"[{rc}]{rs}{rel_pct:.1f}%[/{rc}]"
            row += [bm_str, rel_str]
        table.add_row(*row)
        prev_pv = pv

    console.print(table)

    # ── 重大總經事件 ──
    events = get_upcoming_macro_events()
    if events:
        event_table = Table(title="📅 近期重大總經事件 (未來 90 天)", show_header=True)
        event_table.add_column("日期", style="cyan")
        event_table.add_column("事件", style="bold")
        event_table.add_column("距今", justify="right", style="dim")
        today_date = datetime.utcnow().date()
        for ev_date, ev_label, time_str in events:
            days_away = (ev_date - today_date).days
            event_name = MACRO_EVENT_NAMES.get(ev_label, ev_label)
            event_table.add_row(
                ev_date.strftime("%Y-%m-%d"),
                f"{ev_label} {event_name} ({time_str})",
                f"{days_away} 天後"
            )
        console.print(event_table)



@app.command(name="calendar")
def calendar_cmd(
    ctx: typer.Context,
    days: int = typer.Option(90, "--days", "-d", help="顯示未來天數內的事件"),
):
    """顯示投資組合持倉與 SOX 十大成分股的財報日曆，以及重大總經事件。"""
    user = ctx.obj
    from datetime import datetime as dt_cls, date as date_type, timedelta
    from .quotes import _normalize_symbol_for_yf

    positions = load_manual_positions(user=user)

    portfolio_tickers = set()
    for p in positions:
        sym = p.underlying if p.instrument_type == "option" else p.symbol
        portfolio_tickers.add(_normalize_symbol_for_yf(sym, "stock", p.currency))

    unique_tickers = list(portfolio_tickers.union(SOX_TICKERS))
    console.print(f"🔍 正在背景同步 {len(unique_tickers)} 個標的之財報日期及總經事件...")

    ticker_to_data = fetch_earnings_calendar(unique_tickers)

    today = datetime.utcnow().date()
    cutoff = today + timedelta(days=days)

    events = []

    # Add earnings dates
    for sym, (dates_list, info_date, time_str, period_str) in ticker_to_data.items():
        is_user = any(
            _normalize_symbol_for_yf(p.underlying if p.instrument_type == "option" else p.symbol, "stock", p.currency) == sym
            for p in positions
        )
        is_sox = sym in SOX_TICKERS
        
        if is_user and is_sox:
            label_base = f"🔔 [bold white]{sym}[/bold white] 財報公佈 (持倉/SOX 十大)"
        elif is_user:
            label_base = f"🔔 [bold white]{sym}[/bold white] 財報公佈 (持倉)"
        else:
            label_base = f"💻 {sym} 財報公佈 (SOX 十大)"

        if info_date and today <= info_date <= cutoff:
            if period_str:
                label = f"{label_base} ({period_str} {time_str})"
            else:
                label = f"{label_base} ({time_str})"
            events.append((info_date, label))
        else:
            for d in dates_list:
                if isinstance(d, dt_cls):
                    d = d.date()
                if today <= d <= cutoff:
                    events.append((d, label_base))

    # Add macro events
    macro_list = get_upcoming_macro_events(days=days)
    for ev_date, ev_label, time_str in macro_list:
        event_name = MACRO_EVENT_NAMES.get(ev_label, ev_label)
        events.append((ev_date, f"{event_name} ({time_str})"))

    if not events:
        console.print(f"[yellow]⚠️ 未來 {days} 天內沒有任何重要事件。[/yellow]")
        return

    # Sort events chronologically
    events.sort(key=lambda x: x[0])
    
    # Group by month
    by_month = {}
    for d, label in events:
        m_key = d.strftime("%Y-%m (%B)")
        by_month.setdefault(m_key, []).append((d, label))

    console.print(f"\n📅 [bold cyan]重要事件日曆 (未來 {days} 天)[/bold cyan]")
    console.print("=" * 50)

    for m_key, ev_list in sorted(by_month.items()):
        # Draw monthly table
        table = Table(title=f"\n📅 [bold magenta]{m_key}[/bold magenta]", show_header=True, expand=True)
        table.title_align = "left"
        table.add_column("日期", style="cyan", width=12)
        table.add_column("事件 / 標的", style="white", width=40)
        table.add_column("距今", justify="right", style="dim", width=10)
        
        for d, label in ev_list:
            days_away = (d - today).days
            table.add_row(
                d.strftime("%Y-%m-%d"),
                label,
                f"{days_away} 天後"
            )
        console.print(table)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    user: str = typer.Option("default", "--user", "-u", help="指定使用者帳戶"),
):
    """AssetTrack CLI 投資組合追蹤入口。"""
    if ctx.invoked_subcommand is not None:
        ctx.obj = user
        return
        
    from .tui import run_tui_dashboard
    run_tui_dashboard(user)


if __name__ == "__main__":
    app()
