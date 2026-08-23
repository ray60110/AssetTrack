"""
assettrack/shared.py — 共享純邏輯模組

從 cli.py 遷移至此，讓 tui.py 可直接 import 而不依賴 Typer CLI 層。
包含：
  - MACRO_EVENT_NAMES: dict[str, str]          — 總經事件顯示名稱對照
  - get_upcoming_macro_events()                 — 取得 FED/NFP/CPI 硬編碼日期
  - draw_history_chart()                        — ASCII 組合 + 大盤 bar/line 圖
  - is_taiwan_position()                         — 判斷部位是否為台股（投資建議一律排除）
  - position_stance_by_symbol()                 — 依持倉判斷各標的的淨多空立場
  - format_updated_at()                          — 統一「更新時間」時間戳記格式 (yyyy-mm-dd hh:mm)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# 投資建議三層寫作格式（bug#00117）——所有投資建議的單一真理來源
# ─────────────────────────────────────────────────────────────────────────────
# 使用者要求把「投資建議的寫作格式全部調整」成清楚三層：
#   1. 結論（verdict）      ：先點出標的的多空方向與結論（單行）。
#   2. 判斷依據（basis）    ：1–2 句「如何判斷此結論」。
#   3. breakdown（detail_sections）：解釋為何、給公式與帶入本標的數字的計算方式，
#      並收納所有量化附註（回測命中率／顯著性、財報降權、部位一致性、IV 位階…）。
# 各分析模組的生成函式一律先組出 list[Recommendation]，主頁卡片只投影第一＋二層
# （dashboard_line 收斂成一句），detail 畫面投影第一＋二層＋可點選連結（detail_headline），
# 公式細節頁（RecommendationDetailScreen）投影完整第三層——三種投影、同一份真理來源，
# 維持「結論＝被回測＝同一函式」紀律。

@dataclass
class Recommendation:
    """一則投資建議的結構化表示（三層寫作格式的單一真理來源）。"""
    rec_id: str                       # 穩定唯一 id（僅供辨識/測試；畫面點選用 render token）
    category: str                     # 'etf'|'etf_stance'|'sector'|'options'|'event'
    direction: Optional[str]          # '多'|'空'|'觀望'|None（事件為 None，資訊性）
    verdict: str                      # 第一層：emoji＋方向＋標的＋結論（單行 Rich markup）
    basis: str                        # 第二層：1–2 句「如何判斷」（Rich markup，可空）
    detail_sections: list = field(default_factory=list)  # 第三層：list[dict]
    #   每個 section: {'heading': str, 'formula': str, 'substitution': str, 'explanation': str}


def _section(heading: str, formula: str = "", substitution: str = "", explanation: str = "") -> dict:
    """建一個第三層 breakdown section。缺項留空字串（畫面自動略過空列）。"""
    return {"heading": heading, "formula": formula, "substitution": substitution,
            "explanation": explanation}


def dashboard_line(rec: "Recommendation") -> str:
    """主頁投影：把第一層結論＋第二層判斷依據收斂成「一句話」（bug#00117）。"""
    basis = (rec.basis or "").strip()
    if basis:
        return f"{rec.verdict}　[dim]—[/dim] {basis}"
    return rec.verdict


def detail_headline(rec: "Recommendation", token: str) -> str:
    """detail 畫面投影：第一層結論＋第二層判斷依據＋可點選「查看公式細節」連結
    （bug#00118）。token 為該畫面本次 render 指派的 ASCII 安全點選代號；點選觸發
    該畫面的 action_show_formula(token) 推入公式細節頁。"""
    lines = [rec.verdict]
    basis = (rec.basis or "").strip()
    if basis:
        lines.append(f"   [dim]依據：[/dim]{basis}")
    lines.append(f"   [@click=screen.show_formula('{token}')]🔍 查看公式細節 ›[/]")
    return "\n".join(lines)


