"""
tests/test_strategy.py
======================
Unit tests for strategy.indicators — pure functions that can be tested without exchange.
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

from strategy.indicators import (
    compute_emas, compute_volume_sma, compute_rsi,
    compute_vwap_session, analyze_order_book, compute_trend_context,
)


def make_ohlcv_df(n: int = 100, base_price: float = 100000.0) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame for testing."""
    now = datetime.now(timezone.utc)
    timestamps = [now - timedelta(minutes=5 * (n - i)) for i in range(n)]
    prices = [base_price + np.sin(i / 10) * 500 + i * 10 for i in range(n)]
    volumes = [10.0 + np.random.rand() * 5 for _ in range(n)]
    df = pd.DataFrame({
        "open": prices,
        "high": [p + 50 + np.random.rand() * 20 for p in prices],
        "low": [p - 50 - np.random.rand() * 20 for p in prices],
        "close": [p + np.random.rand() * 10 - 5 for p in prices],
        "volume": volumes,
    }, index=pd.DatetimeIndex(timestamps))
    return df


class TestComputeEMAs:
    def test_returns_three_values(self):
        df = make_ohlcv_df(100)
        e9, e21, e50 = compute_emas(df)
        assert isinstance(e9, float)
        assert isinstance(e21, float)
        assert isinstance(e50, float)

    def test_insufficient_data(self):
        df = make_ohlcv_df(10)
        e9, e21, e50 = compute_emas(df)
        # Should return last close for all when insufficient data
        assert e9 == pytest.approx(float(df["close"].iloc[-1]))

    def test_empty_df(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        e9, e21, e50 = compute_emas(df)
        assert e9 == 0.0


class TestComputeVolumeSMA:
    def test_returns_ratio(self):
        df = make_ohlcv_df(100)
        cv, sma, ratio = compute_volume_sma(df, period=20)
        assert cv > 0
        assert sma > 0
        assert ratio > 0

    def test_insufficient_data(self):
        df = make_ohlcv_df(5)
        cv, sma, ratio = compute_volume_sma(df, period=20)
        assert ratio == 1.0  # fallback


class TestComputeRSI:
    def test_returns_value_in_range(self):
        df = make_ohlcv_df(100)
        rsi = compute_rsi(df, period=14)
        assert 0 <= rsi <= 100

    def test_insufficient_data(self):
        df = make_ohlcv_df(5)
        rsi = compute_rsi(df, period=14)
        assert rsi == 50.0


class TestAnalyzeOrderBook:
    def test_empty_order_book(self):
        strength, in_range = analyze_order_book(None)
        assert strength == 1.0
        assert in_range is False

    def test_no_bids_asks(self):
        ob = {"bids": [], "asks": []}
        strength, in_range = analyze_order_book(ob)
        assert strength == 1.0
        assert in_range is False

    def test_normal_order_book(self):
        mid = 100000.0
        ob = {
            "bids": [[mid - 10, 1.5], [mid - 50, 2.0], [mid - 200, 5.0]],
            "asks": [[mid + 10, 1.0], [mid + 50, 1.5], [mid + 200, 2.0]],
        }
        strength, in_range = analyze_order_book(ob)
        assert strength > 0
        assert isinstance(in_range, bool)


class TestComputeTrendContext:
    def test_bullish(self):
        df = make_ohlcv_df(100, base_price=100000.0)
        # Force an uptrend
        df["close"] = [100000 + i * 100 for i in range(100)]
        trend, e21, e50 = compute_trend_context(df)
        assert trend in ("bullish", "neutral")

    def test_bearish(self):
        df = make_ohlcv_df(100, base_price=100000.0)
        df["close"] = [100000 - i * 100 for i in range(100)]
        trend, e21, e50 = compute_trend_context(df)
        assert trend in ("bearish", "neutral")

    def test_insufficient_data(self):
        df = make_ohlcv_df(10)
        trend, e21, e50 = compute_trend_context(df)
        assert trend == "neutral"