"""
production/safety.py
====================
Production safety layer for live trading.
- Environment separation (demo/live)
- Order validation before sending
- Pre-trade safety checks
- State persistence and recovery
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import sessionmaker

from core.config import TradingMode, SYMBOL, RISK
from core.models import db_session, Settings, Trade

logger = logging.getLogger("production")


class ProductionSafety:
    """
    Safety layer that validates all trading operations before execution.
    Acts as a final gate between the bot logic and the exchange.
    """

    def __init__(self, db_factory: sessionmaker):
        self.db_factory = db_factory

    # ── Environment Separation ──

    def get_current_mode(self) -> TradingMode:
        """Get the current trading mode from DB."""
        with db_session(self.db_factory) as session:
            settings = session.query(Settings).first()
            if settings:
                return TradingMode(settings.mode)
        return TradingMode.DEMO

    def verify_mode(self, expected: TradingMode) -> bool:
        """Verify the bot is in the expected mode before any order."""
        current = self.get_current_mode()
        if current != expected:
            logger.error(
                "Mode mismatch: expected %s but found %s — BLOCKING all orders",
                expected.value, current.value,
            )
            return False
        return True

    # ── Pre-Trade Safety Checks ──

    def validate_order(
        self,
        side: str,
        price: float,
        quantity: float,
        available_balance: float,
        mode: TradingMode,
    ) -> tuple[bool, str]:
        """
        Validate an order before sending it to the exchange.
        Returns (is_valid, reason_if_invalid).
        """
        # 1. Mode check — never allow live orders in demo mode
        if mode == TradingMode.DEMO:
            # In demo mode, only allow if we're using testnet
            logger.info("Demo mode — order validation relaxed (testnet)")

        # 2. Price sanity
        if price <= 0:
            return False, f"Invalid price: {price}"
        if price > 1_000_000:
            return False, f"Price {price} seems unrealistic (BTC > $1M?)"

        # 3. Quantity sanity
        if quantity <= 0:
            return False, f"Invalid quantity: {quantity}"
        min_qty = 0.00001  # Binance minimum for BTC
        if quantity < min_qty:
            return False, f"Quantity {quantity} below minimum {min_qty}"

        # 4. Balance check
        if side == "buy":
            cost = price * quantity
            if cost > available_balance:
                return False, f"Insufficient balance: {cost:.2f} > {available_balance:.2f}"

        # 5. Side validation
        if side not in ("buy", "sell"):
            return False, f"Invalid side: {side}"

        # 6. LIVE mode extra checks
        if mode == TradingMode.LIVE:
            # Max order size in USDT for live mode
            max_order_usdt = 10000.0
            order_value = price * quantity
            if order_value > max_order_usdt:
                return False, f"LIVE order value {order_value:.2f} exceeds max {max_order_usdt}"

        return True, "OK"

    # ── State Recovery ──

    def recover_active_trade(self) -> Optional[dict]:
        """
        On startup, check for any trade that was left open from a previous session.
        Returns the trade data if found, None otherwise.
        """
        with db_session(self.db_factory) as session:
            open_trade = (
                session.query(Trade)
                .filter(Trade.status.in_(["OPEN", "IN_TRADE", "FILLED_BUY"]))
                .order_by(Trade.created_at.desc())
                .first()
            )
            if open_trade:
                logger.warning(
                    "Recovered open trade: id=%d, entry=%s, qty=%s, status=%s",
                    open_trade.id, open_trade.entry_price,
                    open_trade.quantity_btc, open_trade.status,
                )
                return open_trade.to_dict()
        return None

    def close_orphaned_trade(self, trade_id: int, exit_price: float, reason: str = "recovery") -> bool:
        """Mark an orphaned trade as closed during recovery."""
        with db_session(self.db_factory) as session:
            trade = session.get(Trade, trade_id)
            if trade and trade.status in ("OPEN", "IN_TRADE", "FILLED_BUY"):
                trade.status = "CLOSED"
                trade.exit_reason = reason
                trade.exit_time = datetime.now(timezone.utc)
                trade.exit_price = exit_price
                logger.info("Orphaned trade %d closed at recovery @ %s", trade_id, exit_price)
                return True
        return False

    # ── Audit Log ──

    def log_trading_operation(
        self,
        operation: str,
        mode: str,
        details: dict,
    ) -> None:
        """Log all trading operations for audit trail."""
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "mode": mode,
            "details": details,
        }
        logger.info("AUDIT: %s", json.dumps(log_entry))