def render_detail_recs(recs: "list[Recommendation]", header: Optional[str] = None,
                       start: int = 0) -> "tuple[str, dict]":
    """把一串 Recommendation render 成 detail 畫面用的 markup 字串，並回傳
    {token: rec} 對照（供 action_show_formula 查詢）。token 為 'r0','r1',…（ASCII
    安全，避開 Chinese/引號破壞 @click markup 解析與 hotkey 佔用問題，bug#00118）。
    `start` 供同一畫面多段 render 時延續編號避免碰撞。"""
    mapping: dict = {}
    blocks: list[str] = []
    if header:
        blocks.append(header)
    for i, rec in enumerate(recs):
        tok = f"r{start + i}"
        mapping[tok] = rec
        blocks.append(detail_headline(rec, tok))
    return "\n\n".join(blocks), mapping


def format_updated_at(dt: Optional[datetime]) -> str:
    """將 datetime 格式化為全畫面統一的「更新時間」戳記字串：yyyy-mm-dd hh:mm。

    尚未有資料（dt 為 None）時回傳「—」，不臆測時間。"""
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def is_taiwan_position(p) -> bool:
    """判斷一筆部位是否屬於台股 / 台幣市場。

    bug#00091（使用者決策：投資建議一律以美股為主、移除台股）：各分析功能
    （主動式ETF／期權觀察清單／類股板塊）與其回測，一律排除台股
    部位；台股/TWD 的「持倉追蹤、報價、基準幣別換算」不受影響、照常運作，僅
    「投資建議」層面剔除台股。判定口徑與 models.Position 內建的 is_tw 完全一致
    （幣別 TWD、代碼 .TW/.TWO 結尾、或 market == "TW"），是唯一的判定來源。"""
    currency = (getattr(p, "currency", None) or "")
    symbol = (getattr(p, "symbol", None) or "")
    market = (getattr(p, "market", None) or "")
    return (
        currency == "TWD"
        or symbol.endswith(".TW")
        or symbol.endswith(".TWO")
        or market == "TW"
    )


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
        # 投資建議一律排除台股（bug#00091）——台股持倉仍照常追蹤，只是不進入
        # ETF／期權等結論的「與你部位方向一致/相反」交叉比對。
        if is_taiwan_position(p):
            continue
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

DEFAULT_EVENT_TIMEZONE = "Asia/Taipei"


def event_timezone(timezone_name: str = DEFAULT_EVENT_TIMEZONE):
    """Return a validated IANA timezone for event display.

    Invalid or unavailable timezone names fall back to Asia/Taipei so calendar
    rendering never fails. UI input validates before persisting, making this
    fallback primarily useful for old/corrupt preference files.
    """
    import zoneinfo

    try:
        return zoneinfo.ZoneInfo(timezone_name)
    except (KeyError, ValueError, TypeError):
        return zoneinfo.ZoneInfo(DEFAULT_EVENT_TIMEZONE)


def format_timezone_label(timezone_name: str, at: Optional[datetime] = None) -> str:
    """Format an IANA timezone with its date-aware UTC offset."""
    tz = event_timezone(timezone_name)
    local_dt = (at or datetime.now(tz)).astimezone(tz)
    offset = local_dt.utcoffset()
    total_minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{getattr(tz, 'key', timezone_name)} (UTC{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d})"


def _fmt_signed_pct(v: float, decimals: int = 1) -> str:
    """帶正負號的百分比字串，並依方向上色（升為紅、降為綠，符合通膨/利率語意）。"""
    sign = "+" if v >= 0 else ""
    color = "red" if v > 0 else "green" if v < 0 else "dim"
    return f"[{color}]{sign}{v:.{decimals}f}%[/{color}]"


