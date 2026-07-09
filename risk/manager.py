"""
risk/manager.py
===============
RiskManager — extracted from main.py as a standalone module.
Handles: position sizing, SL/TP calculation, trailing stop, circuit breaker.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import sessionmaker

from core.config import RISK, ROUND_TRIP_FEE
from core.models import DailyStats, db_session

logger = logging.getLogger("risk")


class RiskManager:
    """
    Enforces strict risk rules:
      • Position sizing: min(available_usdt, max_allowed) with 1% max risk
      • Stop-loss: 0.3%-0.5% below entry or below recent swing low
      • Take-profit: minimum 1:1.5 R:R and >=0.5% gross profit
      • Trailing stop: move to break-even at 1R profit
      • Daily circuit breaker: 3 losses -> 24h halt
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
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stats = session.query(DailyStats).filter_by(date=today).first()
        if not stats:
            stats = DailyStats(date=today, net_pnl_usdt=0.0)
            session.add(stats)
        stats.consecutive_losses = self._consecutive_losses
        stats.trades_total = self._trades_today
        stats.halted = self._halted
        stats.halt_until = self._halt_until
        if stats.net_pnl_usdt is None:
            stats.net_pnl_usdt = 0.0
        return stats

    def can_trade(self) -> tuple[bool, str]:
        if self._halted:
            if self._halt_until:
                remaining = self._halt_until - datetime.now(timezone.utc)
                if remaining.total_seconds() > 0:
                    return False, f"Circuit breaker active — halted until {self._halt_until.isoformat()}"
                else:
                    self._halted = False
                    self._halt_until = None
                    self._consecutive_losses = 0
                    with db_session(self.db_factory) as session:
                        self._save_daily_state(session)
            else:
                return False, "Circuit breaker active (no expiry set)"
        return True, "OK"

    def calculate_position_size(
        self, available_usdt: float, entry_price: float, sl_price: float
    ) -> dict:
        sl_dist_pct = abs(entry_price - sl_price) / entry_price
        max_pos_usdt = available_usdt * RISK.max_position_pct
        size_usdt = min(max_pos_usdt, available_usdt)
        risk_usdt = size_usdt * sl_dist_pct
        risk_pct = risk_usdt / available_usdt if available_usdt > 0 else 0

        if risk_pct > RISK.max_risk_per_trade_pct:
            size_usdt = available_usdt * RISK.max_risk_per_trade_pct / sl_dist_pct
            size_usdt = min(size_usdt, max_pos_usdt)
            risk_usdt = size_usdt * sl_dist_pct
            risk_pct = risk_usdt / available_usdt if available_usdt > 0 else 0

        qty_btc = size_usdt / entry_price if entry_price > 0 else 0
        return {
            "size_usdt": round(size_usdt, 2),
            "quantity_btc": round(qty_btc, 8),
            "sl_pct": round(sl_dist_pct * 100, 4),
            "risk_usdt": round(risk_usdt, 2),
            "risk_pct": round(risk_pct * 100, 4),
        }

    def calculate_sl_tp(
        self, entry_price: float, recent_swing_low: Optional[float] = None
    ) -> dict:
        sl_default = entry_price * (1 - RISK.sl_default_pct)
        if recent_swing_low and recent_swing_low < entry_price:
            swing_sl = recent_swing_low * 0.999
            sl_price = max(sl_default, swing_sl)
        else:
            sl_price = sl_default

        sl_min = entry_price * (1 - RISK.sl_min_pct)
        sl_max = entry_price * (1 - RISK.sl_max_pct)
        sl_price = max(sl_max, min(sl_min, sl_price))

        sl_dist = entry_price - sl_price
        sl_pct = sl_dist / entry_price

        tp_rr = entry_price + (sl_dist * RISK.min_rr_ratio)
        tp_min = entry_price * (1 + RISK.min_gross_profit_pct)
        tp_price = max(tp_rr, tp_min)
        tp_dist = tp_price - entry_price
        tp_pct = tp_dist / entry_price
        be_price = entry_price * (1 + ROUND_TRIP_FEE)

        return {
            "sl_price": round(sl_price, 2),
            "tp_price": round(tp_price, 2),
            "sl_pct": round(sl_pct * 100, 4),
            "tp_pct": round(tp_pct * 100, 4),
            "rr_ratio": round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0,
            "breakeven_price": round(be_price, 2),
        }

    def update_trailing_stop(
        self, entry_price: float, current_price: float,
        current_sl: float, breakeven_price: float,
        original_sl_price: Optional[float] = None,
    ) -> Optional[float]:
        """
        Trailing stop logic.
        original_sl_price: the initial SL set at entry (if None, uses current_sl).
        """
        orig_sl = original_sl_price if original_sl_price is not None else current_sl
        r_dist = entry_price - orig_sl  # 1R = distance from entry to original SL
        if r_dist <= 0:
            return None

        r1_level = entry_price + r_dist  # 1R profit target

        if current_price < r1_level:
            return None  # haven't reached 1R yet

        # At/above 1R: move to break-even if not already there
        if current_sl < breakeven_price:
            logger.info("Trailing: SL -> break-even @ %s", breakeven_price)
            return breakeven_price

        # Above BE: trail at current_price - 1R (lock in profit)
        trail_level = current_price - r_dist
        if trail_level > current_sl:
            logger.info("Trailing: SL -> %s (locking profit)", trail_level)
            return round(trail_level, 2)

        return None

    def record_trade_result(self, net_pnl: float) -> None:
        self._trades_today += 1
        if net_pnl < 0:
            self._consecutive_losses += 1
            logger.warning("Loss — consecutive: %d/%d", self._consecutive_losses, RISK.max_daily_losses)
            if self._consecutive_losses >= RISK.max_daily_losses:
                self._halted = True
                self._halt_until = datetime.now(timezone.utc) + timedelta(hours=RISK.cooldown_hours)
                logger.error("⚠️ CIRCUIT BREAKER — halted until %s", self._halt_until.isoformat())
        else:
            self._consecutive_losses = 0

        with db_session(self.db_factory) as session:
            stats = self._save_daily_state(session)
            stats.net_pnl_usdt = (stats.net_pnl_usdt or 0.0) + net_pnl
            if net_pnl > 0:
                stats.wins = (stats.wins or 0) + 1
            else:
                stats.losses = (stats.losses or 0) + 1

    def get_status(self) -> dict:
        return {
            "halted": self._halted,
            "halt_until": self._halt_until.isoformat() if self._halt_until else None,
            "consecutive_losses": self._consecutive_losses,
            "max_daily_losses": RISK.max_daily_losses,
            "trades_today": self._trades_today,
            "can_trade": self.can_trade()[0],
        }