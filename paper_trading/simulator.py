"""
paper_trading/simulator.py
=========================
PaperTradingEngine — simulates a real exchange for testing.
Implements: fees, slippage, partial fills, latency, order rejection.
Uses the same interface as ExecutionEngine so the bot logic is identical.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from core.config import SYMBOL, EFFECTIVE_MAKER_FEE, TradingMode

logger = logging.getLogger("paper_trading")


@dataclass
class PaperOrder:
    """Simulated order in the paper trading system."""
    id: str
    symbol: str
    side: str  # "buy" / "sell"
    type: str  # "limit" / "market"
    price: float
    quantity: float
    filled_qty: float = 0.0
    status: str = "open"  # open / filled / partial / cancelled / rejected
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    fill_price: float = 0.0
    fee_paid: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class PaperTradingEngine:
    """
    Simulated exchange for paper trading.
    Mimics: maker fees, slippage, partial fills, latency, order rejection.

    Uses the same public interface as execution.engine.ExecutionEngine
    so the bot can switch between live and paper without code changes.
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        mode: TradingMode = TradingMode.DEMO,
        use_bnb_fee: bool = True,
        initial_balance_usdt: float = 10000.0,
        initial_balance_btc: float = 0.0,
        slippage_pct: float = 0.0002,      # 0.02% default slippage
        latency_ms: int = 50,               # simulated execution latency
        rejection_rate: float = 0.02,       # 2% order rejection chance
        partial_fill_rate: float = 0.05,   # 5% chance of partial fill
    ):
        self.mode = mode
        self.use_bnb_fee = use_bnb_fee
        self.slippage_pct = slippage_pct
        self.latency_ms = latency_ms
        self.rejection_rate = rejection_rate
        self.partial_fill_rate = partial_fill_rate

        # Simulated balances
        self._balance_usdt = initial_balance_usdt
        self._balance_btc = initial_balance_btc
        self._used_usdt = 0.0
        self._used_btc = 0.0

        # Order tracking
        self._orders: dict[str, PaperOrder] = {}
        self._order_counter = 0

        # Current market price (set by the bot or test harness)
        self._current_price: float = 100000.0

        # Running flag
        self._running = False

    # ──────────────────────────────────────────────
    #  Lifecycle (same interface as ExecutionEngine)
    # ──────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        logger.info(
            "PaperTradingEngine started — balance: $%.2f USDT, ₿%.8f BTC",
            self._balance_usdt, self._balance_btc,
        )

    async def stop(self) -> None:
        self._running = False
        logger.info("PaperTradingEngine stopped")

    # ──────────────────────────────────────────────
    #  Market price injection (for testing)
    # ──────────────────────────────────────────────

    def set_market_price(self, price: float) -> None:
        """Update the current market price (called by bot loop or test harness)."""
        self._current_price = price

    @property
    def market_price(self) -> float:
        return self._current_price

    # ──────────────────────────────────────────────
    #  Simulated exchange properties
    # ──────────────────────────────────────────────

    @property
    def exchange(self):
        """Provide a minimal exchange-like object for compatibility with BotLoop."""
        return _PaperExchangeProxy(self)

    # ──────────────────────────────────────────────
    #  Order placement (same interface as ExecutionEngine)
    # ──────────────────────────────────────────────

    async def place_limit_buy(
        self, price: float, quantity: float, timeout_sec: int = 60
    ) -> Optional[dict]:
        """Place a simulated limit buy order."""
        if not self._running:
            return None

        # Simulate latency
        await asyncio.sleep(self.latency_ms / 1000.0)

        # Simulate order rejection
        if random.random() < self.rejection_rate:
            logger.warning("Paper: BUY order rejected (simulated)")
            return None

        # Check sufficient funds
        cost = price * quantity
        if cost > self._balance_usdt:
            logger.warning("Paper: insufficient USDT for buy (%.2f > %.2f)", cost, self._balance_usdt)
            return None

        # Simulate slippage on the fill price
        fill_price = price * (1 + self.slippage_pct)  # buy slips up
        fill_qty = self._simulate_fill_qty(quantity)

        if fill_qty <= 0:
            logger.warning("Paper: BUY order not filled (simulated timeout)")
            return None

        # Calculate fee
        fee = fill_qty * fill_price * EFFECTIVE_MAKER_FEE

        # Update balances
        self._balance_usdt -= (fill_qty * fill_price + fee)
        self._balance_btc += fill_qty

        # Create order record
        order_id = self._next_order_id()
        order = PaperOrder(
            id=order_id, symbol=SYMBOL, side="buy", type="limit",
            price=price, quantity=quantity, filled_qty=fill_qty,
            status="filled" if fill_qty >= quantity * 0.99 else "partial",
            fill_price=fill_price, fee_paid=fee,
        )
        self._orders[order_id] = order

        logger.info(
            "Paper BUY FILLED: %.8f BTC @ $%.2f (slip: %.4f%%) fee: $%.4f",
            fill_qty, fill_price, self.slippage_pct * 100, fee,
        )

        return {
            "order_id": order_id,
            "status": order.status,
            "filled": fill_qty,
            "average": fill_price,
            "fee": {"cost": fee, "currency": "USDT"},
        }

    async def place_limit_sell(
        self, price: float, quantity: float, timeout_sec: int = 120
    ) -> Optional[dict]:
        """Place a simulated limit sell order."""
        if not self._running:
            return None

        await asyncio.sleep(self.latency_ms / 1000.0)

        if random.random() < self.rejection_rate:
            logger.warning("Paper: SELL order rejected (simulated)")
            return None

        available_btc = self._balance_btc
        if quantity > available_btc:
            quantity = available_btc  # sell what we have

        if quantity <= 0:
            logger.warning("Paper: no BTC to sell")
            return None

        # Simulate slippage (sell slips down)
        fill_price = price * (1 - self.slippage_pct)
        fill_qty = self._simulate_fill_qty(quantity)

        if fill_qty <= 0:
            return None

        fee = fill_qty * fill_price * EFFECTIVE_MAKER_FEE

        # Update balances
        self._balance_btc -= fill_qty
        self._balance_usdt += (fill_qty * fill_price - fee)

        order_id = self._next_order_id()
        order = PaperOrder(
            id=order_id, symbol=SYMBOL, side="sell", type="limit",
            price=price, quantity=quantity, filled_qty=fill_qty,
            status="filled" if fill_qty >= quantity * 0.99 else "partial",
            fill_price=fill_price, fee_paid=fee,
        )
        self._orders[order_id] = order

        logger.info(
            "Paper SELL FILLED: %.8f BTC @ $%.2f fee: $%.4f",
            fill_qty, fill_price, fee,
        )

        return {
            "order_id": order_id,
            "status": order.status,
            "filled": fill_qty,
            "average": fill_price,
            "fee": {"cost": fee, "currency": "USDT"},
        }

    # ──────────────────────────────────────────────
    #  Order management
    # ──────────────────────────────────────────────

    async def cancel_order(self, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id].status = "cancelled"
            return True
        return False

    async def get_order_status(self, order_id: str) -> Optional[dict]:
        if order_id not in self._orders:
            return None
        o = self._orders[order_id]
        return {
            "order_id": order_id,
            "status": o.status,
            "filled": o.filled_qty,
            "remaining": o.quantity - o.filled_qty,
            "average": o.fill_price,
            "fee": {"cost": o.fee_paid, "currency": "USDT"},
        }

    async def cancel_all_open_orders(self) -> int:
        count = 0
        for o in self._orders.values():
            if o.status == "open":
                o.status = "cancelled"
                count += 1
        return count

    # ──────────────────────────────────────────────
    #  Balance (same interface as ExecutionEngine)
    # ──────────────────────────────────────────────

    async def get_balance(self) -> dict:
        return {
            "usdt_free": round(self._balance_usdt, 2),
            "usdt_used": round(self._used_usdt, 2),
            "usdt_total": round(self._balance_usdt + self._used_usdt, 2),
            "btc_free": round(self._balance_btc, 8),
            "btc_used": round(self._used_btc, 8),
            "btc_total": round(self._balance_btc + self._used_btc, 8),
        }

    # ──────────────────────────────────────────────
    #  Emergency sell
    # ──────────────────────────────────────────────

    async def emergency_sell_all(self) -> Optional[dict]:
        """Sell all BTC immediately at market price."""
        if self._balance_btc <= 0:
            return None

        fill_price = self._current_price * (1 - self.slippage_pct * 2)  # worse slippage for emergency
        fill_qty = self._balance_btc
        fee = fill_qty * fill_price * EFFECTIVE_MAKER_FEE

        self._balance_btc = 0.0
        self._balance_usdt += (fill_qty * fill_price - fee)

        order_id = self._next_order_id()
        order = PaperOrder(
            id=order_id, symbol=SYMBOL, side="sell", type="limit",
            price=fill_price, quantity=fill_qty, filled_qty=fill_qty,
            status="filled", fill_price=fill_price, fee_paid=fee,
        )
        self._orders[order_id] = order

        logger.warning(
            "Paper EMERGENCY SELL: %.8f @ $%.2f fee: $%.4f",
            fill_qty, fill_price, fee,
        )
        return {
            "order_id": order_id,
            "status": "filled",
            "filled": fill_qty,
            "average": fill_price,
            "fee": {"cost": fee, "currency": "USDT"},
        }

    # ──────────────────────────────────────────────
    #  Fee calculation (same interface as ExecutionEngine)
    # ──────────────────────────────────────────────

    def _calculate_fee(self, quantity: float, price: float) -> float:
        return quantity * price * EFFECTIVE_MAKER_FEE

    def calculate_round_trip_fee(self, quantity: float, price: float) -> float:
        from core.config import ROUND_TRIP_FEE
        return quantity * price * ROUND_TRIP_FEE

    # ──────────────────────────────────────────────
    #  Internal helpers
    # ──────────────────────────────────────────────

    def _next_order_id(self) -> str:
        self._order_counter += 1
        return f"PAPER-{self._order_counter:06d}"

    def _simulate_fill_qty(self, requested_qty: float) -> float:
        """Simulate partial fills."""
        if random.random() < self.partial_fill_rate:
            # Partial fill: 50-90% of requested
            fill_pct = random.uniform(0.5, 0.9)
            return requested_qty * fill_pct
        return requested_qty

    def _format_price(self, price: float) -> float:
        return round(price, 2)

    def _format_amount(self, amount: float) -> float:
        return round(amount, 8)


# ──────────────────────────────────────────────
#  Exchange proxy for BotLoop compatibility
# ──────────────────────────────────────────────

class _PaperExchangeProxy:
    """
    Minimal proxy that mimics the ccxt exchange object's fetch_ticker method.
    This allows BotLoop to use `self.s.execution.exchange.fetch_ticker(SYMBOL)` unchanged.
    """

    def __init__(self, engine: PaperTradingEngine):
        self._engine = engine

    async def fetch_ticker(self, symbol: str) -> dict:
        price = self._engine.market_price
        return {
            "symbol": symbol,
            "last": price,
            "bid": price * 0.9999,
            "ask": price * 1.0001,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }

    async def fetch_balance(self) -> dict:
        return {
            "USDT": {
                "free": self._engine._balance_usdt,
                "used": self._engine._used_usdt,
                "total": self._engine._balance_usdt + self._engine._used_usdt,
            },
            "BTC": {
                "free": self._engine._balance_btc,
                "used": self._engine._used_btc,
                "total": self._engine._balance_btc + self._engine._used_btc,
            },
        }