def format_macro_readings(readings: "Optional[dict]") -> Optional[str]:
    """將 fetch_latest_macro_readings() 的結果格式化為單行 Rich markup，供
    UpcomingEventsScreen 顯示各總經指標「最新一期已公佈數值」。

    每項資料若為 None（缺 FRED_API_KEY／API 失敗／資料不足）則跳過該項，不以
    預設值填補（比照全專案「不臆測」慣例）。全部皆缺時回傳 None，呼叫端可據此
    顯示「尚未取得」而非空白。日期以 (as_of) 標註資料所屬月份。"""
    if not readings:
        return None

    parts: list[str] = []

    cpi = readings.get("core_cpi")
    if cpi:
        parts.append(
            f"[bold]核心CPI[/bold] {_fmt_signed_pct(cpi['yoy_pct'])}[dim]YoY[/dim] "
            f"{_fmt_signed_pct(cpi['mom_pct'])}[dim]MoM[/dim]"
        )

    pce = readings.get("core_pce")
    if pce:
        parts.append(
            f"[bold]核心PCE[/bold] {_fmt_signed_pct(pce['yoy_pct'])}[dim]YoY[/dim] "
            f"{_fmt_signed_pct(pce['mom_pct'])}[dim]MoM[/dim]"
        )

    nfp = readings.get("nfp")
    if nfp:
        chg_k = nfp["change"] / 1000.0
        sign = "+" if chg_k >= 0 else ""
        color = "green" if chg_k >= 0 else "red"
        parts.append(f"[bold]NFP[/bold] [{color}]{sign}{chg_k:,.0f}K[/{color}]")

    ur = readings.get("unemployment")
    if ur:
        chg = ur["change_pp"]
        arrow = "▲" if chg > 0 else "▼" if chg < 0 else "＝"
        acolor = "red" if chg > 0 else "green" if chg < 0 else "dim"
        parts.append(
            f"[bold]失業率[/bold] {ur['rate_pct']:.1f}% "
            f"[{acolor}]{arrow}{abs(chg):.1f}[/{acolor}]"
        )

    ff = readings.get("fed_funds")
    if ff:
        parts.append(f"[bold]聯邦資金利率[/bold] {ff['rate_pct']:.2f}%")

    if not parts:
        return None

    return "[dim]│[/dim] ".join(parts)


