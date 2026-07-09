"""
strategy/engine.py
=================
StrategyEngine — fetches OHLCV data, computes indicators via strategy.indicators,
and evaluates the 5-condition confluence checklist.
Migrated from strategy.py to use core.config and strategy.indicators.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import ccxt.pro as ccxtpro
import pandas as pd

from core.config import STRATEGY, SYMBOL, TIMEFRAME_EXECUTION, TIMEFRAME_CONTEXT, TradingMode
from core.exceptions import InsufficientDataError, ExchangeConnectionError
from strategy.indicators import (
    compute_emas, compute_volume_sma, compute_rsi,
    compute_vwap_session, analyze_order_book, compute_trend_context,
)

logger = logging.getLogger("strategy")


# ──────────────────────────────────────────────
#  Data Structures
# ──────────────────────────────────────────────

@dataclass
class IndicatorSnapshot:
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
    score: int
    conditions: dict[str, bool] = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    should_enter: bool = False
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
    """Async strategy engine — OHLCV fetching + confluence evaluation."""

    def __init__(self, api_key: str, api_secret: str, mode: TradingMode = TradingMode.DEMO):
        self.mode = mode
        self.api_key = api_key
        self.api_secret = api_secret
        self.exchange: Optional[ccxtpro.binance] = None
        self._ohlcv_5m: pd.DataFrame = pd.DataFrame()
        self._ohlcv_15m: pd.DataFrame = pd.DataFrame()
        self._vwap_session_date: Optional[str] = None
        self._vwap_cum_pv: float = 0.0
        self._vwap_cum_vol: float = 0.0
        self._order_book: Optional[dict] = None
        self._running = False
        self._ws_tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._running:
            logger.warning("StrategyEngine already running")
            return
        self.exchange = ccxtpro.binance({
            "apiKey": self.api_key, "secret": self.api_secret,
            "enableRateLimit": True, "options": {"defaultType": "spot"},
        })
        if self.mode == TradingMode.DEMO:
            self.exchange.set_sandbox_mode(True)
            logger.info("StrategyEngine: TESTNET mode")
        else:
            logger.info("StrategyEngine: LIVE mode")
        await self._warmup_ohlcv()
        self._running = True
        self._ws_tasks.append(asyncio.create_task(self._watch_ohlcv_5m()))
        self._ws_tasks.append(asyncio.create_task(self._watch_ohlcv_15m()))
        self._ws_tasks.append(asyncio.create_task(self._watch_order_book()))

    async def stop(self) -> None:
        self._running = False
        for t in self._ws_tasks:
            t.cancel()
        self._ws_tasks.clear()
        if self.exchange:
            await self.exchange.close()
            self.exchange = None

    async def _warmup_ohlcv(self) -> None:
        ohlcv_5m = await self.exchange.fetch_ohlcv(SYMBOL, TIMEFRAME_EXECUTION, limit=STRATEGY.ohlcv_buffer_5m)
        self._ohlcv_5m = self._ohlcv_to_df(ohlcv_5m)
        ohlcv_15m = await self.exchange.fetch_ohlcv(SYMBOL, TIMEFRAME_CONTEXT, limit=STRATEGY.ohlcv_buffer_15m)
        self._ohlcv_15m = self._ohlcv_to_df(ohlcv_15m)
        self._reset_vwap_session()
        self._update_vwap_from_df(self._ohlcv_5m)
        logger.info("Warmup: 5m=%d, 15m=%d", len(self._ohlcv_5m), len(self._ohlcv_15m))

    async def _watch_ohlcv_5m(self) -> None:
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
        while self._running:
            try:
                self._order_book = await self.exchange.watch_order_book(SYMBOL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("OB WS error: %s", e)
                await asyncio.sleep(5)

    # ── Helpers ──

    @staticmethod
    def _ohlcv_to_df(ohlcv: list[list]) -> pd.DataFrame:
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.set_index("timestamp").sort_index()

    def _merge_ohlcv(self, df: pd.DataFrame, new_candles: list[list], timeframe: str) -> None:
        new_df = self._ohlcv_to_df(new_candles)
        if new_df.empty:
            return
        combined = pd.concat([df, new_df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        limit = STRATEGY.ohlcv_buffer_5m if timeframe == TIMEFRAME_EXECUTION else STRATEGY.ohlcv_buffer_15m
        trimmed = combined.iloc[-limit:].copy()
        if timeframe == TIMEFRAME_EXECUTION:
            self._ohlcv_5m = trimmed
        else:
            self._ohlcv_15m = trimmed

    def _reset_vwap_session(self) -> None:
        self._vwap_cum_pv = 0.0
        self._vwap_cum_vol = 0.0
        self._vwap_session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _update_vwap_from_df(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        candles = df[df.index.strftime("%Y-%m-%d") == today]
        if candles.empty:
            candles = df.tail(50)
        typical = (candles["high"] + candles["low"] + candles["close"]) / 3
        self._vwap_cum_pv = float((typical * candles["volume"]).sum())
        self._vwap_cum_vol = float(candles["volume"].sum())
        self._vwap_session_date = today

    def _compute_vwap(self) -> float:
        if self._vwap_cum_vol > 0:
            return self._vwap_cum_pv / self._vwap_cum_vol
        if not self._ohlcv_5m.empty:
            return float(self._ohlcv_5m["close"].iloc[-1])
        return 0.0

    # ── Public API ──

    async def get_snapshot(self) -> Optional[IndicatorSnapshot]:
        if self._ohlcv_5m.empty:
            return None
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._vwap_session_date != today:
            self._reset_vwap_session()
            self._update_vwap_from_df(self._ohlcv_5m)

        ema9, ema21, ema50 = compute_emas(self._ohlcv_5m, STRATEGY.ema_fast, STRATEGY.ema_mid, STRATEGY.ema_slow)
        cv, vs20, vr = compute_volume_sma(self._ohlcv_5m, STRATEGY.volume_sma_period)
        rsi = compute_rsi(self._ohlcv_5m, STRATEGY.rsi_period)
        vwap = self._compute_vwap()
        strength, wall_in_range = analyze_order_book(
            self._order_book,
            STRATEGY.order_book_depth_pct,
            STRATEGY.bid_wall_min_pct,
            STRATEGY.bid_wall_max_pct,
        )
        trend, e21_15, e50_15 = compute_trend_context(self._ohlcv_15m, STRATEGY.ema_mid, STRATEGY.ema_slow)
        price = float(self._ohlcv_5m["close"].iloc[-1])
        ts = self._ohlcv_5m.index[-1].to_pydatetime()

        return IndicatorSnapshot(
            timestamp=ts, price=price, ema9=ema9, ema21=ema21, ema50=ema50,
            vwap=vwap, current_volume=cv, volume_sma20=vs20, volume_ratio=vr,
            rsi=rsi, bid_wall_strength=strength, bid_wall_within_range=wall_in_range,
            trend_15m=trend, ema21_15m=e21_15, ema50_15m=e50_15,
        )

    def evaluate_confluence(self, snap: IndicatorSnapshot) -> ConfluenceResult:
        conditions: dict[str, bool] = {}
        details: dict = {}

        c1 = snap.price > snap.ema21 and snap.ema21 > snap.ema50
        conditions["c1_bullish_trend"] = c1
        details["c1"] = {"price": snap.price, "ema21": snap.ema21, "ema50": snap.ema50}

        vwap_dist = abs(snap.price - snap.vwap) / snap.vwap if snap.vwap > 0 else 1.0
        above_vwap = snap.price > snap.vwap
        retest = vwap_dist <= 0.0015 and snap.price > snap.ema9
        c2 = above_vwap or retest
        conditions["c2_vwap_position"] = c2
        details["c2"] = {"vwap": snap.vwap, "dist_pct": round(vwap_dist * 100, 4)}

        c3 = snap.volume_ratio >= STRATEGY.volume_spike_multiplier
        conditions["c3_volume_spike"] = c3
        details["c3"] = {"ratio": round(snap.volume_ratio, 3)}

        c4 = STRATEGY.rsi_lower <= snap.rsi <= STRATEGY.rsi_upper
        conditions["c4_rsi_zone"] = c4
        details["c4"] = {"rsi": round(snap.rsi, 2)}

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

        return ConfluenceResult(score=score, conditions=conditions, details=details,
                                should_enter=should_enter, reason=reason)

    async def fetch_ticker(self) -> Optional[dict]:
        if not self.exchange:
            return None
        try:
            t = await self.exchange.fetch_ticker(SYMBOL)
            return {"symbol": t["symbol"], "last": t["last"], "bid": t["bid"], "ask": t["ask"]}
        except Exception as e:
            logger.error("fetch_ticker: %s", e)
            return None

    async def fetch_balance(self) -> Optional[dict]:
        if not self.exchange:
            return None
        try:
            b = await self.exchange.fetch_balance()
            return {
                "usdt_free": float(b.get("USDT", {}).get("free", 0)),
                "usdt_total": float(b.get("USDT", {}).get("total", 0)),
                "btc_free": float(b.get("BTC", {}).get("free", 0)),
                "btc_total": float(b.get("BTC", {}).get("total", 0)),
            }
        except Exception as e:
            logger.error("fetch_balance: %s", e)
            return None