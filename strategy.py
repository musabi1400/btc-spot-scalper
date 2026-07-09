"""
strategy.py
===========
Core strategy engine for BTC/USDT Spot Scalping.

Responsibilities:
  1. Fetch live OHLCV data via ccxt.pro async methods (REST + WebSocket).
  2. Compute technical indicators (VWAP, EMAs, Volume SMA, RSI, Order-Book depth).
  3. Evaluate the 5-condition confluence checklist for Long entries.
  4. Provide the 15m trend context.

All public methods are async and safe to call from the main event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import ccxt.pro as ccxtpro
import numpy as np
import pandas as pd
import pandas_ta as ta

from config import STRATEGY, SYMBOL, TIMEFRAME_EXECUTION, TIMEFRAME_CONTEXT, TradingMode

logger = logging.getLogger("strategy")


# ──────────────────────────────────────────────
#  Data Structures
# ──────────────────────────────────────────────

@dataclass
class IndicatorSnapshot:
    """Immutable snapshot of all indicator values at a point in time."""
    timestamp: datetime
    price: float

    # EMAs (5m)
    ema9: float
    ema21: float
    ema50: float

    # VWAP (session)
    vwap: float

    # Volume
    current_volume: float
    volume_sma20: float
    volume_ratio: float          # current_volume / volume_sma20

    # RSI
    rsi: float

    # Order book
    bid_wall_strength: float     # ratio of bids within range vs asks
    bid_wall_within_range: bool  # is there a thick bid wall 0.1%-0.3% below?

    # 15m context
    trend_15m: str               # "bullish" / "bearish" / "neutral"
    ema21_15m: float
    ema50_15m: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class ConfluenceResult:
    """Result of the 5-condition confluence check."""
    score: int                          # how many conditions are TRUE (0-5)
    conditions: dict[str, bool] = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    should_enter: bool = False          # score >= min_confluence AND mandatory condition met
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "conditions": self.conditions,
            "details": self.details,
            "should_enter": self.should_enter,
            "reason": self.reason,
        }


# ──────────────────────────────────────────────
#  Strategy Engine
# ──────────────────────────────────────────────

class StrategyEngine:
    """
    Async strategy engine that fetches data from Binance via ccxt.pro
    and evaluates the scalping confluence rules.

    Usage:
        engine = StrategyEngine(api_key, api_secret, mode=TradingMode.DEMO)
        await engine.start()                 # initialise + warm up indicators
        snapshot = await engine.get_snapshot()
        confluence = engine.evaluate_confluence(snapshot)
        await engine.stop()
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        mode: TradingMode = TradingMode.DEMO,
    ):
        self.mode = mode
        self.api_key = api_key
        self.api_secret = api_secret

        # ccxt.pro exchange instance
        self.exchange: Optional[ccxtpro.binance] = None

        # Cached OHLCV data
        self._ohlcv_5m: pd.DataFrame = pd.DataFrame()
        self._ohlcv_15m: pd.DataFrame = pd.DataFrame()

        # VWAP session tracking
        self._vwap_session_date: Optional[str] = None
        self._vwap_cum_pv: float = 0.0   # cumulative price × volume
        self._vwap_cum_vol: float = 0.0  # cumulative volume

        # Order book cache
        self._order_book: Optional[dict] = None

        # Running flag
        self._running = False
        self._ws_tasks: list[asyncio.Task] = []

    # ──────────────────────────────────────────────
    #  Lifecycle
    # ──────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise the exchange connection and warm up indicator data."""
        if self._running:
            logger.warning("StrategyEngine already running")
            return

        self.exchange = ccxtpro.binance({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        })

        # Testnet sandbox mode
        if self.mode == TradingMode.DEMO:
            self.exchange.set_sandbox_mode(True)
            logger.info("Exchange initialised in TESTNET (sandbox) mode")
        else:
            logger.info("Exchange initialised in LIVE (production) mode")

        # Warm up: fetch historical OHLCV for both timeframes
        await self._warmup_ohlcv()

        # Start WebSocket watchers
        self._running = True
        self._ws_tasks.append(asyncio.create_task(self._watch_ohlcv_5m()))
        self._ws_tasks.append(asyncio.create_task(self._watch_ohlcv_15m()))
        self._ws_tasks.append(asyncio.create_task(self._watch_order_book()))

        logger.info("StrategyEngine started — WS streams active")

    async def stop(self) -> None:
        """Cancel all background tasks and close the exchange connection."""
        self._running = False
        for task in self._ws_tasks:
            task.cancel()
        self._ws_tasks.clear()
        if self.exchange:
            await self.exchange.close()
            self.exchange = None
        logger.info("StrategyEngine stopped")

    # ──────────────────────────────────────────────
    #  OHLCV fetching
    # ──────────────────────────────────────────────

    async def _warmup_ohlcv(self) -> None:
        """Fetch historical candles to populate indicator buffers."""
        # 5m: need at least 200 candles for EMA50 + volume SMA20 + RSI14
        ohlcv_5m = await self.exchange.fetch_ohlcv(
            SYMBOL, TIMEFRAME_EXECUTION, limit=STRATEGY.ohlcv_buffer_5m
        )
        self._ohlcv_5m = self._ohlcv_to_df(ohlcv_5m)

        # 15m: 100 candles for context trend
        ohlcv_15m = await self.exchange.fetch_ohlcv(
            SYMBOL, TIMEFRAME_CONTEXT, limit=STRATEGY.ohlcv_buffer_15m
        )
        self._ohlcv_15m = self._ohlcv_to_df(ohlcv_15m)

        # Initialise VWAP session from today's 5m data
        self._reset_vwap_session()
        self._update_vwap_from_df(self._ohlcv_5m)

        logger.info(
            "Warmup complete — 5m: %d candles, 15m: %d candles",
            len(self._ohlcv_5m), len(self._ohlcv_15m),
        )

    async def _watch_ohlcv_5m(self) -> None:
        """Watch the 5m OHLCV stream via ccxt.pro WebSocket."""
        while self._running:
            try:
                ohlcv = await self.exchange.watch_ohlcv(SYMBOL, TIMEFRAME_EXECUTION)
                if ohlcv:
                    self._merge_ohlcv(self._ohlcv_5m, ohlcv, TIMEFRAME_EXECUTION)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("5m WS error: %s", e)
                await asyncio.sleep(5)

    async def _watch_ohlcv_15m(self) -> None:
        """Watch the 15m OHLCV stream for trend context."""
        while self._running:
            try:
                ohlcv = await self.exchange.watch_ohlcv(SYMBOL, TIMEFRAME_CONTEXT)
                if ohlcv:
                    self._merge_ohlcv(self._ohlcv_15m, ohlcv, TIMEFRAME_CONTEXT)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("15m WS error: %s", e)
                await asyncio.sleep(5)

    async def _watch_order_book(self) -> None:
        """Watch the order book stream for bid/ask wall analysis."""
        while self._running:
            try:
                ob = await self.exchange.watch_order_book(SYMBOL)
                self._order_book = ob
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("OrderBook WS error: %s", e)
                await asyncio.sleep(5)

    # ──────────────────────────────────────────────
    #  OHLCV helpers
    # ──────────────────────────────────────────────

    @staticmethod
    def _ohlcv_to_df(ohlcv: list[list]) -> pd.DataFrame:
        """Convert ccxt OHLCV list to a typed DataFrame."""
        df = pd.DataFrame(
            ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp").sort_index()
        # Keep only the last N rows to bound memory
        return df

    def _merge_ohlcv(
        self, df: pd.DataFrame, new_candles: list[list], timeframe: str
    ) -> None:
        """
        Merge incoming WS candles into the cached DataFrame.
        Updates the last row if it's the same period, or appends a new row.
        """
        new_df = self._ohlcv_to_df(new_candles)
        if new_df.empty:
            return

        # Combine, deduplicate by index, sort, keep last N
        combined = pd.concat([df, new_df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()

        limit = STRATEGY.ohlcv_buffer_5m if timeframe == TIMEFRAME_EXECUTION else STRATEGY.ohlcv_buffer_15m
        # Use .iloc via a copy to avoid SettingWithCopy warnings
        trimmed = combined.iloc[-limit:].copy()

        # Update the instance attribute (DataFrame is mutable)
        if timeframe == TIMEFRAME_EXECUTION:
            self._ohlcv_5m = trimmed
        else:
            self._ohlcv_15m = trimmed

    # ──────────────────────────────────────────────
    #  VWAP (Session-based)
    # ──────────────────────────────────────────────

    def _reset_vwap_session(self) -> None:
        """Reset cumulative VWAP accumulators (called at session start / new day)."""
        self._vwap_cum_pv = 0.0
        self._vwap_cum_vol = 0.0
        self._vwap_session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _update_vwap_from_df(self, df: pd.DataFrame) -> None:
        """Compute VWAP from the 5m DataFrame for the current UTC session."""
        if df.empty:
            return

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Filter candles belonging to today's UTC session
        today_candles = df[df.index.strftime("%Y-%m-%d") == today_str]
        if today_candles.empty:
            # Fallback: use last 50 candles if session hasn't started
            today_candles = df.tail(50)

        # Typical price = (high + low + close) / 3
        typical_price = (today_candles["high"] + today_candles["low"] + today_candles["close"]) / 3
        pv = (typical_price * today_candles["volume"]).sum()
        vol = today_candles["volume"].sum()

        if vol > 0:
            self._vwap_cum_pv = float(pv)
            self._vwap_cum_vol = float(vol)
            self._vwap_session_date = today_str

    def _compute_vwap(self) -> float:
        """Return the current session VWAP value."""
        if self._vwap_cum_vol > 0:
            return self._vwap_cum_pv / self._vwap_cum_vol
        # Fallback: use last close if no volume
        if not self._ohlcv_5m.empty:
            return float(self._ohlcv_5m["close"].iloc[-1])
        return 0.0

    # ──────────────────────────────────────────────
    #  Indicator Computation
    # ──────────────────────────────────────────────

    def _compute_emas(self, df: pd.DataFrame) -> tuple[float, float, float]:
        """Compute EMA 9, 21, 50 on the close series. Returns (ema9, ema21, ema50)."""
        if df.empty or len(df) < 50:
            last_close = float(df["close"].iloc[-1]) if not df.empty else 0.0
            return last_close, last_close, last_close

        ema9 = ta.ema(df["close"], length=STRATEGY.ema_fast)
        ema21 = ta.ema(df["close"], length=STRATEGY.ema_mid)
        ema50 = ta.ema(df["close"], length=STRATEGY.ema_slow)

        return (
            float(ema9.iloc[-1]),
            float(ema21.iloc[-1]),
            float(ema50.iloc[-1]),
        )

    def _compute_volume_sma(self, df: pd.DataFrame) -> tuple[float, float, float]:
        """
        Compute the 20-period SMA of volume.
        Returns (current_volume, volume_sma20, ratio).
        """
        if df.empty or len(df) < STRATEGY.volume_sma_period:
            current_vol = float(df["volume"].iloc[-1]) if not df.empty else 0.0
            return current_vol, current_vol, 1.0

        vol_sma = df["volume"].rolling(window=STRATEGY.volume_sma_period).mean()
        current_vol = float(df["volume"].iloc[-1])
        sma_val = float(vol_sma.iloc[-1])

        ratio = current_vol / sma_val if sma_val > 0 else 1.0
        return current_vol, sma_val, ratio

    def _compute_rsi(self, df: pd.DataFrame) -> float:
        """Compute RSI(14) on the close series."""
        if df.empty or len(df) < STRATEGY.rsi_period + 1:
            return 50.0  # neutral

        rsi = ta.rsi(df["close"], length=STRATEGY.rsi_period)
        if rsi is not None and not rsi.empty and not np.isnan(rsi.iloc[-1]):
            return float(rsi.iloc[-1])
        return 50.0

    def _analyze_order_book(self) -> tuple[float, bool]:
        """
        Analyze the order book for bid walls within 0.1%-0.3% below current price.

        Returns:
            bid_wall_strength: ratio of bid volume to ask volume in the 1% range.
            bid_wall_within_range: True if a thick bid wall exists 0.1%-0.3% below price.
        """
        if not self._order_book:
            return 1.0, False

        bids = self._order_book.get("bids", [])
        asks = self._order_book.get("asks", [])
        if not bids or not asks:
            return 1.0, False

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid_price = (best_bid + best_ask) / 2

        if mid_price <= 0:
            return 1.0, False

        # Define ranges
        range_upper = mid_price * (1 + STRATEGY.order_book_depth_pct)  # +1 %
        range_lower = mid_price * (1 - STRATEGY.order_book_depth_pct)  # -1 %

        # Bid wall zone: 0.1% to 0.3% below mid-price
        wall_zone_upper = mid_price * (1 - STRATEGY.bid_wall_min_pct)  # 0.1 % below
        wall_zone_lower = mid_price * (1 - STRATEGY.bid_wall_max_pct)  # 0.3 % below

        # Sum volumes
        bid_vol_in_range = sum(
            qty for price, qty in bids if range_lower <= price <= range_upper
        )
        ask_vol_in_range = sum(
            qty for price, qty in asks if range_lower <= price <= range_upper
        )

        bid_wall_strength = bid_vol_in_range / ask_vol_in_range if ask_vol_in_range > 0 else 1.0

        # Check for thick bid wall in the 0.1%-0.3% zone
        bid_wall_vol = sum(
            qty for price, qty in bids if wall_zone_lower <= price <= wall_zone_upper
        )
        # "Thick" means the wall volume is at least 2x the average single-level volume in range
        avg_level_vol = bid_vol_in_range / max(len(bids), 1)
        bid_wall_within_range = bid_wall_vol > (avg_level_vol * 2) if avg_level_vol > 0 else False

        return float(bid_wall_strength), bool(bid_wall_within_range)

    def _compute_15m_trend(self) -> tuple[str, float, float]:
        """
        Determine the 15m trend context.
        Returns (trend_str, ema21_15m, ema50_15m).
        """
        if self._ohlcv_15m.empty or len(self._ohlcv_15m) < 50:
            return "neutral", 0.0, 0.0

        ema21 = ta.ema(self._ohlcv_15m["close"], length=STRATEGY.ema_mid)
        ema50 = ta.ema(self._ohlcv_15m["close"], length=STRATEGY.ema_slow)
        current_close = float(self._ohlcv_15m["close"].iloc[-1])

        e21 = float(ema21.iloc[-1]) if ema21 is not None else current_close
        e50 = float(ema50.iloc[-1]) if ema50 is not None else current_close

        if current_close > e21 > e50:
            trend = "bullish"
        elif current_close < e21 < e50:
            trend = "bearish"
        else:
            trend = "neutral"

        return trend, e21, e50

    # ──────────────────────────────────────────────
    #  Public: Get Full Snapshot
    # ──────────────────────────────────────────────

    async def get_snapshot(self) -> Optional[IndicatorSnapshot]:
        """
        Compute a full indicator snapshot from the latest cached data.
        Returns None if insufficient data.
        """
        if self._ohlcv_5m.empty:
            logger.warning("No 5m data available yet")
            return None

        # Check VWAP session reset
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._vwap_session_date != today:
            self._reset_vwap_session()
            self._update_vwap_from_df(self._ohlcv_5m)

        # EMAs on 5m
        ema9, ema21, ema50 = self._compute_emas(self._ohlcv_5m)

        # Volume
        current_vol, vol_sma20, vol_ratio = self._compute_volume_sma(self._ohlcv_5m)

        # RSI
        rsi = self._compute_rsi(self._ohlcv_5m)

        # VWAP
        vwap = self._compute_vwap()

        # Order book
        bid_wall_strength, bid_wall_within_range = self._analyze_order_book()

        # 15m context
        trend_15m, ema21_15m, ema50_15m = self._compute_15m_trend()

        # Current price (latest 5m close)
        current_price = float(self._ohlcv_5m["close"].iloc[-1])
        latest_ts = self._ohlcv_5m.index[-1].to_pydatetime()

        snapshot = IndicatorSnapshot(
            timestamp=latest_ts,
            price=current_price,
            ema9=ema9,
            ema21=ema21,
            ema50=ema50,
            vwap=vwap,
            current_volume=current_vol,
            volume_sma20=vol_sma20,
            volume_ratio=vol_ratio,
            rsi=rsi,
            bid_wall_strength=bid_wall_strength,
            bid_wall_within_range=bid_wall_within_range,
            trend_15m=trend_15m,
            ema21_15m=ema21_15m,
            ema50_15m=ema50_15m,
        )

        return snapshot

    # ──────────────────────────────────────────────
    #  Confluence Evaluation
    # ──────────────────────────────────────────────

    def evaluate_confluence(self, snap: IndicatorSnapshot) -> ConfluenceResult:
        """
        Evaluate the 5-condition long-entry confluence checklist.

        Condition 1 (MANDATORY): Price > EMA21 AND EMA21 > EMA50 (bullish trend).
        Condition 2: Price > VWAP or successful retest bounce.
        Condition 3: Volume > 1.5× 20-period volume SMA.
        Condition 4: RSI between 40 and 60.
        Condition 5: Order book shows thick bid walls 0.1%-0.3% below price.

        Entry triggers when score >= 3 (min_confluence) AND Condition 1 is TRUE.
        """
        conditions: dict[str, bool] = {}
        details: dict = {}

        # ── Condition 1 (mandatory): Bullish EMA trend ──
        c1 = snap.price > snap.ema21 and snap.ema21 > snap.ema50
        conditions["c1_bullish_trend"] = c1
        details["c1"] = {
            "price": snap.price,
            "ema21": snap.ema21,
            "ema50": snap.ema50,
            "price_gt_ema21": snap.price > snap.ema21,
            "ema21_gt_ema50": snap.ema21 > snap.ema50,
        }

        # ── Condition 2: VWAP position / retest ──
        # "Above VWAP" OR "close to VWAP within 0.15% and bouncing"
        vwap_dist_pct = abs(snap.price - snap.vwap) / snap.vwap if snap.vwap > 0 else 1.0
        above_vwap = snap.price > snap.vwap
        retest_bounce = (
            vwap_dist_pct <= 0.0015
            and snap.price > snap.ema9  # short-term momentum confirms bounce
        )
        c2 = above_vwap or retest_bounce
        conditions["c2_vwap_position"] = c2
        details["c2"] = {
            "price": snap.price,
            "vwap": snap.vwap,
            "vwap_distance_pct": round(vwap_dist_pct * 100, 4),
            "above_vwap": above_vwap,
            "retest_bounce": retest_bounce,
        }

        # ── Condition 3: Volume spike ──
        c3 = snap.volume_ratio >= STRATEGY.volume_spike_multiplier
        conditions["c3_volume_spike"] = c3
        details["c3"] = {
            "current_volume": snap.current_volume,
            "volume_sma20": snap.volume_sma20,
            "ratio": round(snap.volume_ratio, 3),
            "threshold": STRATEGY.volume_spike_multiplier,
        }

        # ── Condition 4: RSI in momentum zone ──
        c4 = STRATEGY.rsi_lower <= snap.rsi <= STRATEGY.rsi_upper
        conditions["c4_rsi_zone"] = c4
        details["c4"] = {
            "rsi": round(snap.rsi, 2),
            "lower": STRATEGY.rsi_lower,
            "upper": STRATEGY.rsi_upper,
        }

        # ── Condition 5: Order book bid wall ──
        c5 = snap.bid_wall_within_range
        conditions["c5_bid_wall"] = c5
        details["c5"] = {
            "bid_wall_strength": round(snap.bid_wall_strength, 3),
            "bid_wall_within_range": snap.bid_wall_within_range,
        }

        # ── Score & Decision ──
        score = sum(1 for v in conditions.values() if v)
        mandatory_met = conditions["c1_bullish_trend"]

        should_enter = score >= STRATEGY.min_confluence_score and mandatory_met

        if should_enter:
            reason = f"Entry signal: {score}/5 conditions met, mandatory C1 satisfied"
        elif not mandatory_met:
            reason = f"Skip: mandatory C1 (bullish trend) not met (score {score}/5)"
        else:
            reason = f"Skip: only {score}/5 conditions met (need {STRATEGY.min_confluence_score})"

        # Add 15m context to reason
        if snap.trend_15m == "bearish":
            should_enter = False
            reason += " | 15m trend is BEARISH — entry blocked"
        elif snap.trend_15m == "neutral":
            reason += " | 15m trend NEUTRAL — proceed with caution"

        return ConfluenceResult(
            score=score,
            conditions=conditions,
            details=details,
            should_enter=should_enter,
            reason=reason,
        )

    # ──────────────────────────────────────────────
    #  Utility: Fetch latest ticker (REST fallback)
    # ──────────────────────────────────────────────

    async def fetch_ticker(self) -> Optional[dict]:
        """Fetch the current ticker via REST (fallback when WS not ready)."""
        if not self.exchange:
            return None
        try:
            ticker = await self.exchange.fetch_ticker(SYMBOL)
            return {
                "symbol": ticker["symbol"],
                "last": ticker["last"],
                "bid": ticker["bid"],
                "ask": ticker["ask"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.error("fetch_ticker error: %s", e)
            return None

    async def fetch_balance(self) -> Optional[dict]:
        """Fetch spot balance (USDT + BTC)."""
        if not self.exchange:
            return None
        try:
            balance = await self.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            btc = balance.get("BTC", {})
            return {
                "usdt_free": float(usdt.get("free", 0)),
                "usdt_used": float(usdt.get("used", 0)),
                "usdt_total": float(usdt.get("total", 0)),
                "btc_free": float(btc.get("free", 0)),
                "btc_used": float(btc.get("used", 0)),
                "btc_total": float(btc.get("total", 0)),
            }
        except Exception as e:
            logger.error("fetch_balance error: %s", e)
            return None