def macro_recommendations(readings: "Optional[dict]") -> "list[Recommendation]":
    """把 fetch_latest_macro_readings() 的重點總經指標組成三層結構化建議（bug#00117）。
    此類為「資訊性」——不投多空方向票（direction=None）：第一層＝指標＋期對期變動、
    第二層＝經濟意涵如何解讀、第三層＝期對期變動公式＋帶入上期/本期實際值＋來源說明。
    UpcomingEventsScreen 以此為單一真理來源，format_macro_analysis_lines 為其薄 wrapper。"""
    if not readings:
        return []

    recs: list[Recommendation] = []

    cpi = readings.get("core_cpi")
    if cpi and cpi.get("mom_pct") is not None:
        chg_s = f"{cpi['mom_change_pp']:+.2f}pp" if cpi.get("mom_change_pp") is not None else "—"
        prev_s = f"{cpi['prev_mom_pct']:.2f}%" if cpi.get("prev_mom_pct") is not None else "—"
        color = "green" if (cpi.get("mom_change_pp") or 0) < 0 else "red" if (cpi.get("mom_change_pp") or 0) > 0 else "yellow"
        recs.append(Recommendation(
            rec_id="event:core_cpi", category="event", direction=None,
            verdict=(f"📌 [bold white]核心 CPI[/bold white] ({cpi['as_of']}): "
                     f"月增 [bold]{cpi['mom_pct']:.2f}%[/bold] (較上期 {prev_s} [{color}]{chg_s}[/{color}]) │ "
                     f"年增 {cpi['yoy_pct']:.2f}%"),
            basis=cpi.get("interpretation") or "",
            detail_sections=[_section(
                "期對期變動計算（月增率）",
                formula="月增率變動量 Δ = 本期月增率(MoM) − 上期月增率(MoM)",
                substitution=(f"= {cpi['mom_pct']:.2f}% − {prev_s} = {chg_s}"
                              f"（年增率 YoY = {cpi['yoy_pct']:.2f}%）"),
                explanation=("月增率反映最近一個月的通膨動能：Δ<0（放緩）為通膨壓力減緩、"
                             "利於降息與寬鬆；Δ>0（回升）為粘性通膨反彈、降息預期可能延後。"
                             "資料來源：FRED 核心 CPI（剔除食品與能源），零臆測、缺資料即不列。"))],
        ))

    pce = readings.get("core_pce")
    if pce and pce.get("mom_pct") is not None:
        chg_s = f"{pce['mom_change_pp']:+.2f}pp" if pce.get("mom_change_pp") is not None else "—"
        prev_s = f"{pce['prev_mom_pct']:.2f}%" if pce.get("prev_mom_pct") is not None else "—"
        color = "green" if (pce.get("mom_change_pp") or 0) < 0 else "red" if (pce.get("mom_change_pp") or 0) > 0 else "yellow"
        recs.append(Recommendation(
            rec_id="event:core_pce", category="event", direction=None,
            verdict=(f"📌 [bold white]核心 PCE (Fed首要指標)[/bold white] ({pce['as_of']}): "
                     f"月增 [bold]{pce['mom_pct']:.2f}%[/bold] (較上期 {prev_s} [{color}]{chg_s}[/{color}]) │ "
                     f"年增 {pce['yoy_pct']:.2f}%"),
            basis=pce.get("interpretation") or "",
            detail_sections=[_section(
                "期對期變動計算（月增率）",
                formula="月增率變動量 Δ = 本期月增率(MoM) − 上期月增率(MoM)",
                substitution=(f"= {pce['mom_pct']:.2f}% − {prev_s} = {chg_s}"
                              f"（年增率 YoY = {pce['yoy_pct']:.2f}%）"),
                explanation=("核心 PCE 為 Fed 貨幣政策首要通膨指標。Δ<0（放緩）強化降息與寬鬆空間、"
                             "Δ>0（回升）延後寬鬆預期。資料來源：FRED 核心 PCE（剔除食品與能源）。"))],
        ))

    nfp = readings.get("nfp")
    if nfp and nfp.get("change") is not None:
        chg_k = nfp["change"] / 1000.0
        prev_k = nfp["prev_change"] / 1000.0 if nfp.get("prev_change") is not None else None
        prev_s = f"{prev_k:+,.0f}K" if prev_k is not None else "—"
        diff_k = nfp["change_diff"] / 1000.0 if nfp.get("change_diff") is not None else None
        diff_s = f"{diff_k:+,.0f}K" if diff_k is not None else "—"
        color = "green" if (diff_k or 0) < 0 else "yellow"
        recs.append(Recommendation(
            rec_id="event:nfp", category="event", direction=None,
            verdict=(f"📌 [bold white]非農就業 (NFP)[/bold white] ({nfp['as_of']}): "
                     f"新增 [bold]{chg_k:+,.0f}K[/bold] (較上月 {prev_s} [{color}]{diff_s}[/{color}])"),
            basis=nfp.get("interpretation") or "",
            detail_sections=[_section(
                "期對期變動計算（新增就業人數）",
                formula="人數變動 Δ = 本期新增非農就業 − 上期新增非農就業（單位：千人 K）",
                substitution=f"= {chg_k:+,.0f}K − {prev_s} = {diff_s}",
                explanation=("NFP 為勞動市場強弱指標。就業降溫（Δ<0）通常減緩薪資-通膨壓力、"
                             "利於寬鬆；過熱（Δ>0）則可能延後降息。資料來源：FRED 非農就業總人數月變動。"))],
        ))

    ur = readings.get("unemployment")
    if ur and ur.get("rate_pct") is not None:
        chg = ur["change_pp"]
        prev_r = ur.get("prev_pct")
        prev_s = f"{prev_r:.1f}%" if prev_r is not None else "—"
        color = "green" if chg > 0 else "red" if chg < 0 else "yellow"
        recs.append(Recommendation(
            rec_id="event:unemployment", category="event", direction=None,
            verdict=(f"📌 [bold white]失業率[/bold white] ({ur['as_of']}): "
                     f"[bold]{ur['rate_pct']:.1f}%[/bold] (較上期 {prev_s} [{color}]{chg:+.1f}pp[/{color}])"),
            basis=ur.get("interpretation") or "",
            detail_sections=[_section(
                "期對期變動計算（失業率）",
                formula="百分點變動 Δ = 本期失業率 − 上期失業率",
                substitution=f"= {ur['rate_pct']:.1f}% − {prev_s} = {chg:+.1f}pp",
                explanation=("失業率上升（Δ>0）通常伴隨經濟降溫、支持寬鬆；下降（Δ<0）為勞動市場"
                             "緊俏。資料來源：FRED 失業率（U-3）。"))],
        ))

    ff = readings.get("fed_funds")
    if ff and ff.get("rate_pct") is not None:
        chg = ff["change_pp"]
        prev_r = ff.get("prev_pct")
        prev_s = f"{prev_r:.2f}%" if prev_r is not None else "—"
        color = "green" if chg < 0 else "red" if chg > 0 else "yellow"
        recs.append(Recommendation(
            rec_id="event:fed_funds", category="event", direction=None,
            verdict=(f"📌 [bold white]有效聯邦資金利率[/bold white] ({ff['as_of']}): "
                     f"[bold]{ff['rate_pct']:.2f}%[/bold] (較上期 {prev_s} [{color}]{chg:+.2f}pp[/{color}])"),
            basis=ff.get("interpretation") or "",
            detail_sections=[_section(
                "期對期變動計算（政策利率）",
                formula="百分點變動 Δ = 本期有效聯邦資金利率 − 上期",
                substitution=f"= {ff['rate_pct']:.2f}% − {prev_s} = {chg:+.2f}pp",
                explanation=("政策利率下行（Δ<0）為寬鬆、支撐風險資產；上行（Δ>0）為緊縮。"
                             "資料來源：FRED 有效聯邦資金利率 (EFFR)。"))],
        ))

    return recs


