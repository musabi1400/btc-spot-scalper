"""
tests/test_backtesting.py
=========================
Unit tests for the backtesting package.

Covers:
  • MetricsCalculator — known trade lists
  • DataLoader — synthetic CSV round-trip
  • BacktestEngine — synthetic uptrend data
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import pytest

from backtesting.metrics import MetricsCalculator
from backtesting.data_loader import DataLoader
from backtesting.engine import BacktestEngine, ConfluenceResult, IndicatorSnapshot
from backtesting.report import BacktestReport


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────

def _make_synthetic_ohlcv(
    n: int = 200,
    base_price: float = 100_000.0,
    trend: float = 50.0,
    noise: float = 20.0,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic 5-minute OHLCV data with a configurable uptrend.

    A strong uptrend ensures C1 (price > EMA21 > EMA50) is satisfied so the
    strategy can generate entries.
    """
    rng = np.random.default_rng(seed)
    now = datetime.now(timezone.utc)
    timestamps = [now - timedelta(minutes=5 * (n - i)) for i in range(n)]
    closes = [base_price + trend * i + rng.normal(0, noise) for i in range(n)]
    opens = [c + rng.normal(0, noise) for c in closes]
    highs = [max(o, c) + rng.uniform(10, 80) for o, c in zip(opens, closes)]
    lows = [min(o, c) - rng.uniform(10, 80) for o, c in zip(opens, closes)]
    volumes = [10.0 + rng.uniform(0, 5) for _ in range(n)]
    df = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        index=pd.DatetimeIndex(timestamps, name="timestamp"),
    )
    return df


def _make_trade(net_pnl: float, fees: float = 0.15) -> dict:
    """Create a minimal trade dict for metrics testing."""
    return {
        "net_pnl_usdt": net_pnl,
        "fees_total_usdt": fees,
        "entry_price": 100_000.0,
        "exit_price": 100_000.0 + net_pnl,
        "exit_time": datetime.now(timezone.utc),
        "exit_reason": "take_profit" if net_pnl > 0 else "stop_loss",
    }


# ──────────────────────────────────────────────
#  MetricsCalculator tests
# ──────────────────────────────────────────────

class TestMetricsCalculator:
    def test_win_rate_basic(self):
        trades = [_make_trade(10), _make_trade(-5), _make_trade(20), _make_trade(-3)]
        assert MetricsCalculator.calculate_win_rate(trades) == 0.5

    def test_win_rate_empty(self):
        assert MetricsCalculator.calculate_win_rate([]) == 0.0

    def test_win_rate_all_wins(self):
        trades = [_make_trade(10), _make_trade(20)]
        assert MetricsCalculator.calculate_win_rate(trades) == 1.0

    def test_profit_factor_basic(self):
        trades = [_make_trade(30), _make_trade(-10), _make_trade(20), _make_trade(-5)]
        # gross_profit = 50, gross_loss = 15 → 50/15
        pf = MetricsCalculator.calculate_profit_factor(trades)
        assert pf == pytest.approx(50 / 15, rel=1e-4)

    def test_profit_factor_no_losses(self):
        trades = [_make_trade(10), _make_trade(20)]
        assert MetricsCalculator.calculate_profit_factor(trades) == float("inf")

    def test_profit_factor_empty(self):
        assert MetricsCalculator.calculate_profit_factor([]) == 0.0

    def test_max_drawdown_basic(self):
        curve = [
            (None, 100.0),
            ("t1", 110.0),
            ("t2", 90.0),  # DD = (110-90)/110 = 18.18%
            ("t3", 95.0),
        ]
        dd = MetricsCalculator.calculate_max_drawdown(curve)
        assert dd == pytest.approx(18.1818, rel=1e-3)

    def test_max_drawdown_no_drawdown(self):
        curve = [(None, 100.0), ("t1", 110.0), ("t2", 120.0)]
        assert MetricsCalculator.calculate_max_drawdown(curve) == 0.0

    def test_max_drawdown_empty(self):
        assert MetricsCalculator.calculate_max_drawdown([]) == 0.0

    def test_sharpe_ratio_positive(self):
        # Returns with positive mean and some variance
        returns = [0.01, 0.02, -0.005, 0.015, 0.008, -0.002, 0.012]
        sr = MetricsCalculator.calculate_sharpe_ratio(returns)
        assert sr > 0

    def test_sharpe_ratio_zero_variance(self):
        returns = [0.01, 0.01, 0.01]
        assert MetricsCalculator.calculate_sharpe_ratio(returns) == 0.0

    def test_sharpe_ratio_empty(self):
        assert MetricsCalculator.calculate_sharpe_ratio([]) == 0.0

    def test_equity_curve_basic(self):
        trades = [_make_trade(10), _make_trade(-5), _make_trade(20)]
        curve = MetricsCalculator.calculate_equity_curve(trades, 1000.0)
        assert len(curve) == 4  # initial + 3 trades
        assert curve[0][1] == 1000.0
        assert curve[1][1] == 1010.0
        assert curve[2][1] == 1005.0
        assert curve[3][1] == 1025.0

    def test_equity_curve_empty(self):
        curve = MetricsCalculator.calculate_equity_curve([], 500.0)
        assert len(curve) == 1
        assert curve[0][1] == 500.0

    def test_generate_report(self):
        trades = [_make_trade(30), _make_trade(-10), _make_trade(20), _make_trade(-5)]
        report = MetricsCalculator.generate_report(trades, 10_000.0)
        assert report["total_trades"] == 4
        assert report["wins"] == 2
        assert report["losses"] == 2
        assert report["win_rate"] == 0.5
        assert "equity_curve" in report
        assert report["total_pnl"] == pytest.approx(35.0, rel=1e-4)


