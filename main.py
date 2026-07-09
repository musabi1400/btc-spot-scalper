"""
main.py
=======
FastAPI backend for the BTC Spot Scalper.

Integrates:
  • StrategyEngine   — indicator computation + confluence evaluation
  • ExecutionEngine  — ccxt.pro order placement (limit/maker only)
  • RiskManager      — position sizing, SL/TP, circuit breaker, trailing stop
  • BotLoop          — main async loop that ties everything together
  • WebSocket hub    — real-time push to the dashboard frontend
  • REST API         — settings, trades, logs, emergency stop, mode toggle

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import Engine as SqlaEngine
from sqlalchemy.orm import sessionmaker

from config import (
    APP, RISK, STRATEGY, SYMBOL, TradingMode,
    EFFECTIVE_MAKER_FEE, ROUND_TRIP_FEE,
)
from models import (
    Base, Settings, Trade, BotLog, DailyStats, SignalHistory,
    build_engine, build_session_factory, db_session,
)
from strategy import StrategyEngine, IndicatorSnapshot, ConfluenceResult
from execution import ExecutionEngine, FillResult

# ──────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


# ──────────────────────────────────────────────
#  Credential Encryption (AES-256-GCM via Fernet)
# ──────────────────────────────────────────────

class CredentialVault:
    """Encrypt/decrypt API keys stored in the database using Fernet."""

    def __init__(self, key: Optional[str] = None):
        self._fernet = None
        if key:
            self._init_fernet(key)

    def _init_fernet(self, key: str) -> None:
        from cryptography.fernet import Fernet
        import base64
        # Derive a 32-byte key from the provided string
        key_bytes = key.encode("utf-8").ljust(32, b"\0")[:32]
        fernet_key = base64.urlsafe_b64encode(key_bytes)
        self._fernet = Fernet(fernet_key)

    def encrypt(self, plaintext: str) -> str:
        if not self._fernet or not plaintext:
            return plaintext or ""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if not self._fernet or not ciphertext:
            return ciphertext or ""
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except Exception:
            return ""


# ──────────────────────────────────────────────
#  Risk Manager
# ──────────────────────────────────────────────

class RiskManager:
    """
    Standalone risk manager enforcing strict rules:
      • Position sizing: min(available_usdt, max_allowed) with 1% max risk
      • Stop-loss: 0.3%-0.5% below entry or below recent swing low
      • Take-profit: minimum 1:1.5 R:R and ≥0.5% gross profit
      • Trailing stop: move to break-even at 1R profit
      • Daily circuit breaker: 3 losses → 24h halt
      • Max concurrent trades: 1
    """

    def __init__(self, db_factory: sessionmaker):
        self.db_factory = db_factory
        self._halted: bool = False
        self._halt_until: Optional[datetime] = None
        self._consecutive_losses: int = 0
        self._trades_today: int = 0
        self._load_daily_state()

    def _load_daily_state(self) -> None:
        """Load today's stats from the database on startup."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with db_session(self.db_factory) as session:
            stats = session.query(DailyStats).filter_by(date=today).first()
            if stats:
                self._consecutive_losses = stats.consecutive_losses
                self._trades_today = stats.trades_total
                self._halted = stats.halted
                self._halt_until = stats.halt_until
                if self._halted and self._halt_until:
                    if datetime.now(timezone.utc) >= self._halt_until:
                        self._halted = False
                        self._halt_until = None
                        stats.halted = False
                        stats.halt_until = None
                        logger.info("Circuit breaker expired — trading resumed")

    def _save_daily_state(self, session) -> DailyStats:
        """Update or create today's DailyStats row."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stats = session.query(DailyStats).filter_by(date=today).first()
        if not stats:
            stats = DailyStats(date=today)
            session.add(stats)
        stats.consecutive_losses = self._consecutive_losses
        stats.trades_total = self._trades_today
        stats.halted = self._halted
        stats.halt_until = self._halt_until
        return stats

    # ── Pre-trade checks ──

    def can_trade(self) -> tuple[bool, str]:
        """Check if the bot is allowed to open a new trade."""
        if self._halted:
            if self._halt_until:
                remaining = self._halt_until - datetime.now(timezone.utc)
                if remaining.total_seconds() > 0:
                    return False, f"Circuit breaker active — halted until {self._halt_until.isoformat()}"
                else:
                    # Expired — reset
                    self._halted = False
                    self._halt_until = None
                    self._consecutive_losses = 0
                    with db_session(self.db_factory) as session:
                        self._save_daily_state(session)
            else:
                return False, "Circuit breaker active (no expiry set)"

        return True, "OK"

    # ── Position sizing ──

    def calculate_position_size(
        self,
        available_usdt: float,
        entry_price: float,
        sl_price: float,
    ) -> dict:
        """
        Calculate the optimal position size.

        In Spot (no leverage):
          size = min(available_usdt × max_position_pct, risk_amount / sl_distance_pct)

        Returns dict with:
          size_usdt, quantity_btc, sl_pct, risk_usdt, risk_pct
        """
        sl_distance_pct = abs(entry_price - sl_price) / entry_price

        # Max position size as % of available capital
        max_position_usdt = available_usdt * RISK.max_position_pct

        # Risk-based sizing: 1% of total balance ÷ SL distance
        # In spot without leverage, the actual risk = position_size × sl_distance_pct
        # We want: position_size × sl_distance_pct <= balance × max_risk_pct
        # But without leverage, position_size <= available_usdt
        # So: risk_usdt = min(max_position_usdt, available_usdt) × sl_distance_pct

        size_usdt = min(max_position_usdt, available_usdt)
        risk_usdt = size_usdt * sl_distance_pct
        risk_pct = risk_usdt / available_usdt if available_usdt > 0 else 0

        # If risk exceeds 1%, reduce position size
        if risk_pct > RISK.max_risk_per_trade_pct:
            size_usdt = available_usdt * RISK.max_risk_per_trade_pct / sl_distance_pct
            size_usdt = min(size_usdt, max_position_usdt)
            risk_usdt = size_usdt * sl_distance_pct
            risk_pct = risk_usdt / available_usdt if available_usdt > 0 else 0

        quantity_btc = size_usdt / entry_price if entry_price > 0 else 0

        return {
            "size_usdt": round(size_usdt, 2),
            "quantity_btc": round(quantity_btc, 8),
            "sl_pct": round(sl_distance_pct * 100, 4),
            "risk_usdt": round(risk_usdt, 2),
            "risk_pct": round(risk_pct * 100, 4),
        }

    # ── Stop-loss & Take-profit calculation ──

    def calculate_sl_tp(
        self,
        entry_price: float,
        recent_swing_low: Optional[float] = None,
    ) -> dict:
        """
        Calculate SL and TP prices.

        SL: 0.3%-0.5% below entry, or below recent swing low (whichever is tighter
            but not closer than 0.2% to avoid immediate stop-out).

        TP: max(1.5 × SL distance, 0.5% gross profit) above entry.
        """
        # Default SL at 0.4% below entry
        sl_default = entry_price * (1 - RISK.sl_default_pct)

        # If swing low provided, use the tighter of the two (closer to entry)
        if recent_swing_low and recent_swing_low < entry_price:
            swing_sl = recent_swing_low * 0.999  # 0.1% below swing low for buffer
            # Use whichever is closer to entry (tighter SL = less risk)
            sl_price = max(sl_default, swing_sl)
        else:
            sl_price = sl_default

        # Clamp SL to 0.3%-0.5% range
        sl_min = entry_price * (1 - RISK.sl_min_pct)
        sl_max = entry_price * (1 - RISK.sl_max_pct)
        sl_price = max(sl_max, min(sl_min, sl_price))  # tighter of the two bounds

        sl_distance = entry_price - sl_price
        sl_pct = sl_distance / entry_price

        # TP: minimum 1:1.5 R:R, and at least 0.5% gross profit
        tp_rr_based = entry_price + (sl_distance * RISK.min_rr_ratio)
        tp_min_profit = entry_price * (1 + RISK.min_gross_profit_pct)
        tp_price = max(tp_rr_based, tp_min_profit)

        tp_distance = tp_price - entry_price
        tp_pct = tp_distance / entry_price

        # Break-even price (entry + round-trip fees)
        breakeven_price = entry_price * (1 + ROUND_TRIP_FEE)

        return {
            "sl_price": round(sl_price, 2),
            "tp_price": round(tp_price, 2),
            "sl_pct": round(sl_pct * 100, 4),
            "tp_pct": round(tp_pct * 100, 4),
            "rr_ratio": round(tp_distance / sl_distance, 2) if sl_distance > 0 else 0,
            "breakeven_price": round(breakeven_price, 2),
        }

    # ── Trailing stop ──

    def update_trailing_stop(
        self,
        entry_price: float,
        current_price: float,
        current_sl: float,
        breakeven_price: float,
    ) -> Optional[float]:
        """
        Trailing stop logic:
          At 1R profit, move SL to break-even (entry + round-trip fees).
          After that, trail SL to maintain at least 1R distance below current price.

        Returns new SL price if updated, or None if no change.
        """
        sl_distance = entry_price - current_sl  # original SL distance
        if sl_distance <= 0:
            return None

        # 1R profit level
        r1_level = entry_price + sl_distance  # 1R = same distance as SL

        if current_price >= r1_level:
            # Move to break-even (never move SL down)
            if current_sl < breakeven_price:
                logger.info("Trailing stop: moving SL to break-even @ %s", breakeven_price)
                return breakeven_price

            # Further trailing: keep SL at current_price - 1R (lock in profit)
            trail_level = current_price - sl_distance
            if trail_level > current_sl:
                logger.info("Trailing stop: advancing SL to %s (locking profit)", trail_level)
                return round(trail_level, 2)

        return None

    # ── Post-trade recording ──

    def record_trade_result(self, net_pnl: float) -> None:
        """Record a completed trade result and update circuit breaker state."""
        self._trades_today += 1

        if net_pnl < 0:
            self._consecutive_losses += 1
            logger.warning(
                "Loss recorded — consecutive losses: %d/%d",
                self._consecutive_losses, RISK.max_daily_losses,
            )
            if self._consecutive_losses >= RISK.max_daily_losses:
                self._halted = True
                self._halt_until = datetime.now(timezone.utc) + timedelta(hours=RISK.cooldown_hours)
                logger.error(
                    "⚠️ CIRCUIT BREAKER TRIGGERED — trading halted until %s",
                    self._halt_until.isoformat(),
                )
        else:
            # Reset consecutive losses on a win
            self._consecutive_losses = 0

        with db_session(self.db_factory) as session:
            stats = self._save_daily_state(session)
            stats.net_pnl_usdt += net_pnl
            if net_pnl > 0:
                stats.wins += 1
            else:
                stats.losses += 1

    def get_status(self) -> dict:
        """Return current risk manager state for the dashboard."""
        return {
            "halted": self._halted,
            "halt_until": self._halt_until.isoformat() if self._halt_until else None,
            "consecutive_losses": self._consecutive_losses,
            "max_daily_losses": RISK.max_daily_losses,
            "trades_today": self._trades_today,
            "can_trade": self.can_trade()[0],
        }


# ──────────────────────────────────────────────
#  WebSocket Hub (broadcast to dashboard clients)
# ──────────────────────────────────────────────

class WebSocketHub:
    """Manage WebSocket connections and broadcast updates."""

    def __init__(self):
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.append(ws)
        logger.info("WS client connected — total: %d", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)
        logger.info("WS client disconnected — total: %d", len(self._clients))

    async def broadcast(self, message: dict) -> None:
        """Send a message to all connected clients."""
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)


# ──────────────────────────────────────────────
#  Application State Container
# ──────────────────────────────────────────────

class AppState:
    """Holds all shared state: engines, risk manager, DB, WebSocket hub."""

    def __init__(self):
        self.engine: Optional[SqlaEngine] = None
        self.db_factory: Optional[sessionmaker] = None
        self.vault: CredentialVault = CredentialVault(APP.encryption_key)

        self.strategy: Optional[StrategyEngine] = None
        self.execution: Optional[ExecutionEngine] = None
        self.risk: Optional[RiskManager] = None
        self.ws_hub = WebSocketHub()

        self.bot_task: Optional[asyncio.Task] = None
        self.bot_running: bool = False
        self.emergency_stop_flag: bool = False

        # Current active trade (in-memory mirror of DB)
        self.active_trade: Optional[Trade] = None

        # Latest snapshot & confluence (for dashboard)
        self.latest_snapshot: Optional[dict] = None
        self.latest_confluence: Optional[dict] = None

        # Bot state: SEARCHING / IN_TRADE / HALTED / STOPPED
        self.bot_state: str = "STOPPED"

    def get_settings(self) -> Settings:
        """Load the single settings row from DB."""
        with db_session(self.db_factory) as session:
            settings = session.query(Settings).first()
            if not settings:
                settings = Settings(id=1)
                session.add(settings)
            return settings

    def log(self, level: str, message: str, context: Optional[dict] = None) -> None:
        """Persist a log entry and broadcast it to WS clients."""
        with db_session(self.db_factory) as session:
            entry = BotLog(
                level=level,
                message=message,
                context=json.dumps(context) if context else None,
            )
            session.add(entry)
        # Also log to console
        log_level = getattr(logging, level.upper(), logging.INFO)
        logger.log(log_level, message)


state = AppState()


# ──────────────────────────────────────────────
#  Bot Main Loop
# ──────────────────────────────────────────────

class BotLoop:
    """
    Main trading loop that orchestrates strategy evaluation,
    order execution, and trade management.
    """

    def __init__(self, app_state: AppState):
        self.s = app_state
        self._tick_interval = 5  # seconds between strategy evaluations

    async def run(self) -> None:
        """Main bot loop — runs until stopped or emergency flag set."""
        self.s.bot_running = True
        self.s.bot_state = "SEARCHING"
        self.s.log("INFO", "Bot loop started")

        try:
            while self.s.bot_running and not self.s.emergency_stop_flag:
                await self._tick()
                await asyncio.sleep(self._tick_interval)
        except asyncio.CancelledError:
            logger.info("Bot loop cancelled")
        except Exception as e:
            logger.error("Bot loop crashed: %s", e)
            self.s.log("ERROR", f"Bot loop crashed: {e}")
        finally:
            self.s.bot_running = False
            self.s.bot_state = "STOPPED"
            self.s.log("INFO", "Bot loop stopped")

    async def _tick(self) -> None:
        """Single evaluation cycle."""
        # 1. Check risk manager
        can_trade, reason = self.s.risk.can_trade()
        if not can_trade:
            if self.s.bot_state != "HALTED":
                self.s.log("WARN", f"Trading halted: {reason}")
            self.s.bot_state = "HALTED"
            await self._broadcast_status()
            return

        # 2. Check for active trade — manage it first
        if self.s.active_trade and self.s.active_trade.status in ("IN_TRADE", "FILLED_BUY"):
            await self._manage_active_trade()
            return  # don't search for new entries while in a trade

        # 3. Evaluate strategy for new entry
        self.s.bot_state = "SEARCHING"
        snapshot = await self.s.strategy.get_snapshot()
        if not snapshot:
            return

        confluence = self.s.strategy.evaluate_confluence(snapshot)

        # Store latest for dashboard
        self.s.latest_snapshot = snapshot.to_dict()
        self.s.latest_confluence = confluence.to_dict()

        # Record signal in history
        self._record_signal(snapshot, confluence)

        # Broadcast update
        await self._broadcast_status()

        # 4. Check for entry
        if confluence.should_enter:
            await self._execute_entry(snapshot, confluence)

    async def _execute_entry(
        self, snap: IndicatorSnapshot, confluence: ConfluenceResult
    ) -> None:
        """Execute a buy entry based on confluence signal."""
        self.s.log("TRADE", f"Entry signal detected — {confluence.reason}")

        # Get balance
        balance = await self.s.execution.get_balance()
        available_usdt = balance["usdt_free"]

        if available_usdt < 10:  # minimum $10 to trade
            self.s.log("WARN", f"Insufficient USDT balance: {available_usdt}")
            return

        # Calculate SL/TP
        # Find recent swing low from 5m data (last 10 candles)
        recent_swing_low = None
        if hasattr(self.s.strategy, '_ohlcv_5m') and not self.s.strategy._ohlcv_5m.empty:
            recent_lows = self.s.strategy._ohlcv_5m["low"].tail(10)
            recent_swing_low = float(recent_lows.min())

        sl_tp = self.s.risk.calculate_sl_tp(snap.price, recent_swing_low)

        # Calculate position size
        sizing = self.s.risk.calculate_position_size(
            available_usdt, snap.price, sl_tp["sl_price"]
        )

        self.s.log(
            "INFO",
            f"Entry plan: size=${sizing['size_usdt']}, qty={sizing['quantity_btc']} BTC, "
            f"SL={sl_tp['sl_price']} ({sl_tp['sl_pct']}%), TP={sl_tp['tp_price']} ({sl_tp['tp_pct']}%), "
            f"R:R=1:{sl_tp['rr_ratio']}, risk=${sizing['risk_usdt']} ({sizing['risk_pct']}%)",
        )

        # Place limit buy at current price (use best bid for maker fill)
        ticker = await self.s.execution.exchange.fetch_ticker(SYMBOL)
        best_bid = ticker.get("bid", snap.price)
        # Place at best bid to ensure maker fill
        buy_price = best_bid

        fill = await self.s.execution.place_limit_buy(
            price=buy_price,
            quantity=sizing["quantity_btc"],
            timeout_sec=RISK.order_fill_timeout_sec,
        )

        if fill is None:
            self.s.log("WARN", "Buy order not filled — skipping entry")
            return

        # Create trade record in DB
        trade = Trade(
            symbol=SYMBOL,
            order_id_buy=fill.order_id,
            side="BUY",
            status="IN_TRADE",
            entry_time=fill.timestamp,
            entry_price=fill.avg_fill_price,
            quantity_btc=fill.filled_qty,
            position_size_usdt=fill.filled_qty * fill.avg_fill_price,
            stop_loss_price=sl_tp["sl_price"],
            take_profit_price=sl_tp["tp_price"],
            sl_pct=sl_tp["sl_pct"],
            tp_pct=sl_tp["tp_pct"],
            fee_buy_usdt=fill.fee_paid,
            fees_total_usdt=fill.fee_paid,
            confluence_score=confluence.score,
            conditions_met=json.dumps(list(confluence.conditions.keys())),
            entry_5m_close=snap.price,
        )
        with db_session(self.s.db_factory) as session:
            session.add(trade)
            session.flush()
            session.refresh(trade)
            self.s.active_trade = trade

        self.s.bot_state = "IN_TRADE"
        self.s.log(
            "TRADE",
            f"BUY FILLED: {fill.filled_qty} BTC @ ${fill.avg_fill_price} | "
            f"SL: ${sl_tp['sl_price']} | TP: ${sl_tp['tp_price']}",
        )
        await self._broadcast_status()

    async def _manage_active_trade(self) -> None:
        """Monitor the active trade for SL/TP/trailing triggers."""
        trade = self.s.active_trade
        if not trade:
            return

        # Get current price
        ticker = await self.s.execution.exchange.fetch_ticker(SYMBOL)
        current_price = ticker.get("last", 0)
        if current_price <= 0:
            return

        entry = trade.entry_price
        sl = trade.stop_loss_price
        tp = trade.take_profit_price
        trailing_sl = trade.trailing_sl_price or sl

        # Update trailing stop
        breakeven = entry * (1 + ROUND_TRIP_FEE)
        new_sl = self.s.risk.update_trailing_stop(entry, current_price, trailing_sl, breakeven)
        if new_sl and new_sl > trailing_sl:
            trailing_sl = new_sl
            with db_session(self.s.db_factory) as session:
                db_trade = session.get(Trade, trade.id)
                if db_trade:
                    db_trade.trailing_sl_price = new_sl
            trade.trailing_sl_price = new_sl
            self.s.log("INFO", f"Trailing SL updated to ${new_sl}")

        # Check exit conditions
        should_exit = False
        exit_reason = ""

        if current_price <= trailing_sl:
            should_exit = True
            exit_reason = "stop_loss" if trailing_sl == sl else "trailing_stop"
        elif current_price >= tp:
            should_exit = True
            exit_reason = "take_profit"

        if should_exit:
            await self._execute_exit(current_price, exit_reason)
        else:
            # Broadcast updated trade status
            await self._broadcast_status()

    async def _execute_exit(self, exit_price: float, reason: str) -> None:
        """Execute the sell order and close the trade."""
        trade = self.s.active_trade
        if not trade:
            return

        self.s.log("TRADE", f"Exit signal: {reason} @ ${exit_price}")

        # Place limit sell
        # For TP: sell at TP price (maker)
        # For SL: sell aggressively (IOC) to ensure fill
        if reason == "take_profit":
            sell_price = trade.take_profit_price
            fill = await self.s.execution.place_limit_sell(
                price=sell_price, quantity=trade.quantity_btc, timeout_sec=120
            )
        else:
            # SL / trailing / emergency — use aggressive IOC
            ticker = await self.s.execution.exchange.fetch_ticker(SYMBOL)
            best_bid = ticker.get("bid", exit_price)
            sell_price = best_bid * 0.9995  # just below bid for fast fill
            fill = await self.s.execution.place_limit_sell(
                price=sell_price, quantity=trade.quantity_btc, timeout_sec=30
            )

        if fill is None:
            self.s.log("ERROR", "Exit order failed — retrying with emergency sell")
            fill = await self.s.execution.emergency_sell_all()
            if fill is None:
                self.s.log("ERROR", "CRITICAL: Could not exit position — manual intervention needed!")
                return

        # Calculate PnL
        gross_pnl = (fill.avg_fill_price - trade.entry_price) * fill.filled_qty
        total_fees = trade.fee_buy_usdt + fill.fee_paid
        net_pnl = gross_pnl - total_fees
        return_pct = (net_pnl / trade.position_size_usdt) * 100 if trade.position_size_usdt > 0 else 0

        # Update trade in DB
        with db_session(self.s.db_factory) as session:
            db_trade = session.get(Trade, trade.id)
            if db_trade:
                db_trade.order_id_sell = fill.order_id
                db_trade.exit_time = fill.timestamp
                db_trade.exit_price = fill.avg_fill_price
                db_trade.exit_reason = reason
                db_trade.fee_sell_usdt = fill.fee_paid
                db_trade.fees_total_usdt = total_fees
                db_trade.gross_pnl_usdt = gross_pnl
                db_trade.net_pnl_usdt = net_pnl
                db_trade.return_pct = return_pct
                db_trade.status = "CLOSED"

        # Record in risk manager (circuit breaker)
        self.s.risk.record_trade_result(net_pnl)

        # Log the result
        result_str = "PROFIT" if net_pnl > 0 else "LOSS"
        self.s.log(
            "TRADE",
            f"TRADE CLOSED ({result_str}): {trade.quantity_btc} BTC | "
            f"Entry: ${trade.entry_price} → Exit: ${fill.avg_fill_price} | "
            f"Gross PnL: ${gross_pnl:.4f} | Fees: ${total_fees:.4f} | "
            f"Net PnL: ${net_pnl:.4f} ({return_pct:.3f}%) | Reason: {reason}",
        )

        # Clear active trade
        self.s.active_trade = None
        self.s.bot_state = "SEARCHING"
        await self._broadcast_status()

    async def _execute_emergency_exit(self) -> None:
        """Emergency stop: cancel all orders and sell everything."""
        self.s.log("ERROR", "⚠️ EMERGENCY STOP triggered — liquidating position")

        # Cancel all open orders
        await self.s.execution.cancel_all_open_orders()

        # Sell all BTC
        if self.s.active_trade:
            fill = await self.s.execution.emergency_sell_all()
            if fill:
                gross_pnl = (fill.avg_fill_price - self.s.active_trade.entry_price) * fill.filled_qty
                total_fees = self.s.active_trade.fee_buy_usdt + fill.fee_paid
                net_pnl = gross_pnl - total_fees
                return_pct = (net_pnl / self.s.active_trade.position_size_usdt) * 100 if self.s.active_trade.position_size_usdt > 0 else 0

                with db_session(self.s.db_factory) as session:
                    db_trade = session.get(Trade, self.s.active_trade.id)
                    if db_trade:
                        db_trade.exit_time = fill.timestamp
                        db_trade.exit_price = fill.avg_fill_price
                        db_trade.exit_reason = "emergency"
                        db_trade.fee_sell_usdt = fill.fee_paid
                        db_trade.fees_total_usdt = total_fees
                        db_trade.gross_pnl_usdt = gross_pnl
                        db_trade.net_pnl_usdt = net_pnl
                        db_trade.return_pct = return_pct
                        db_trade.status = "CLOSED"

                self.s.risk.record_trade_result(net_pnl)
                self.s.log("ERROR", f"Emergency exit completed — Net PnL: ${net_pnl:.4f}")

        self.s.active_trade = None
        self.s.bot_state = "STOPPED"
        await self._broadcast_status()

    def _record_signal(self, snap: IndicatorSnapshot, confluence: ConfluenceResult) -> None:
        """Persist a signal evaluation to the audit trail."""
        with db_session(self.s.db_factory) as session:
            signal = SignalHistory(
                price=snap.price,
                confluence_score=confluence.score,
                conditions_met=json.dumps(list(confluence.conditions.keys())),
                conditions_detail=json.dumps(confluence.details),
                action="ENTER" if confluence.should_enter else "EVAL",
            )
            session.add(signal)

    async def _broadcast_status(self) -> None:
        """Broadcast current bot state to all WS clients."""
        balance = await self.s.execution.get_balance() if self.s.execution else None
        data = {
            "type": "status",
            "bot_state": self.s.bot_state,
            "bot_running": self.s.bot_running,
            "balance": balance,
            "active_trade": self.s.active_trade.to_dict() if self.s.active_trade else None,
            "latest_snapshot": self.s.latest_snapshot,
            "latest_confluence": self.s.latest_confluence,
            "risk_status": self.s.risk.get_status() if self.s.risk else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self.s.ws_hub.broadcast(data)


# ──────────────────────────────────────────────
#  Pydantic Models for API
# ──────────────────────────────────────────────

class CredentialsUpdate(BaseModel):
    api_key: str
    api_secret: str

class ModeUpdate(BaseModel):
    mode: str  # "demo" or "live"

class AutoTradeToggle(BaseModel):
    enabled: bool

class EmergencyStopRequest(BaseModel):
    confirm: bool  # must be True


# ──────────────────────────────────────────────
#  FastAPI App
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    # Startup
    logger.info("Starting BTC Scalper application...")
    state.engine = build_engine(APP.dsn)
    state.db_factory = build_session_factory(state.engine)

    # Initialise risk manager
    state.risk = RiskManager(state.db_factory)

    # Load settings and initialise engines if credentials exist
    settings = state.get_settings()
    if settings.api_key_encrypted:
        api_key = state.vault.decrypt(settings.api_key_encrypted)
        api_secret = state.vault.decrypt(settings.api_secret_encrypted)
        mode = TradingMode(settings.mode)
        await _init_engines(api_key, api_secret, mode)

    state.log("INFO", "Application started successfully")
    yield

    # Shutdown
    logger.info("Shutting down...")
    if state.bot_task:
        state.bot_task.cancel()
        try:
            await state.bot_task
        except asyncio.CancelledError:
            pass
    if state.strategy:
        await state.strategy.stop()
    if state.execution:
        await state.execution.stop()
    state.log("INFO", "Application stopped")


async def _init_engines(api_key: str, api_secret: str, mode: TradingMode) -> bool:
    """Initialise strategy and execution engines with credentials."""
    try:
        # Stop existing engines first
        if state.strategy:
            await state.strategy.stop()
        if state.execution:
            await state.execution.stop()

        state.strategy = StrategyEngine(api_key, api_secret, mode)
        state.execution = ExecutionEngine(api_key, api_secret, mode)

        await state.strategy.start()
        await state.execution.start()
        return True
    except Exception as e:
        logger.error("Engine init failed: %s", e)
        state.log("ERROR", f"Engine init failed: {e}")
        return False


app = FastAPI(
    title="BTC Spot Scalper",
    description="Automated Bitcoin scalping bot — Spot only, no leverage",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ──────────────────────────────────────────────
#  REST API Endpoints
# ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the dashboard."""
    index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Dashboard not found</h1><p>static/index.html missing</p>", 404)


