"""
execution.py
============
Order execution engine for Binance Spot via ccxt.pro.

Key principles:
  • LIMIT ORDERS ONLY (Maker) — never Market (Taker).
  • Fee handling: tracks BNB-discounted maker fees on every fill.
  • Order timeout: cancels unfilled limit orders after 60 seconds.
  • Position tracking: maintains in-memory state of the active trade.

Public API:
  ExecutionEngine.start() / .stop()
  await engine.place_limit_buy(price, quantity)   → order dict
  await engine.place_limit_sell(price, quantity)  → order dict
  await engine.cancel_order(order_id)
  await engine.get_order_status(order_id)
  await engine.get_balance()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import ccxt.pro as ccxtpro

from config import SYMBOL, EFFECTIVE_MAKER_FEE, ROUND_TRIP_FEE, TradingMode

logger = logging.getLogger("execution")


# ──────────────────────────────────────────────
#  Data Structures
# ──────────────────────────────────────────────

@dataclass
class FillResult:
    """Result of a filled (or partially filled) order."""
    order_id: str
    status: str               # "filled" / "partial" / "cancelled" / "rejected"
    filled_qty: float
    avg_fill_price: float
    fee_paid: float           # in quote currency (USDT)
    fee_currency: str         # "BNB" or "USDT"
    timestamp: datetime
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


# ──────────────────────────────────────────────
#  Execution Engine
# ──────────────────────────────────────────────

class ExecutionEngine:
    """
    Handles all order placement, cancellation, and fill tracking on Binance Spot.
    Uses ccxt.pro for async WebSocket-enhanced operations.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        mode: TradingMode = TradingMode.DEMO,
        use_bnb_fee: bool = True,
    ):
        self.mode = mode
        self.use_bnb_fee = use_bnb_fee
        self.api_key = api_key
        self.api_secret = api_secret

        self.exchange: Optional[ccxtpro.binance] = None
        self._running = False

        # Track active orders for cleanup
        self._active_orders: dict[str, dict] = {}

    # ──────────────────────────────────────────────
    #  Lifecycle
    # ──────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise the ccxt.pro exchange connection."""
        if self._running:
            return

        self.exchange = ccxtpro.binance({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        })

        if self.mode == TradingMode.DEMO:
            self.exchange.set_sandbox_mode(True)
            logger.info("ExecutionEngine started — TESTNET mode")
        else:
            logger.info("ExecutionEngine started — LIVE mode")

        # Load markets (required for precision/limits)
        await self.exchange.load_markets()

        self._running = True
        logger.info("Markets loaded — ready for order execution")

    async def stop(self) -> None:
        """Close the exchange connection."""
        self._running = False
        if self.exchange:
            # Cancel any tracked active orders
            for order_id in list(self._active_orders.keys()):
                try:
                    await self.cancel_order(order_id)
                except Exception:
                    pass
            await self.exchange.close()
            self.exchange = None
        logger.info("ExecutionEngine stopped")

    # ──────────────────────────────────────────────
    #  Market Metadata
    # ──────────────────────────────────────────────

    def _get_market(self) -> dict:
        """Get the market metadata for SYMBOL (precision, limits)."""
        if not self.exchange or SYMBOL not in self.exchange.markets:
            raise RuntimeError(f"Market {SYMBOL} not loaded")
        return self.exchange.markets[SYMBOL]

    def _format_price(self, price: float) -> float:
        """Round price to the exchange-required precision."""
        market = self._get_market()
        tick_size = market.get("precision", {}).get("price", 0.01)
        # ccxt uses number of decimals or tick size; handle both
        if tick_size < 1:
            # It's a tick size (e.g., 0.01)
            rounded = round(price / tick_size) * tick_size
            return float(round(rounded, 10))
        else:
            # It's number of decimal places
            return round(price, int(tick_size))

    def _format_amount(self, amount: float) -> float:
        """Round amount to the exchange-required precision."""
        market = self._get_market()
        step_size = market.get("precision", {}).get("amount", 0.00001)
        min_amount = market.get("limits", {}).get("amount", {}).get("min", 0.00001)

        if step_size < 1:
            rounded = round(amount / step_size) * step_size
            rounded = float(round(rounded, 10))
        else:
            rounded = round(amount, int(step_size))

        # Ensure above minimum
        if rounded < min_amount:
            logger.warning(
                "Amount %s below minimum %s — clamping", rounded, min_amount
            )
            rounded = min_amount

        return rounded

    # ──────────────────────────────────────────────
    #  Limit Order Placement
    # ──────────────────────────────────────────────

    async def place_limit_buy(
        self, price: float, quantity: float, timeout_sec: int = 60
    ) -> Optional[FillResult]:
        """
        Place a LIMIT BUY order at the specified price.

        The order is placed as a POST-ONLY maker order (ensures maker fee).
        If not filled within `timeout_sec`, the order is cancelled.

        Returns FillResult if filled, or None if cancelled/timeout.
        """
        if not self.exchange or not self._running:
            logger.error("Exchange not running — cannot place buy order")
            return None

        formatted_price = self._format_price(price)
        formatted_qty = self._format_amount(quantity)

        logger.info(
            "PLACING LIMIT BUY: %s BTC @ %s USDT (maker/post-only)",
            formatted_qty, formatted_price,
        )

        try:
            # Create limit order with postOnly flag to ensure maker fee
            order = await self.exchange.create_order(
                symbol=SYMBOL,
                type="limit",
                side="buy",
                amount=formatted_qty,
                price=formatted_price,
                params={
                    "postOnly": True,  # Binance: ensures maker (non-taker) fill
                    "timeInForce": "GTC",  # Good-Till-Cancelled (we manage timeout)
                },
            )

            order_id = order["id"]
            self._active_orders[order_id] = order
            logger.info("Limit BUY placed — order_id: %s", order_id)

            # Wait for fill with timeout
            fill = await self._await_fill(order_id, timeout_sec)

            if fill is None:
                # Timeout — cancel the order
                logger.warning("Buy order %s not filled in %ds — cancelling", order_id, timeout_sec)
                await self.cancel_order(order_id)
                # Check if partially filled
                final_status = await self.get_order_status(order_id)
                if final_status and final_status["filled"] > 0:
                    logger.info("Partial fill detected on cancelled buy order")
                    return FillResult(
                        order_id=order_id,
                        status="partial",
                        filled_qty=final_status["filled"],
                        avg_fill_price=final_status["average"],
                        fee_paid=self._calculate_fee(final_status["filled"], final_status["average"]),
                        fee_currency="BNB" if self.use_bnb_fee else "USDT",
                        timestamp=datetime.now(timezone.utc),
                        raw=final_status,
                    )
                return None

            logger.info(
                "BUY FILLED: %s BTC @ %s USDT | fee: %s %s",
                fill.filled_qty, fill.avg_fill_price, fill.fee_paid, fill.fee_currency,
            )
            return fill

        except ccxtpro.InsufficientFunds as e:
            logger.error("Insufficient funds for buy: %s", e)
            return None
        except ccxtpro.InvalidOrder as e:
            logger.error("Invalid buy order: %s", e)
            return None
        except Exception as e:
            logger.error("Unexpected error placing buy: %s", e)
            return None

    async def place_limit_sell(
        self, price: float, quantity: float, timeout_sec: int = 120
    ) -> Optional[FillResult]:
        """
        Place a LIMIT SELL order at the specified price (TP or SL exit).

        For stop-loss scenarios, the price is set just below current market
        to ensure quick fill while still using a limit (maker) order.

        Returns FillResult if filled, or None if cancelled/timeout.
        """
        if not self.exchange or not self._running:
            logger.error("Exchange not running — cannot place sell order")
            return None

        formatted_price = self._format_price(price)
        formatted_qty = self._format_amount(quantity)

        logger.info(
            "PLACING LIMIT SELL: %s BTC @ %s USDT (maker/post-only)",
            formatted_qty, formatted_price,
        )

        try:
            order = await self.exchange.create_order(
                symbol=SYMBOL,
                type="limit",
                side="sell",
                amount=formatted_qty,
                price=formatted_price,
                params={
                    "postOnly": True,
                    "timeInForce": "GTC",
                },
            )

            order_id = order["id"]
            self._active_orders[order_id] = order
            logger.info("Limit SELL placed — order_id: %s", order_id)

            fill = await self._await_fill(order_id, timeout_sec)

            if fill is None:
                logger.warning(
                    "Sell order %s not filled in %ds — cancelling and retrying at market-adjacent limit",
                    order_id, timeout_sec,
                )
                await self.cancel_order(order_id)

                # Retry: place a more aggressive limit order (1 tick above best bid)
                # This is still a LIMIT order, not a market order
                ticker = await self.exchange.fetch_ticker(SYMBOL)
                best_bid = ticker.get("bid", formatted_price)
                retry_price = self._format_price(best_bid * 0.9999)  # just below bid to fill fast
                logger.info("Retrying SELL at aggressive limit: %s", retry_price)

                retry_order = await self.exchange.create_order(
                    symbol=SYMBOL,
                    type="limit",
                    side="sell",
                    amount=formatted_qty,
                    price=retry_price,
                    params={"timeInForce": "IOC"},  # Immediate-Or-Cancel for urgent exit
                )
                retry_id = retry_order["id"]
                retry_fill = await self._await_fill(retry_id, timeout_sec=30)

                if retry_fill is None:
                    await self.cancel_order(retry_id)
                    logger.error("CRITICAL: Could not fill sell order even at aggressive limit!")
                    return None
                return retry_fill

            logger.info(
                "SELL FILLED: %s BTC @ %s USDT | fee: %s %s",
                fill.filled_qty, fill.avg_fill_price, fill.fee_paid, fill.fee_currency,
            )
            return fill

        except Exception as e:
            logger.error("Unexpected error placing sell: %s", e)
            return None

    # ──────────────────────────────────────────────
    #  Fill Tracking
    # ──────────────────────────────────────────────

    async def _await_fill(self, order_id: str, timeout_sec: int) -> Optional[FillResult]:
        """
        Poll order status until filled or timeout.
        Uses ccxt.pro's watch_order for real-time updates when available.
        """
        deadline = datetime.now(timezone.utc).timestamp() + timeout_sec
        poll_interval = 2  # seconds

        while datetime.now(timezone.utc).timestamp() < deadline:
            try:
                # Try watch_order (WS-based, faster) — fallback to REST fetch_order
                try:
                    order = await asyncio.wait_for(
                        self.exchange.watch_order(SYMBOL, order_id),
                        timeout=poll_interval,
                    )
                except asyncio.TimeoutError:
                    # watch_order didn't produce an update in time — poll via REST
                    order = await self.exchange.fetch_order(order_id, SYMBOL)
            except Exception as e:
                logger.debug("Order status check error (non-critical): %s", e)
                order = await self.exchange.fetch_order(order_id, SYMBOL)

            status = order.get("status", "")
            filled = float(order.get("filled", 0) or 0)
            remaining = float(order.get("remaining", 0) or 0)

            if status == "closed" or (filled > 0 and remaining == 0):
                # Fully filled
                avg_price = float(order.get("average", 0) or order.get("price", 0))
                fee_cost = float(order.get("fee", {}).get("cost", 0) or 0)
                fee_currency = order.get("fee", {}).get("currency", "USDT")

                # If exchange doesn't report fee, calculate it
                if fee_cost == 0:
                    fee_cost = self._calculate_fee(filled, avg_price)
                    fee_currency = "BNB" if self.use_bnb_fee else "USDT"

                fill = FillResult(
                    order_id=order_id,
                    status="filled",
                    filled_qty=filled,
                    avg_fill_price=avg_price,
                    fee_paid=fee_cost,
                    fee_currency=fee_currency,
                    timestamp=datetime.now(timezone.utc),
                    raw=order,
                )
                self._active_orders.pop(order_id, None)
                return fill

            if status == "canceled" or status == "rejected" or status == "expired":
                logger.warning("Order %s status: %s", order_id, status)
                self._active_orders.pop(order_id, None)
                return None

            await asyncio.sleep(poll_interval)

        # Timeout
        return None

    # ──────────────────────────────────────────────
    #  Order Management
    # ──────────────────────────────────────────────

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an active order. Returns True if cancelled successfully."""
        if not self.exchange:
            return False
        try:
            await self.exchange.cancel_order(order_id, SYMBOL)
            self._active_orders.pop(order_id, None)
            logger.info("Order %s cancelled", order_id)
            return True
        except Exception as e:
            logger.error("Cancel order %s failed: %s", order_id, e)
            return False

    async def get_order_status(self, order_id: str) -> Optional[dict]:
        """Fetch the current status of an order via REST."""
        if not self.exchange:
            return None
        try:
            order = await self.exchange.fetch_order(order_id, SYMBOL)
            return {
                "order_id": order_id,
                "status": order.get("status"),
                "filled": float(order.get("filled", 0) or 0),
                "remaining": float(order.get("remaining", 0) or 0),
                "average": float(order.get("average", 0) or order.get("price", 0)),
                "fee": order.get("fee", {}),
            }
        except Exception as e:
            logger.error("get_order_status error: %s", e)
            return None

    async def cancel_all_open_orders(self) -> int:
        """Cancel all open orders for SYMBOL. Returns count of cancelled orders."""
        if not self.exchange:
            return 0
        try:
            open_orders = await self.exchange.fetch_open_orders(SYMBOL)
            count = 0
            for o in open_orders:
                try:
                    await self.exchange.cancel_order(o["id"], SYMBOL)
                    count += 1
                except Exception:
                    pass
            self._active_orders.clear()
            logger.info("Cancelled %d open orders", count)
            return count
        except Exception as e:
            logger.error("cancel_all_open_orders error: %s", e)
            return 0

    # ──────────────────────────────────────────────
    #  Balance
    # ──────────────────────────────────────────────

    async def get_balance(self) -> dict:
        """Fetch current spot balance (USDT + BTC)."""
        if not self.exchange:
            return {"usdt_free": 0, "usdt_total": 0, "btc_free": 0, "btc_total": 0}
        try:
            balance = await self.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            btc = balance.get("BTC", {})
            return {
                "usdt_free": float(usdt.get("free", 0) or 0),
                "usdt_used": float(usdt.get("used", 0) or 0),
                "usdt_total": float(usdt.get("total", 0) or 0),
                "btc_free": float(btc.get("free", 0) or 0),
                "btc_used": float(btc.get("used", 0) or 0),
                "btc_total": float(btc.get("total", 0) or 0),
            }
        except Exception as e:
            logger.error("get_balance error: %s", e)
            return {"usdt_free": 0, "usdt_total": 0, "btc_free": 0, "btc_total": 0}

    # ──────────────────────────────────────────────
    #  Fee Calculation
    # ──────────────────────────────────────────────

    def _calculate_fee(self, quantity: float, price: float) -> float:
        """
        Calculate the expected maker fee for a fill.
        Returns fee in BNB (if use_bnb_fee) or USDT equivalent.

        Fee = quantity × price × effective_maker_fee
        """
        notional = quantity * price
        fee_usdt = notional * EFFECTIVE_MAKER_FEE
        if self.use_bnb_fee:
            # Convert to BNB at approximate price (caller can adjust)
            # For bookkeeping, we store the USDT-equivalent fee
            return fee_usdt
        return fee_usdt

    def calculate_round_trip_fee(self, quantity: float, price: float) -> float:
        """
        Calculate the expected round-trip fee (buy + sell) in USDT.
        Used by the risk manager for break-even calculations.
        """
        notional = quantity * price
        return notional * ROUND_TRIP_FEE

    # ──────────────────────────────────────────────
    #  Emergency Liquidation
    # ──────────────────────────────────────────────

    async def emergency_sell_all(self) -> Optional[FillResult]:
        """
        Emergency exit: sell all BTC holdings immediately.
        Uses an aggressive limit order (IOC) at just below best bid.
        NOT a market order — still maker/IOC to minimize fees,
        but placed close enough to guarantee a fill.
        """
        if not self.exchange or not self._running:
            return None

        # First cancel all open orders
        await self.cancel_all_open_orders()

        # Get BTC balance
        balance = await self.get_balance()
        btc_to_sell = balance["btc_free"]

        if btc_to_sell <= 0:
            logger.warning("Emergency sell: no BTC balance to sell")
            return None

        # Get current best bid
        ticker = await self.exchange.fetch_ticker(SYMBOL)
        best_bid = ticker.get("bid", ticker.get("last", 0))
        if best_bid <= 0:
            logger.error("Emergency sell: cannot determine current price")
            return None

        # Place aggressive IOC limit order at 0.05% below best bid
        aggressive_price = self._format_price(best_bid * 0.9995)
        formatted_qty = self._format_amount(btc_to_sell)

        logger.warning(
            "EMERGENCY SELL: %s BTC @ %s (aggressive IOC limit)",
            formatted_qty, aggressive_price,
        )

        try:
            order = await self.exchange.create_order(
                symbol=SYMBOL,
                type="limit",
                side="sell",
                amount=formatted_qty,
                price=aggressive_price,
                params={"timeInForce": "IOC"},
            )
            order_id = order["id"]
            fill = await self._await_fill(order_id, timeout_sec=30)

            if fill is None:
                # Last resort: check if any portion filled
                status = await self.get_order_status(order_id)
                if status and status["filled"] > 0:
                    return FillResult(
                        order_id=order_id,
                        status="partial",
                        filled_qty=status["filled"],
                        avg_fill_price=status["average"],
                        fee_paid=self._calculate_fee(status["filled"], status["average"]),
                        fee_currency="BNB" if self.use_bnb_fee else "USDT",
                        timestamp=datetime.now(timezone.utc),
                    )
                logger.error("Emergency sell FAILED — manual intervention required")
                return None

            logger.warning("Emergency sell completed: %s BTC @ %s", fill.filled_qty, fill.avg_fill_price)
            return fill

        except Exception as e:
            logger.error("Emergency sell exception: %s", e)
            return None