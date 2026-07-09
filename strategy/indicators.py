"""
strategy/indicators.py
======================
Pure indicator calculation functions extracted from the StrategyEngine.
Each function is stateless and testable.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta as ta
from typing import Optional


def compute_emas(df: pd.DataFrame, fast: int = 9, mid: int = 21, slow: int = 50) -> tuple[float, float, float]:
    """Compute EMA(fast), EMA(mid), EMA(slow) on close. Returns latest values."""
    if df.empty or len(df) < slow:
        last = float(df["close"].iloc[-1]) if not df.empty else 0.0
        return last, last, last
    return (
        float(ta.ema(df["close"], length=fast).iloc[-1]),
        float(ta.ema(df["close"], length=mid).iloc[-1]),
        float(ta.ema(df["close"], length=slow).iloc[-1]),
    )


def compute_volume_sma(df: pd.DataFrame, period: int = 20) -> tuple[float, float, float]:
    """Returns (current_volume, sma, ratio)."""
    if df.empty or len(df) < period:
        cv = float(df["volume"].iloc[-1]) if not df.empty else 0.0
        return cv, cv, 1.0
    sma = df["volume"].rolling(window=period).mean()
    cv = float(df["volume"].iloc[-1])
    sv = float(sma.iloc[-1])
    ratio = cv / sv if sv > 0 else 1.0
    return cv, sv, ratio


def compute_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """Compute RSI(period) on close."""
    if df.empty or len(df) < period + 1:
        return 50.0
    rsi = ta.rsi(df["close"], length=period)
    if rsi is not None and not rsi.empty and not np.isnan(rsi.iloc[-1]):
        return float(rsi.iloc[-1])
    return 50.0


def compute_vwap_session(df: pd.DataFrame, session_date_str: str) -> float:
    """Compute session VWAP for the given UTC date string."""
    if df.empty:
        return 0.0
    candles = df[df.index.strftime("%Y-%m-%d") == session_date_str]
    if candles.empty:
        candles = df.tail(50)
    typical = (candles["high"] + candles["low"] + candles["close"]) / 3
    pv = (typical * candles["volume"]).sum()
    vol = candles["volume"].sum()
    return float(pv / vol) if vol > 0 else float(candles["close"].iloc[-1])


def analyze_order_book(
    order_book: Optional[dict],
    depth_pct: float = 0.01,
    wall_min_pct: float = 0.001,
    wall_max_pct: float = 0.003,
) -> tuple[float, bool]:
    """
    Analyze order book for bid walls.
    Returns (bid_wall_strength, bid_wall_within_range).
    """
    if not order_book:
        return 1.0, False
    bids = order_book.get("bids", [])
    asks = order_book.get("asks", [])
    if not bids or not asks:
        return 1.0, False
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return 1.0, False

    range_upper = mid * (1 + depth_pct)
    range_lower = mid * (1 - depth_pct)
    wall_upper = mid * (1 - wall_min_pct)
    wall_lower = mid * (1 - wall_max_pct)

    bid_vol = sum(q for p, q in bids if range_lower <= p <= range_upper)
    ask_vol = sum(q for p, q in asks if range_lower <= p <= range_upper)
    strength = bid_vol / ask_vol if ask_vol > 0 else 1.0

    wall_vol = sum(q for p, q in bids if wall_lower <= p <= wall_upper)
    avg_level = bid_vol / max(len(bids), 1)
    within_range = wall_vol > (avg_level * 2) if avg_level > 0 else False

    return float(strength), bool(within_range)


def compute_trend_context(df: pd.DataFrame, ema_mid: int = 21, ema_slow: int = 50) -> tuple[str, float, float]:
    """Determine trend from higher TF. Returns (trend_str, ema_mid, ema_slow)."""
    if df.empty or len(df) < ema_slow:
        return "neutral", 0.0, 0.0
    e21 = float(ta.ema(df["close"], length=ema_mid).iloc[-1])
    e50 = float(ta.ema(df["close"], length=ema_slow).iloc[-1])
    close = float(df["close"].iloc[-1])
    if close > e21 > e50:
        return "bullish", e21, e50
    elif close < e21 < e50:
        return "bearish", e21, e50
    return "neutral", e21, e50