@app.get("/api/status")
async def get_status():
    """Get current bot status, balance, and risk state."""
    balance = await state.execution.get_balance() if state.execution else None
    return {
        "bot_state": state.bot_state,
        "bot_running": state.bot_running,
        "mode": state.get_settings().mode,
        "auto_trade": state.get_settings().auto_trade,
        "balance": balance,
        "active_trade": state.active_trade.to_dict() if state.active_trade else None,
        "latest_snapshot": state.latest_snapshot,
        "latest_confluence": state.latest_confluence,
        "risk_status": state.risk.get_status() if state.risk else None,
        "ws_clients": state.ws_hub.client_count,
    }


@app.get("/api/settings")
async def get_settings_api():
    """Get current settings (without exposing secrets)."""
    s = state.get_settings()
    return {
        "mode": s.mode,
        "auto_trade": s.auto_trade,
        "use_bnb_fee": s.use_bnb_fee,
        "has_api_key": bool(s.api_key_encrypted),
        "has_api_secret": bool(s.api_secret_encrypted),
    }


@app.post("/api/settings/credentials")
async def update_credentials(creds: CredentialsUpdate):
    """Update API credentials (encrypted in DB)."""
    # Encrypt and store
    enc_key = state.vault.encrypt(creds.api_key)
    enc_secret = state.vault.encrypt(creds.api_secret)

    with db_session(state.db_factory) as session:
        settings = session.query(Settings).first()
        if not settings:
            settings = Settings(id=1)
            session.add(settings)
        settings.api_key_encrypted = enc_key
        settings.api_secret_encrypted = enc_secret

    # Re-init engines with new credentials
    mode = TradingMode(state.get_settings().mode)
    success = await _init_engines(creds.api_key, creds.api_secret, mode)

    if success:
        state.log("INFO", "API credentials updated — engines reconnected")
        return {"status": "ok", "message": "Credentials updated and engines reconnected"}
    else:
        raise HTTPException(500, "Credentials saved but engine init failed")


