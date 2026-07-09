"""
backtesting/engine.py
=====================
BacktestEngine — synchronous, candle-by-candle backtesting of the BTC Spot
Scalper confluence strategy.

Design notes
------------
* **No async, no ccxt.pro** — data is loaded via :class:`DataLoader` (sync
  REST or CSV) and iterated with a plain ``for`` loop.
* **Confluence logic is a faithful replica** of
  ``StrategyEngine.evaluate_confluence`` in ``strategy/engine.py``.  We cannot
  import that method directly because ``StrategyEngine.__init__`` wires up a
  live ccxt.pro exchange connection, which we don't want in a backtest.
* **RiskManager** is used for SL/TP and position sizing.  An in-memory SQLite
  database is created so ``RiskManager`` can be instantiated without touching
  the real database.
* **Fees** are applied on both entry and exit using
  ``core.config.EFFECTIVE_MAKER_FEE`` (round-trip = ``EFFECTIVE_MAKER_FEE * 2``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import (
    EFFECTIVE_MAKER_FEE,
    ROUND_TRIP_FEE,
    RISK,
    STRATEGY,
    TradingMode,
)
from core.models import Base, build_session_factory
from risk.manager import RiskManager
from strategy.indicators import (
    compute_emas,
    compute_volume_sma,
    compute_rsi,
    compute_vwap_session,
    analyze_order_book,
    compute_trend_context,
)

from backtesting.data_loader import DataLoader
from backtesting.metrics import MetricsCalculator

logger = logging.getLogger("backtest")


# ──────────────────────────────────────────────
#  Data structures (mirror strategy.engine for fidelity)
# ──────────────────────────────────────────────

@dataclass
class IndicatorSnapshot:
    """Mirrors ``strategy.engine.IndicatorSnapshot`` for backtest evaluation."""

    timestamp: datetime
    price: float
    ema9: float
    ema21: float
    ema50: float
    vwap: float
    current_volume: float
    volume_sma20: float
    volume_ratio: float
    rsi: float
    bid_wall_strength: float
    bid_wall_within_range: bool
    trend_15m: str
    ema21_15m: float
    ema50_15m: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class ConfluenceResult:
    """Mirrors ``strategy.engine.ConfluenceResult``."""

    score: int
    conditions: dict[str, bool] = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    should_enter: bool = False
    reason: str = ""


# ──────────────────────────────────────────────
#  Backtest Engine
# ──────────────────────────────────────────────

class BacktestEngine:
    """Synchronous backtesting engine.

    Parameters
    ----------
    initial_capital
        Simulated starting balance in USDT.
    symbol
        Trading symbol (default ``BTC/USDT``).
    """

    def __init__(
        self,
        initial_capital: float = 10_000.0,
        symbol: str = "BTC/USDT",
    ) -> None:
        self.initial_capital = float(initial_capital)
        self.symbol = symbol

        # Simulated balance / state
        self.balance: float = float(initial_capital)
        self.trades: list[dict] = []
        self.equity_curve: list[tuple] = []

        # Build an in-memory SQLite DB so RiskManager can be instantiated.
        self._mem_engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            echo=False,
            future=True,
        )
        Base.metadata.create_all(self._mem_engine)
        self._db_factory: sessionmaker = build_session_factory(self._mem_engine)
        self.risk_manager = RiskManager(self._db_factory)

        # Current open position (if any)
        self._open_position: Optional[dict] = None

    # ── public API ──────────────────────────────────────────────

    def run(self, df_5m: pd.DataFrame) -> dict:
        """Run the backtest over *df_5m* (5-minute OHLCV data).

        Parameters
        ----------
        df_5m
            DataFrame indexed by tz-aware ``timestamp`` with columns
            ``open, high, low, close, volume``.

        Returns
        -------
        dict
            Full metrics report (see :meth:`MetricsCalculator.generate_report`).
        """
        if df_5m.empty:
            raise ValueError("Cannot run backtest on empty DataFrame")

        # Reset state
        self.balance = float(self.initial_capital)
        self.trades = []
        self.equity_curve = []
        self._open_position = None

        # Pre-compute 15-minute resampled data for trend context.
        df_15m = self._resample_15m(df_5m)

        # We need at least ema_slow (50) candles for indicators.
        min_candles = STRATEGY.ema_slow
        if len(df_5m) < min_candles + 1:
            raise ValueError(
                f"Need at least {min_candles + 1} candles, got {len(df_5m)}"
            )

        # Iterate candle-by-candle.
        for i in range(min_candles, len(df_5m)):
            window_5m = df_5m.iloc[: i + 1]
            current_candle = df_5m.iloc[i]
            current_ts = df_5m.index[i]

            # 1. Manage any open position first (check SL/TP on this candle).
            if self._open_position is not None:
                self._manage_open_position(current_candle, current_ts)

            # 2. If still in a position, skip new entries (max 1 concurrent).
            if self._open_position is not None:
                continue

            # 3. Compute indicators and evaluate confluence.
            snap = self._compute_snapshot(window_5m, df_15m, current_ts)
            result = self.evaluate_confluence(snap)

            if result.should_enter:
                self._enter_trade(snap, result)

        # Close any remaining open position at the last close.
        if self._open_position is not None:
            last_candle = df_5m.iloc[-1]
            last_ts = df_5m.index[-1]
            self._close_trade(
                self._open_position,
                exit_price=float(last_candle["close"]),
                exit_time=last_ts,
                exit_reason="backtest_end",
            )
            self._open_position = None

        # Build metrics + equity curve.
        report = MetricsCalculator.generate_report(self.trades, self.initial_capital)
        self.equity_curve = report.get("equity_curve", [])
        return report

    def run_from_csv(self, filepath: str) -> dict:
        """Convenience: load CSV then :meth:`run`."""
        df = DataLoader.load_from_csv(filepath)
        return self.run(df)

    # ── confluence (faithful replica of StrategyEngine.evaluate_confluence) ─

    def evaluate_confluence(self, snap: IndicatorSnapshot) -> ConfluenceResult:
        """Evaluate the 5-condition confluence checklist.

        This is a byte-for-byte faithful replica of
        ``StrategyEngine.evaluate_confluence``.  The only difference is that
        ``bid_wall_within_range`` is always ``False`` in backtesting because we
        have no historical order-book data, so condition C5 is never met.
        """
        conditions: dict[str, bool] = {}
        details: dict = {}

        # C1 — bullish trend: price > EMA21 > EMA50
        c1 = snap.price > snap.ema21 and snap.ema21 > snap.ema50
        conditions["c1_bullish_trend"] = c1
        details["c1"] = {"price": snap.price, "ema21": snap.ema21, "ema50": snap.ema50}

        # C2 — VWAP position
        vwap_dist = abs(snap.price - snap.vwap) / snap.vwap if snap.vwap > 0 else 1.0
        above_vwap = snap.price > snap.vwap
        retest = vwap_dist <= 0.0015 and snap.price > snap.ema9
        c2 = above_vwap or retest
        conditions["c2_vwap_position"] = c2
        details["c2"] = {"vwap": snap.vwap, "dist_pct": round(vwap_dist * 100, 4)}

        # C3 — volume spike
        c3 = snap.volume_ratio >= STRATEGY.volume_spike_multiplier
        conditions["c3_volume_spike"] = c3
        details["c3"] = {"ratio": round(snap.volume_ratio, 3)}

        # C4 — RSI zone
        c4 = STRATEGY.rsi_lower <= snap.rsi <= STRATEGY.rsi_upper
        conditions["c4_rsi_zone"] = c4
        details["c4"] = {"rsi": round(snap.rsi, 2)}

        # C5 — bid wall (always False in backtest: no order-book data)
        c5 = snap.bid_wall_within_range
        conditions["c5_bid_wall"] = c5
        details["c5"] = {"strength": round(snap.bid_wall_strength, 3)}

        score = sum(1 for v in conditions.values() if v)
        mandatory = conditions["c1_bullish_trend"]
        should_enter = score >= STRATEGY.min_confluence_score and mandatory

        if should_enter:
            reason = f"Entry: {score}/5 conditions, C1 satisfied"
        elif not mandatory:
            reason = f"Skip: C1 not met ({score}/5)"
        else:
            reason = f"Skip: {score}/5 (need {STRATEGY.min_confluence_score})"

        if snap.trend_15m == "bearish":
            should_enter = False
            reason += " | 15m BEARISH — blocked"

        return ConfluenceResult(
            score=score,
            conditions=conditions,
            details=details,
            should_enter=should_enter,
            reason=reason,
        )

    # ── private helpers ─────────────────────────────────────────

    @staticmethod
    def _resample_15m(df_5m: pd.DataFrame) -> pd.DataFrame:
        """Resample 5-minute OHLCV to 15-minute candles."""
        if df_5m.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df_15m = df_5m.resample("15min").agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        ).dropna()
        return df_15m

    def _compute_snapshot(
        self,
        window_5m: pd.DataFrame,
        df_15m: pd.DataFrame,
        current_ts: pd.Timestamp,
    ) -> IndicatorSnapshot:
        """Compute all indicators for the current candle."""
        # EMAs
        ema9, ema21, ema50 = compute_emas(
            window_5m,
            STRATEGY.ema_fast,
            STRATEGY.ema_mid,
            STRATEGY.ema_slow,
        )
        # Volume
        cv, vs20, vr = compute_volume_sma(window_5m, STRATEGY.volume_sma_period)
        # RSI
        rsi = compute_rsi(window_5m, STRATEGY.rsi_period)
        # VWAP (session-based)
        session_date = current_ts.strftime("%Y-%m-%d")
        vwap = compute_vwap_session(window_5m, session_date)
        # Order book — not available in backtest → always (1.0, False)
        strength, wall_in_range = analyze_order_book(
            None,
            STRATEGY.order_book_depth_pct,
            STRATEGY.bid_wall_min_pct,
            STRATEGY.bid_wall_max_pct,
        )
        # 15m trend context
        window_15m = df_15m.loc[:current_ts]
        if window_15m.empty:
            trend, e21_15, e50_15 = "neutral", 0.0, 0.0
        else:
            trend, e21_15, e50_15 = compute_trend_context(
                window_15m,
                STRATEGY.ema_mid,
                STRATEGY.ema_slow,
            )

        price = float(window_5m["close"].iloc[-1])
        ts = current_ts.to_pydatetime()

        return IndicatorSnapshot(
            timestamp=ts,
            price=price,
            ema9=ema9,
            ema21=ema21,
            ema50=ema50,
            vwap=vwap,
            current_volume=cv,
            volume_sma20=vs20,
            volume_ratio=vr,
            rsi=rsi,
            bid_wall_strength=strength,
            bid_wall_within_range=wall_in_range,
            trend_15m=trend,
            ema21_15m=e21_15,
            ema50_15m=e50_15,
        )

    def _enter_trade(self, snap: IndicatorSnapshot, result: ConfluenceResult) -> None:
        """Open a simulated long position at the current candle close."""
        entry_price = snap.price

        # SL/TP via RiskManager
        sl_tp = self.risk_manager.calculate_sl_tp(entry_price, recent_swing_low=None)
        sl_price = sl_tp["sl_price"]
        tp_price = sl_tp["tp_price"]

        # Position sizing via RiskManager (use simulated balance)
        pos = self.risk_manager.calculate_position_size(
            available_usdt=self.balance,
            entry_price=entry_price,
            sl_price=sl_price,
        )
        size_usdt = pos["size_usdt"]
        qty_btc = pos["quantity_btc"]

        if qty_btc <= 0 or size_usdt <= 0:
            return

        # Entry fee
        fee_buy = size_usdt * EFFECTIVE_MAKER_FEE

        self._open_position = {
            "symbol": self.symbol,
            "entry_time": snap.timestamp,
            "entry_price": entry_price,
            "quantity_btc": qty_btc,
            "position_size_usdt": size_usdt,
            "stop_loss_price": sl_price,
            "take_profit_price": tp_price,
            "trailing_sl_price": sl_price,  # current trailing SL
            "original_sl_price": sl_price,
            "breakeven_price": sl_tp["breakeven_price"],
            "fee_buy_usdt": fee_buy,
            "fee_sell_usdt": 0.0,
            "fees_total_usdt": fee_buy,  # will add exit fee on close
            "confluence_score": result.score,
            "conditions_met": [k for k, v in result.conditions.items() if v],
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "gross_pnl_usdt": 0.0,
            "net_pnl_usdt": 0.0,
            "return_pct": 0.0,
        }

    def _manage_open_position(self, candle: pd.Series, ts: pd.Timestamp) -> None:
        """Check SL/TP/trailing for the open position against *candle* high/low."""
        pos = self._open_position
        if pos is None:
            return

        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])

        current_sl = pos["trailing_sl_price"]
        tp = pos["take_profit_price"]

        # ── Trailing stop update ──
        # Use the candle close as the "current_price" for trailing evaluation
        # (conservative: we only trail based on close, not intrabar high).
        new_sl = self.risk_manager.update_trailing_stop(
            entry_price=pos["entry_price"],
            current_price=close,
            current_sl=current_sl,
            breakeven_price=pos["breakeven_price"],
            original_sl_price=pos["original_sl_price"],
        )
        if new_sl is not None and new_sl > current_sl:
            pos["trailing_sl_price"] = new_sl
            current_sl = new_sl

        # ── Exit checks (SL first = conservative) ──
        if low <= current_sl:
            # Stop loss hit
            self._close_trade(
                pos,
                exit_price=float(current_sl),
                exit_time=ts,
                exit_reason="stop_loss",
            )
            self._open_position = None
            return

        if high >= tp:
            # Take profit hit
            self._close_trade(
                pos,
                exit_price=float(tp),
                exit_time=ts,
                exit_reason="take_profit",
            )
            self._open_position = None
            return

        # If trailing SL moved above original SL and we already checked, that's fine.
        # Position remains open.

    def _close_trade(
        self,
        pos: dict,
        exit_price: float,
        exit_time,
        exit_reason: str,
    ) -> None:
        """Finalise a trade: compute PnL, fees, and update balance."""
        qty = pos["quantity_btc"]
        entry_price = pos["entry_price"]
        size_usdt = pos["position_size_usdt"]

        # Exit value
        exit_value = qty * exit_price

        # Exit fee
        fee_sell = exit_value * EFFECTIVE_MAKER_FEE

        # Gross PnL (USDT)
        gross_pnl = (exit_price - entry_price) * qty

        # Net PnL = gross - total fees
        total_fees = pos["fee_buy_usdt"] + fee_sell
        net_pnl = gross_pnl - total_fees

        # Return % on position
        return_pct = (net_pnl / size_usdt * 100) if size_usdt > 0 else 0.0

        pos["exit_time"] = exit_time
        pos["exit_price"] = round(exit_price, 2)
        pos["exit_reason"] = exit_reason
        pos["fee_sell_usdt"] = round(fee_sell, 6)
        pos["fees_total_usdt"] = round(total_fees, 6)
        pos["gross_pnl_usdt"] = round(gross_pnl, 6)
        pos["net_pnl_usdt"] = round(net_pnl, 6)
        pos["return_pct"] = round(return_pct, 4)

        # Update simulated balance
        self.balance += net_pnl

        # Record trade
        self.trades.append(pos)

        # Record in risk manager for circuit-breaker tracking
        self.risk_manager.record_trade_result(net_pnl)