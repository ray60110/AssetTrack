"""
assettrack/greeks.py — Black-Scholes 選擇權希臘字母與損益兩平（純本機運算）

bug#00066: 期權觀察清單需顯示 Delta/Gamma/Theta 與損益兩平點，但 yfinance 只提供
未平倉量、權利金與隱含波動率（IV），並不提供希臘字母。此模組以標準 Black-Scholes
公式，從「真實抓到的」spot / strike / 到期天數 / IV / 無風險利率就地計算希臘字母，
不做任何估計或回填；輸入不足（缺 IV、已到期等）時回傳 None，交由畫面顯示「—」。

單位約定：
  - iv：小數（0.35 代表 35%）。
  - delta：每 $1 標的變動的權利金變動（call 0~1、put -1~0）。
  - gamma：每 $1 標的變動的 delta 變動。
  - theta：**每日**時間價值衰減（已除以 365）。
  - vega：**每 1 個百分點 IV** 的權利金變動（已除以 100）。
  - break_even：call = strike + 權利金；put = strike - 權利金。
"""
from __future__ import annotations

import math
from typing import Optional


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(
    spot: Optional[float],
    strike: Optional[float],
    dte_days: Optional[float],
    iv: Optional[float],
    option_type: str,
    premium: Optional[float] = None,
    r: float = 0.04,
) -> dict:
    """回傳 {delta, gamma, theta, vega, break_even}；任一希臘字母無法計算時為 None。

    break_even 只需 strike 與 premium 即可計算，故即使 IV 缺失、希臘字母為 None，
    仍會盡量回傳 break_even。
    """
    out = {"delta": None, "gamma": None, "theta": None, "vega": None, "break_even": None}

    # 損益兩平（不依賴 IV / 希臘字母）
    if premium is not None and strike is not None:
        out["break_even"] = (strike + premium) if option_type == "call" else (strike - premium)

    try:
        if (
            spot and strike and iv and dte_days is not None
            and spot > 0 and strike > 0 and iv > 0 and dte_days > 0
        ):
            T = dte_days / 365.0
            sqrtT = math.sqrt(T)
            d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * T) / (iv * sqrtT)
            d2 = d1 - iv * sqrtT
            pdf = _norm_pdf(d1)

            if option_type == "call":
                out["delta"] = _norm_cdf(d1)
                theta_yr = (
                    -spot * pdf * iv / (2.0 * sqrtT)
                    - r * strike * math.exp(-r * T) * _norm_cdf(d2)
                )
            else:
                out["delta"] = _norm_cdf(d1) - 1.0
                theta_yr = (
                    -spot * pdf * iv / (2.0 * sqrtT)
                    + r * strike * math.exp(-r * T) * _norm_cdf(-d2)
                )

            out["gamma"] = pdf / (spot * iv * sqrtT)
            out["theta"] = theta_yr / 365.0          # 每日
            out["vega"] = spot * pdf * sqrtT / 100.0  # 每 1% IV
    except (ValueError, ZeroDivisionError):
        pass

    return out


def bs_price(
    spot: Optional[float], strike: Optional[float], dte_days: Optional[float],
    iv: Optional[float], option_type: str, r: float = 0.04,
) -> Optional[float]:
    """Black-Scholes 理論價（權利金）。輸入不足回 None。"""
    try:
        if not (spot and strike and iv and dte_days and spot > 0 and strike > 0 and iv > 0 and dte_days > 0):
            return None
        T = dte_days / 365.0
        sqrtT = math.sqrt(T)
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * T) / (iv * sqrtT)
        d2 = d1 - iv * sqrtT
        if option_type == "call":
            return spot * _norm_cdf(d1) - strike * math.exp(-r * T) * _norm_cdf(d2)
        return strike * math.exp(-r * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    except (ValueError, ZeroDivisionError):
        return None


def implied_vol(
    spot: Optional[float], strike: Optional[float], dte_days: Optional[float],
    premium: Optional[float], option_type: str, r: float = 0.04,
    lo: float = 1e-3, hi: float = 5.0, tol: float = 1e-4, max_iter: int = 60,
) -> Optional[float]:
    """由市場權利金反解隱含波動率（二分法）。用於投資組合淨 Greeks——我們已有選擇權
    的真實 market_price(premium)，只要再抓標的現價即可反推 IV，不必依賴 yfinance 的 IV
    欄位(bug#00069)。權利金低於內含價值(壞資料/套利)或超出可達範圍時回 None。"""
    if not (spot and strike and dte_days and premium and premium > 0 and dte_days > 0):
        return None
    intrinsic = max(0.0, (spot - strike) if option_type == "call" else (strike - spot))
    if premium < intrinsic - 0.02:
        return None
    p_lo = bs_price(spot, strike, dte_days, lo, option_type, r)
    p_hi = bs_price(spot, strike, dte_days, hi, option_type, r)
    if p_lo is None or p_hi is None:
        return None
    if premium <= p_lo:
        return None            # below achievable min → no meaningful IV
    if premium >= p_hi:
        return hi              # extremely high IV; cap
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        pm = bs_price(spot, strike, dte_days, mid, option_type, r)
        if pm is None:
            return None
        if abs(pm - premium) < tol:
            return mid
        if pm < premium:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