@app.post("/api/settings/mode")
async def update_mode(mode_update: ModeUpdate):
    """Switch between demo and live mode."""
    new_mode = mode_update.mode.lower()
    if new_mode not in ("demo", "live"):
        raise HTTPException(400, "Mode must be 'demo' or 'live'")

    with db_session(state.db_factory) as session:
        settings = session.query(Settings).first()
        if not settings:
            settings = Settings(id=1)
            session.add(settings)
        settings.mode = new_mode

    # Stop bot if running
    if state.bot_task:
        state.bot_task.cancel()
        try:
            await state.bot_task
        except asyncio.CancelledError:
            pass

    # Re-init engines with new mode
    settings = state.get_settings()
    if settings.api_key_encrypted:
        api_key = state.vault.decrypt(settings.api_key_encrypted)
        api_secret = state.vault.decrypt(settings.api_secret_encrypted)
        await _init_engines(api_key, api_secret, TradingMode(new_mode))

    state.log("WARN", f"Trading mode changed to: {new_mode.upper()}")
    return {"status": "ok", "mode": new_mode}


@app.post("/api/settings/autotrade")
async def toggle_auto_trade(toggle: AutoTradeToggle):
    """Enable or disable auto-trading."""
    with db_session(state.db_factory) as session:
        settings = session.query(Settings).first()
        if not settings:
            settings = Settings(id=1)
            session.add(settings)
        settings.auto_trade = toggle.enabled

    if toggle.enabled:
        if not state.strategy or not state.execution:
            raise HTTPException(400, "Engines not initialised — set credentials first")
        if not state.bot_running:
            bot_loop = BotLoop(state)
            state.bot_task = asyncio.create_task(bot_loop.run())
            state.log("INFO", "Auto-trading ENABLED — bot loop started")
    else:
        if state.bot_task:
            state.bot_task.cancel()
            try:
                await state.bot_task
            except asyncio.CancelledError:
                pass
            state.log("INFO", "Auto-trading DISABLED — bot loop stopped")

    return {"status": "ok", "auto_trade": toggle.enabled}


