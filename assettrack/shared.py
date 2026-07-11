"""
assettrack/shared.py — 共享純邏輯模組

從 cli.py 遷移至此，讓 tui.py 可直接 import 而不依賴 Typer CLI 層。
包含：
  - MACRO_EVENT_NAMES: dict[str, str]          — 總經事件顯示名稱對照
  - get_upcoming_macro_events()                 — 取得 FED/NFP/CPI 硬編碼日期
  - draw_history_chart()                        — ASCII 組合 + 大盤 bar/line 圖
  - position_stance_by_symbol()                 — 依持倉判斷各標的的淨多空立場
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional


def position_stance_by_symbol(positions) -> "dict[str, str]":
    """依使用者持倉，回傳每個標的的淨多空立場：'多'、'空' 或 '混合'。

    用於讓 ETF/期權觀察清單的結論能與使用者自身部位方向交叉比對，產生
    「與你的部位一致／相反／你尚未持有」這類建設性提示。

    判斷方式：
      - 股票 / ETF：淨數量 > 0 記為看多，< 0 記為看空。
      - 選擇權：long call 或 short put → 看多；long put 或 short call → 看空。
    同一標的多筆部位以 +1（看多）/ -1（看空）累加，正為多、負為空、零為混合。
    """
    lean: dict[str, float] = {}
    for p in positions or []:
        if p.instrument_type == "option":
            sym = (p.underlying or "").upper()
            if not sym:
                continue
            is_call = p.option_type == "call"
            is_long = p.quantity >= 0
            bullish = (is_call and is_long) or ((not is_call) and (not is_long))
            lean[sym] = lean.get(sym, 0.0) + (1.0 if bullish else -1.0)
        elif p.instrument_type in ("stock", "etf"):
            sym = p.symbol.upper().replace(".TWO", "").replace(".TW", "")
            lean[sym] = lean.get(sym, 0.0) + (1.0 if p.quantity >= 0 else -1.0)
    return {
        sym: ("多" if v > 0 else "空" if v < 0 else "混合")
        for sym, v in lean.items()
    }


# ── Macro event display name mapping ──────────────────────────────────────────
MACRO_EVENT_NAMES: dict[str, str] = {
    "▼FED": "▼ FED 利率決議",
    "★NFP": "★ NFP 非農就業 / 失業率",
    "◆CPI": "◆ CPI 通膨指數公佈",
}


def get_upcoming_macro_events(days: int = 90, start_days_ago: int = 0) -> "list[tuple]":
    """
    Returns upcoming macro events within the next ~days days as (date, label, time_str) tuples.
    Hardcoded 2025-2027 schedule. Sorted ascending.
    """
    from datetime import date as date_type, timedelta

    fed_dates = [
        date_type(2025, 7, 30), date_type(2025, 9, 17), date_type(2025, 10, 29),
        date_type(2025, 12, 10), date_type(2026, 1, 28), date_type(2026, 3, 18),
        date_type(2026, 4, 29), date_type(2026, 6, 17), date_type(2026, 7, 29),
        date_type(2026, 9, 16), date_type(2026, 11, 5), date_type(2026, 12, 16),
        date_type(2027, 1, 27), date_type(2027, 3, 17), date_type(2027, 4, 28),
        date_type(2027, 6, 16), date_type(2027, 7, 28), date_type(2027, 9, 22),
        date_type(2027, 11, 3), date_type(2027, 12, 15),
    ]
    nfp_dates = [
        date_type(2025, 7, 4), date_type(2025, 8, 1), date_type(2025, 9, 5),
        date_type(2025, 10, 3), date_type(2025, 11, 7), date_type(2025, 12, 5),
        date_type(2026, 1, 9), date_type(2026, 2, 6), date_type(2026, 3, 6),
        date_type(2026, 4, 3), date_type(2026, 5, 1), date_type(2026, 6, 5),
        date_type(2026, 7, 10), date_type(2026, 8, 7), date_type(2026, 9, 4),
        date_type(2026, 10, 2), date_type(2026, 11, 6), date_type(2026, 12, 4),
        date_type(2027, 1, 8), date_type(2027, 2, 5), date_type(2027, 3, 5),
        date_type(2027, 4, 2), date_type(2027, 5, 7), date_type(2027, 6, 4),
        date_type(2027, 7, 2), date_type(2027, 8, 6), date_type(2027, 9, 3),
        date_type(2027, 10, 8), date_type(2027, 11, 5), date_type(2027, 12, 3),
    ]
    cpi_dates = [
        date_type(2025, 7, 15), date_type(2025, 8, 12), date_type(2025, 9, 10),
        date_type(2025, 10, 15), date_type(2025, 11, 13), date_type(2025, 12, 10),
        date_type(2026, 1, 14), date_type(2026, 2, 11), date_type(2026, 3, 11),
        date_type(2026, 4, 10), date_type(2026, 5, 13), date_type(2026, 6, 10),
        date_type(2026, 7, 14), date_type(2026, 8, 12), date_type(2026, 9, 11),
        date_type(2026, 10, 14), date_type(2026, 11, 12), date_type(2026, 12, 11),
        date_type(2027, 1, 13), date_type(2027, 2, 10), date_type(2027, 3, 12),
        date_type(2027, 4, 13), date_type(2027, 5, 12), date_type(2027, 6, 15),
        date_type(2027, 7, 13), date_type(2027, 8, 11), date_type(2027, 9, 14),
        date_type(2027, 10, 13), date_type(2027, 11, 10), date_type(2027, 12, 10),
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

    label_w = 11
    chart_w = width - label_w - 3
    col_w = max(3, chart_w // n)
    actual_w = col_w * n

    grid = [[" " for _ in range(actual_w)] for _ in range(height)]

    start_row = to_row(port_vals[0])
    if 0 <= start_row < height:
        for x in range(actual_w):
            grid[start_row][x] = "╌"

    broker_chars = ["█", "▓", "▒", "░", "▐", "▌"]
    broker_names_list = list(broker_weekly.keys()) if broker_weekly else []
    broker_color_map = {
        b: broker_chars[i % len(broker_chars)]
        for i, b in enumerate(broker_names_list)
    }

    bar_margin = max(1, col_w // 5)
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
                bc = "█"
                for y in range(row_top, row_bottom + 1):
                    for x in range(x_lo, x_hi + 1):
                        grid[y][x] = bc

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
                        grid[iy][ix] = "┼"
        for bx, by in bm_points:
            if 0 <= by < height and 0 <= bx < actual_w:
                grid[by][bx] = "○"

    lines = []
    for row in range(height):
        val_at_row = y_max - (row * y_range / (height - 1))
        y_lbl = f"${val_at_row:>9,.0f}"
        sep = "┤" if row == start_row else "│"
        lines.append(f"[dim]{y_lbl}[/dim] {sep} " + "".join(grid[row]))

    lines.append(" " * (label_w + 1) + " └" + "─" * actual_w)

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
