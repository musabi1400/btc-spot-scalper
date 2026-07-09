"""
tests/test_paper_trading.py
===========================
Unit tests for PaperTradingEngine — balance, orders, fees, slippage.
"""
import pytest
import asyncio
from datetime import datetime, timezone

from paper_trading.simulator import PaperTradingEngine, PaperOrder
from core.config import TradingMode, EFFECTIVE_MAKER_FEE


@pytest.fixture
def engine():
    e = PaperTradingEngine(
        initial_balance_usdt=10000.0,
        slippage_pct=0.0,      # disable slippage for deterministic tests
        latency_ms=0,           # no delay
        rejection_rate=0.0,    # no rejections
        partial_fill_rate=0.0, # no partial fills
    )
    return e


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self, engine):
        await engine.start()
        assert engine._running is True
        await engine.stop()
        assert engine._running is False


class TestBalance:
    @pytest.mark.asyncio
    async def test_initial_balance(self, engine):
        await engine.start()
        bal = await engine.get_balance()
        assert bal["usdt_free"] == 10000.0
        assert bal["btc_free"] == 0.0
        await engine.stop()

    @pytest.mark.asyncio
    async def test_balance_after_buy(self, engine):
        await engine.start()
        engine.set_market_price(100000.0)
        result = await engine.place_limit_buy(price=100000.0, quantity=0.01)
        assert result is not None
        bal = await engine.get_balance()
        # Should have less USDT (spent on BTC + fee)
        assert bal["usdt_free"] < 10000.0
        assert bal["btc_free"] == pytest.approx(0.01, rel=1e-6)
        await engine.stop()

    @pytest.mark.asyncio
    async def test_balance_after_round_trip(self, engine):
        await engine.start()
        engine.set_market_price(100000.0)
        # Buy
        buy = await engine.place_limit_buy(price=100000.0, quantity=0.01)
        assert buy is not None
        # Sell
        sell = await engine.place_limit_sell(price=101000.0, quantity=0.01)
        assert sell is not None
        bal = await engine.get_balance()
        # Should have profit (sold higher than bought) minus fees
        assert bal["btc_free"] == pytest.approx(0.0, abs=1e-10)
        assert bal["usdt_free"] > 10000.0  # profit
        await engine.stop()


class TestFees:
    @pytest.mark.asyncio
    async def test_buy_fee_charged(self, engine):
        await engine.start()
        engine.set_market_price(100000.0)
        result = await engine.place_limit_buy(price=100000.0, quantity=0.01)
        assert result is not None
        expected_fee = 0.01 * 100000.0 * EFFECTIVE_MAKER_FEE
        assert result["fee"]["cost"] == pytest.approx(expected_fee, rel=1e-6)
        await engine.stop()

    @pytest.mark.asyncio
    async def test_sell_fee_charged(self, engine):
        await engine.start()
        engine.set_market_price(100000.0)
        await engine.place_limit_buy(price=100000.0, quantity=0.01)
        result = await engine.place_limit_sell(price=101000.0, quantity=0.01)
        assert result is not None
        expected_fee = 0.01 * 101000.0 * EFFECTIVE_MAKER_FEE
        assert result["fee"]["cost"] == pytest.approx(expected_fee, rel=1e-6)
        await engine.stop()


class TestSlippage:
    @pytest.mark.asyncio
    async def test_slippage_on_buy(self):
        engine = PaperTradingEngine(
            slippage_pct=0.001,  # 0.1%
            latency_ms=0,
            rejection_rate=0.0,
            partial_fill_rate=0.0,
        )
        await engine.start()
        engine.set_market_price(100000.0)
        result = await engine.place_limit_buy(price=100000.0, quantity=0.01)
        assert result is not None
        # Fill price should be higher than limit due to slippage
        assert result["average"] > 100000.0
        await engine.stop()

    @pytest.mark.asyncio
    async def test_slippage_on_sell(self):
        engine = PaperTradingEngine(
            slippage_pct=0.001,
            latency_ms=0,
            rejection_rate=0.0,
            partial_fill_rate=0.0,
        )
        await engine.start()
        engine.set_market_price(100000.0)
        await engine.place_limit_buy(price=100000.0, quantity=0.01)
        result = await engine.place_limit_sell(price=101000.0, quantity=0.01)
        assert result is not None
        # Fill price should be lower than limit due to slippage
        assert result["average"] < 101000.0
        await engine.stop()


class TestInsufficientFunds:
    @pytest.mark.asyncio
    async def test_buy_exceeds_balance(self, engine):
        await engine.start()
        engine.set_market_price(100000.0)
        # Try to buy 1 BTC = $100,000 but only have $10,000
        result = await engine.place_limit_buy(price=100000.0, quantity=1.0)
        assert result is None  # rejected
        await engine.stop()


class TestEmergencySell:
    @pytest.mark.asyncio
    async def test_emergency_sell_all(self, engine):
        await engine.start()
        engine.set_market_price(100000.0)
        # Buy some BTC first
        await engine.place_limit_buy(price=100000.0, quantity=0.05)
        bal_before = await engine.get_balance()
        assert bal_before["btc_free"] > 0
        # Emergency sell
        result = await engine.emergency_sell_all()
        assert result is not None
        bal_after = await engine.get_balance()
        assert bal_after["btc_free"] == pytest.approx(0.0, abs=1e-10)
        await engine.stop()

    @pytest.mark.asyncio
    async def test_emergency_sell_no_btc(self, engine):
        await engine.start()
        result = await engine.emergency_sell_all()
        assert result is None  # nothing to sell
        await engine.stop()


class TestExchangeProxy:
    @pytest.mark.asyncio
    async def test_fetch_ticker(self, engine):
        await engine.start()
        engine.set_market_price(95000.0)
        ticker = await engine.exchange.fetch_ticker("BTC/USDT")
        assert ticker["last"] == 95000.0
        assert ticker["bid"] < ticker["ask"]
        await engine.stop()


class TestOrderRejection:
    @pytest.mark.asyncio
    async def test_rejection_simulation(self):
        engine = PaperTradingEngine(
            rejection_rate=1.0,  # 100% rejection
            latency_ms=0,
            slippage_pct=0.0,
            partial_fill_rate=0.0,
        )
        await engine.start()
        engine.set_market_price(100000.0)
        result = await engine.place_limit_buy(price=100000.0, quantity=0.01)
        assert result is None  # all orders rejected
        await engine.stop()