@app.post("/api/emergency-stop")
async def emergency_stop(req: EmergencyStopRequest):
    """Emergency stop — cancel all orders and liquidate position."""
    if not req.confirm:
        raise HTTPException(400, "Confirmation required")

    state.emergency_stop_flag = True

    # Stop bot loop
    if state.bot_task:
        state.bot_task.cancel()
        try:
            await state.bot_task
        except asyncio.CancelledError:
            pass

    # Execute emergency exit
    bot_loop = BotLoop(state)
    await bot_loop._execute_emergency_exit()

    state.emergency_stop_flag = False
    state.log("ERROR", "Emergency stop completed — all positions liquidated")
    return {"status": "ok", "message": "Emergency stop executed"}


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    """Get recent trades for the trading journal."""
    with db_session(state.db_factory) as session:
        trades = (
            session.query(Trade)
            .order_by(Trade.created_at.desc())
            .limit(limit)
            .all()
        )
        return [t.to_dict() for t in trades]


@app.get("/api/performance")
async def get_performance():
    """Calculate and return performance metrics."""
    with db_session(state.db_factory) as session:
        trades = session.query(Trade).filter(Trade.status == "CLOSED").all()

        if not trades:
            return {
                "total_trades": 0,
                "win_rate": 0,
                "total_net_profit": 0,
                "profit_factor": 0,
                "avg_win": 0,
                "avg_loss": 0,
                "total_fees": 0,
            }

        wins = [t for t in trades if t.net_pnl_usdt > 0]
        losses = [t for t in trades if t.net_pnl_usdt < 0]

        gross_profit = sum(t.net_pnl_usdt for t in wins)
        gross_loss = abs(sum(t.net_pnl_usdt for t in losses))
        total_net = sum(t.net_pnl_usdt for t in trades)
        total_fees = sum(t.fees_total_usdt or 0 for t in trades)

        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        win_rate = (len(wins) / len(trades)) * 100

        avg_win = gross_profit / len(wins) if wins else 0
        avg_loss = gross_loss / len(losses) if losses else 0

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 2),
            "total_net_profit": round(total_net, 4),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999,
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "total_fees": round(total_fees, 4),
        }