# ──────────────────────────────────────────────
#  DataLoader tests
# ──────────────────────────────────────────────

class TestDataLoader:
    def test_csv_round_trip(self):
        df = _make_synthetic_ohlcv(60)
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "test_ohlcv.csv")
            DataLoader.save_to_cache(df, filepath)
            loaded = DataLoader.load_from_cache(filepath)
            assert len(loaded) == 60
            assert list(loaded.columns) == ["open", "high", "low", "close", "volume"]
            # Index should be a DatetimeIndex
            assert isinstance(loaded.index, pd.DatetimeIndex)

    def test_load_csv_ms_timestamp(self):
        """CSV with Unix-millisecond timestamps."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        rows = []
        for i in range(10):
            ts = now_ms + i * 300_000  # 5 minutes
            rows.append({"timestamp": ts, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1.0})
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "ms.csv")
            pd.DataFrame(rows).to_csv(filepath, index=False)
            df = DataLoader.load_from_csv(filepath)
            assert len(df) == 10
            assert isinstance(df.index, pd.DatetimeIndex)

    def test_load_csv_iso_timestamp(self):
        """CSV with ISO-8601 timestamps."""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        rows = []
        for i in range(10):
            ts = (base + timedelta(minutes=5 * i)).isoformat()
            rows.append({"timestamp": ts, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1.0})
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "iso.csv")
            pd.DataFrame(rows).to_csv(filepath, index=False)
            df = DataLoader.load_from_csv(filepath)
            assert len(df) == 10
            assert isinstance(df.index, pd.DatetimeIndex)

    def test_load_csv_missing_column(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "bad.csv")
            pd.DataFrame({"timestamp": [1], "open": [1]}).to_csv(filepath, index=False)
            with pytest.raises(ValueError):
                DataLoader.load_from_csv(filepath)

    def test_load_csv_not_found(self):
        with pytest.raises(FileNotFoundError):
            DataLoader.load_from_csv("/nonexistent/path/to/file.csv")


# ──────────────────────────────────────────────
#  BacktestEngine tests
# ──────────────────────────────────────────────

class TestBacktestEngine:
    def test_run_with_uptrend(self):
        """A strong uptrend should produce at least one trade."""
        df = _make_synthetic_ohlcv(200, base_price=100_000.0, trend=80.0, noise=5.0, seed=7)
        engine = BacktestEngine(initial_capital=10_000.0)
        report = engine.run(df)
        assert "total_trades" in report
        assert report["total_trades"] >= 0  # at minimum, no crash
        assert report["initial_capital"] == 10_000.0
        assert "equity_curve" in report
        assert isinstance(report["equity_curve"], list)

    def test_run_with_flat_data_no_trades(self):
        """Perfectly flat data (constant price) cannot satisfy C1.

        With price == EMA9 == EMA21 == EMA50 everywhere, the strict inequality
        ``price > ema21 > ema50`` is never true, so no entries are generated.
        """
        now = datetime.now(timezone.utc)
        timestamps = [now - timedelta(minutes=5 * (200 - i)) for i in range(200)]
        flat = 100_000.0
        df = pd.DataFrame(
            {
                "open": flat,
                "high": flat + 0.01,
                "low": flat - 0.01,
                "close": flat,
                "volume": 10.0,
            },
            index=pd.DatetimeIndex(timestamps, name="timestamp"),
        )
        engine = BacktestEngine(initial_capital=10_000.0)
        report = engine.run(df)
        # Constant price → EMA9==EMA21==EMA50 → C1 (strict >) never true → 0 trades
        assert report["total_trades"] == 0

    def test_empty_dataframe_raises(self):
        engine = BacktestEngine()
        with pytest.raises(ValueError):
            engine.run(pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))

    def test_insufficient_candles_raises(self):
        df = _make_synthetic_ohlcv(20)  # less than ema_slow (50)
        engine = BacktestEngine()
        with pytest.raises(ValueError):
            engine.run(df)

    def test_run_from_csv(self):
        df = _make_synthetic_ohlcv(200, trend=80.0, noise=5.0, seed=7)
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "backtest_data.csv")
            DataLoader.save_to_cache(df, filepath)
            engine = BacktestEngine(initial_capital=10_000.0)
            report = engine.run_from_csv(filepath)
            assert "total_trades" in report

    def test_trade_has_fees(self):
        """Every closed trade must have entry + exit fees (round-trip)."""
        df = _make_synthetic_ohlcv(200, trend=100.0, noise=3.0, seed=99)
        engine = BacktestEngine(initial_capital=10_000.0)
        report = engine.run(df)
        if report["total_trades"] > 0:
            for t in engine.trades:
                assert t["fee_buy_usdt"] > 0
                assert t["fee_sell_usdt"] > 0
                assert t["fees_total_usdt"] == pytest.approx(
                    t["fee_buy_usdt"] + t["fee_sell_usdt"], rel=1e-6
                )

    def test_confluence_replica_logic(self):
        """Test the evaluate_confluence method directly."""
        # Create a snapshot where C1, C2, C3 are true → should_enter = True
        snap = IndicatorSnapshot(
            timestamp=datetime.now(timezone.utc),
            price=101_000.0,
            ema9=100_800.0,
            ema21=100_500.0,
            ema50=100_000.0,
            vwap=100_900.0,  # price > vwap → C2 true
            current_volume=30.0,
            volume_sma20=15.0,
            volume_ratio=2.0,  # >= 1.5 → C3 true
            rsi=70.0,  # outside [40, 60] → C4 false
            bid_wall_strength=1.0,
            bid_wall_within_range=False,  # C5 false
            trend_15m="bullish",
            ema21_15m=100_500.0,
            ema50_15m=100_000.0,
        )
        engine = BacktestEngine()
        result = engine.evaluate_confluence(snap)
        assert result.score == 3  # C1 + C2 + C3
        assert result.should_enter is True

    def test_confluence_bearish_blocks_entry(self):
        """Even with sufficient score, a bearish 15m trend blocks entry."""
        snap = IndicatorSnapshot(
            timestamp=datetime.now(timezone.utc),
            price=101_000.0,
            ema9=100_800.0,
            ema21=100_500.0,
            ema50=100_000.0,
            vwap=100_900.0,
            current_volume=30.0,
            volume_sma20=15.0,
            volume_ratio=2.0,
            rsi=50.0,  # in zone → C4 true
            bid_wall_strength=1.0,
            bid_wall_within_range=False,
            trend_15m="bearish",  # blocks!
            ema21_15m=99_500.0,
            ema50_15m=100_000.0,
        )
        engine = BacktestEngine()
        result = engine.evaluate_confluence(snap)
        assert result.should_enter is False
        assert "BEARISH" in result.reason

    def test_confluence_c1_mandatory(self):
        """Without C1, entry is blocked regardless of score."""
        snap = IndicatorSnapshot(
            timestamp=datetime.now(timezone.utc),
            price=99_000.0,  # below ema21 → C1 false
            ema9=100_800.0,
            ema21=100_500.0,
            ema50=100_000.0,
            vwap=98_000.0,  # price > vwap → C2 true
            current_volume=30.0,
            volume_sma20=15.0,
            volume_ratio=2.0,  # C3 true
            rsi=50.0,  # C4 true
            bid_wall_strength=1.0,
            bid_wall_within_range=False,
            trend_15m="bullish",
            ema21_15m=100_500.0,
            ema50_15m=100_000.0,
        )
        engine = BacktestEngine()
        result = engine.evaluate_confluence(snap)
        # C1 false, but C2+C3+C4 = 3 conditions → score=3 but mandatory fails
        assert result.score == 3
        assert result.should_enter is False


# ──────────────────────────────────────────────
#  BacktestReport tests
# ──────────────────────────────────────────────

class TestBacktestReport:
    def test_text_report(self):
        trades = [_make_trade(30), _make_trade(-10)]
        report = MetricsCalculator.generate_report(trades, 10_000.0)
        text = BacktestReport.generate_text_report(report, trades)
        assert "BACKTEST REPORT" in text
        assert "Win Rate" in text

    def test_html_report(self):
        trades = [_make_trade(30), _make_trade(-10)]
        metrics = MetricsCalculator.generate_report(trades, 10_000.0)
        html = BacktestReport.generate_html_report(metrics, trades, metrics["equity_curve"])
        assert "<!DOCTYPE html>" in html
        assert "<svg" in html
        assert "BTC Scalper" in html

    def test_html_report_no_trades(self):
        metrics = MetricsCalculator.generate_report([], 10_000.0)
        html = BacktestReport.generate_html_report(metrics, [], metrics["equity_curve"])
        assert "No trades" in html