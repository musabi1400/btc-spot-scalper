"""
backtesting/metrics.py
======================
Performance-metric calculations for backtesting results.

All methods are ``@staticmethod`` so they can be used without instantiation,
but the class also works as a namespace for IDE auto-completion.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


# Default annualisation factor for 5-minute candle returns.
# 365 days * 24 h * 12 (five-min periods per hour) = 105 120 periods/year.
_DEFAULT_PERIODS_PER_YEAR: int = 365 * 24 * 12


class MetricsCalculator:
    """Static helpers that turn a list of trade dicts into performance metrics."""

    # ── win rate ────────────────────────────────────────────────

    @staticmethod
    def calculate_win_rate(trades: Sequence[dict]) -> float:
        """Fraction of trades that were profitable (0.0 – 1.0).

        Parameters
        ----------
        trades
            Sequence of trade dicts; each must contain ``net_pnl_usdt``.
        """
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.get("net_pnl_usdt", 0) > 0)
        return wins / len(trades)

    # ── profit factor ───────────────────────────────────────────

    @staticmethod
    def calculate_profit_factor(trades: Sequence[dict]) -> float:
        """Gross profit / gross loss.

        Returns ``float('inf')`` when there are no losing trades, and
        ``0.0`` when *trades* is empty or there is no profit.
        """
        if not trades:
            return 0.0
        gross_profit = sum(t.get("net_pnl_usdt", 0) for t in trades if t.get("net_pnl_usdt", 0) > 0)
        gross_loss = abs(sum(t.get("net_pnl_usdt", 0) for t in trades if t.get("net_pnl_usdt", 0) < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    # ── max drawdown ────────────────────────────────────────────

    @staticmethod
    def calculate_max_drawdown(equity_curve: Sequence[tuple]) -> float:
        """Maximum drawdown expressed as a **percentage** (e.g. 12.5 for 12.5 %).

        Parameters
        ----------
        equity_curve
            List of ``(timestamp, equity)`` tuples ordered chronologically.
        """
        if not equity_curve:
            return 0.0
        equities = [e for _, e in equity_curve]
        peak = equities[0]
        max_dd = 0.0
        for eq in equities:
            if eq > peak:
                peak = eq
            dd_pct = (peak - eq) / peak * 100 if peak > 0 else 0.0
            if dd_pct > max_dd:
                max_dd = dd_pct
        return max_dd

    # ── Sharpe ratio ────────────────────────────────────────────

    @staticmethod
    def calculate_sharpe_ratio(
        returns: Sequence[float],
        periods_per_year: int = _DEFAULT_PERIODS_PER_YEAR,
    ) -> float:
        """Annualised Sharpe ratio (risk-free rate = 0).

        Parameters
        ----------
        returns
            Sequence of per-period returns (e.g. equity-period returns).
        periods_per_year
            Number of return periods in one year (default: 5-minute candles).
        """
        if not returns or len(returns) < 2:
            return 0.0
        arr = np.asarray(returns, dtype=float)
        std = arr.std(ddof=1)
        if std == 0:
            return 0.0
        mean = arr.mean()
        return float(mean / std * np.sqrt(periods_per_year))

    # ── equity curve ────────────────────────────────────────────

    @staticmethod
    def calculate_equity_curve(
        trades: Sequence[dict],
        initial_capital: float,
    ) -> list[tuple]:
        """Build an equity curve from a chronologically-sorted trade list.

        Returns a list of ``(exit_time_iso, equity)`` tuples.  The curve starts
        with ``(None, initial_capital)`` so the initial capital is visible.
        """
        curve: list[tuple] = [(None, float(initial_capital))]
        equity = float(initial_capital)
        for t in trades:
            equity += float(t.get("net_pnl_usdt", 0))
            ts = t.get("exit_time")
            if ts is None:
                ts_str = None
            elif hasattr(ts, "isoformat"):
                ts_str = ts.isoformat()
            else:
                ts_str = str(ts)
            curve.append((ts_str, round(equity, 6)))
        return curve

    # ── all-in-one report ──────────────────────────────────────

    @staticmethod
    def generate_report(
        trades: Sequence[dict],
        initial_capital: float,
    ) -> dict:
        """Return every supported metric in a single dictionary."""
        equity_curve = MetricsCalculator.calculate_equity_curve(trades, initial_capital)

        # Period returns derived from the equity curve (skip the initial point).
        equities = [e for _, e in equity_curve]
        returns: list[float] = []
        for i in range(1, len(equities)):
            prev, cur = equities[i - 1], equities[i]
            if prev > 0:
                returns.append((cur - prev) / prev)
            else:
                returns.append(0.0)

        total_pnl = sum(float(t.get("net_pnl_usdt", 0)) for t in trades)
        total_fees = sum(float(t.get("fees_total_usdt", 0)) for t in trades)
        wins = [t for t in trades if t.get("net_pnl_usdt", 0) > 0]
        losses = [t for t in trades if t.get("net_pnl_usdt", 0) <= 0]
        avg_win = (
            sum(float(t["net_pnl_usdt"]) for t in wins) / len(wins) if wins else 0.0
        )
        avg_loss = (
            sum(float(t["net_pnl_usdt"]) for t in losses) / len(losses) if losses else 0.0
        )

        final_equity = equities[-1] if equities else initial_capital
        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(MetricsCalculator.calculate_win_rate(trades), 4),
            "profit_factor": round(MetricsCalculator.calculate_profit_factor(trades), 4),
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(total_fees, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "max_drawdown_pct": round(MetricsCalculator.calculate_max_drawdown(equity_curve), 4),
            "sharpe_ratio": round(
                MetricsCalculator.calculate_sharpe_ratio(returns), 4
            ),
            "initial_capital": round(float(initial_capital), 2),
            "final_equity": round(float(final_equity), 2),
            "return_pct": round(
                ((final_equity - initial_capital) / initial_capital * 100)
                if initial_capital > 0
                else 0.0,
                4,
            ),
            "equity_curve": equity_curve,
        }