@app.get("/api/logs")
async def get_logs(limit: int = 100):
    """Get recent bot logs."""
    with db_session(state.db_factory) as session:
        logs = (
            session.query(BotLog)
            .order_by(BotLog.timestamp.desc())
            .limit(limit)
            .all()
        )
        return [l.to_dict() for l in logs]


@app.get("/api/signals")
async def get_signals(limit: int = 50):
    """Get recent strategy signals (audit trail)."""
    with db_session(state.db_factory) as session:
        signals = (
            session.query(SignalHistory)
            .order_by(SignalHistory.timestamp.desc())
            .limit(limit)
            .all()
        )
        return [s.to_dict() for s in signals]


# ──────────────────────────────────────────────
#  WebSocket Endpoint
# ──────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Real-time WebSocket for dashboard updates."""
    await state.ws_hub.connect(ws)
    try:
        while True:
            # Keep connection alive; client can send ping messages
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({"type": "pong", "timestamp": datetime.now(timezone.utc).isoformat()})
            elif data == "status":
                # Client requests immediate status update
                balance = await state.execution.get_balance() if state.execution else None
                await ws.send_json({
                    "type": "status",
                    "bot_state": state.bot_state,
                    "bot_running": state.bot_running,
                    "balance": balance,
                    "active_trade": state.active_trade.to_dict() if state.active_trade else None,
                    "latest_snapshot": state.latest_snapshot,
                    "latest_confluence": state.latest_confluence,
                    "risk_status": state.risk.get_status() if state.risk else None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
    except WebSocketDisconnect:
        state.ws_hub.disconnect(ws)
    except Exception as e:
        logger.error("WS error: %s", e)
        state.ws_hub.disconnect(ws)


# ──────────────────────────────────────────────
#  Main entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=APP.host,
        port=APP.port,
        log_level="info",
        ws_ping_interval=20,
        ws_ping_timeout=60,
    )