def format_macro_analysis_lines(readings: "Optional[dict]") -> list[str]:
    """將重點經濟指標格式化為期對期比較與經濟意涵解析 bullet 清單。薄 wrapper：以
    macro_recommendations() 為單一真理來源，投影為原本的「📌 指標 / 💡 意涵」兩行格式，
    維持既有呼叫端輸出不變（bug#00117）。"""
    lines: list[str] = []
    for rec in macro_recommendations(readings):
        lines.append(rec.verdict)
        if (rec.basis or "").strip():
            lines.append(f"   💡 [cyan]{rec.basis}[/cyan]")
    return lines



def get_upcoming_macro_events(
    days: int = 90,
    start_days_ago: int = 0,
    timezone_name: str = DEFAULT_EVENT_TIMEZONE,
    reference_date=None,
) -> "list[tuple]":
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

    tz_target = event_timezone(timezone_name)
    today = reference_date or datetime.now(tz_target).date()
    start_date = today - timedelta(days=start_days_ago)
    cutoff = today + timedelta(days=days)

    events: list[tuple] = []
    import zoneinfo
    from datetime import datetime as dt_cls, time as time_cls

    tz_et = zoneinfo.ZoneInfo("America/New_York")

    def to_local(d, time_et):
        dt_et = dt_cls.combine(d, time_et).replace(tzinfo=tz_et)
        dt_local = dt_et.astimezone(tz_target)
        return dt_local.date(), dt_local.strftime("%H:%M")

    for d in fed_dates:
        local_d, local_t = to_local(d, time_cls(14, 0))
        if start_date <= local_d <= cutoff:
            events.append((local_d, "▼FED", local_t))
    for d in nfp_dates:
        local_d, local_t = to_local(d, time_cls(8, 30))
        if start_date <= local_d <= cutoff:
            events.append((local_d, "★NFP", local_t))
    for d in cpi_dates:
        local_d, local_t = to_local(d, time_cls(8, 30))
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
