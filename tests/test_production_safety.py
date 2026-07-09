"""
tests/test_production_safety.py
=============================
Unit tests for ProductionSafety — order validation, mode verification, state recovery.
"""
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.models import Base, Trade, Settings, db_session
from core.config import TradingMode
from production import ProductionSafety


@pytest.fixture
def db_factory():
    engine = create_engine("sqlite://", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def safety(db_factory):
    return ProductionSafety(db_factory)


class TestModeVerification:
    def test_default_mode_is_demo(self, safety):
        mode = safety.get_current_mode()
        assert mode == TradingMode.DEMO

    def test_verify_mode_match(self, safety, db_factory):
        with db_session(db_factory) as session:
            s = session.query(Settings).first()
            if not s:
                s = Settings(id=1)
                session.add(s)
            s.mode = "demo"
        assert safety.verify_mode(TradingMode.DEMO) is True

    def test_verify_mode_mismatch(self, safety, db_factory):
        with db_session(db_factory) as session:
            s = session.query(Settings).first()
            if not s:
                s = Settings(id=1)
                session.add(s)
            s.mode = "live"
        assert safety.verify_mode(TradingMode.DEMO) is False


class TestOrderValidation:
    def test_valid_buy_order(self, safety):
        can, reason = safety.validate_order(
            "buy", 100000.0, 0.01, 5000.0, TradingMode.DEMO
        )
        assert can is True

    def test_invalid_price_zero(self, safety):
        can, reason = safety.validate_order("buy", 0, 0.01, 5000.0, TradingMode.DEMO)
        assert can is False
        assert "price" in reason.lower()

    def test_invalid_price_too_high(self, safety):
        can, reason = safety.validate_order("buy", 2_000_000, 0.01, 5000.0, TradingMode.DEMO)
        assert can is False

    def test_invalid_quantity_zero(self, safety):
        can, reason = safety.validate_order("buy", 100000.0, 0, 5000.0, TradingMode.DEMO)
        assert can is False

    def test_quantity_below_minimum(self, safety):
        can, reason = safety.validate_order("buy", 100000.0, 0.0000001, 5000.0, TradingMode.DEMO)
        assert can is False

    def test_insufficient_balance(self, safety):
        can, reason = safety.validate_order("buy", 100000.0, 0.1, 100.0, TradingMode.DEMO)
        assert can is False
        assert "balance" in reason.lower()

    def test_invalid_side(self, safety):
        can, reason = safety.validate_order("invalid", 100000.0, 0.01, 5000.0, TradingMode.DEMO)
        assert can is False

    def test_live_mode_max_order(self, safety):
        # Live mode has a max order size limit
        can, reason = safety.validate_order(
            "buy", 100000.0, 0.5, 100000.0, TradingMode.LIVE
        )
        # 100000 * 0.5 = 50000 > 10000 max
        assert can is False
        assert "LIVE" in reason

    def test_live_mode_valid_order(self, safety):
        can, reason = safety.validate_order(
            "buy", 100000.0, 0.05, 10000.0, TradingMode.LIVE
        )
        # 100000 * 0.05 = 5000 < 10000 max
        assert can is True


class TestStateRecovery:
    def test_no_open_trades(self, safety):
        result = safety.recover_active_trade()
        assert result is None

    def test_recover_open_trade(self, safety, db_factory):
        with db_session(db_factory) as session:
            trade = Trade(
                symbol="BTC/USDT", status="IN_TRADE",
                entry_price=100000.0, quantity_btc=0.01,
                position_size_usdt=1000.0,
                stop_loss_price=99600.0, take_profit_price=101000.0,
            )
            session.add(trade)
            session.flush()

        result = safety.recover_active_trade()
        assert result is not None
        assert result["status"] == "IN_TRADE"
        assert result["entry_price"] == 100000.0

    def test_close_orphaned_trade(self, safety, db_factory):
        with db_session(db_factory) as session:
            trade = Trade(
                symbol="BTC/USDT", status="IN_TRADE",
                entry_price=100000.0, quantity_btc=0.01,
            )
            session.add(trade)
            session.flush()
            trade_id = trade.id

        success = safety.close_orphaned_trade(trade_id, 99500.0, "recovery")
        assert success is True

        with db_session(db_factory) as session:
            trade = session.get(Trade, trade_id)
            assert trade.status == "CLOSED"
            assert trade.exit_reason == "recovery"
            assert trade.exit_price == 99500.0

    def test_close_nonexistent_trade(self, safety):
        success = safety.close_orphaned_trade(99999, 100000.0)
        assert success is False