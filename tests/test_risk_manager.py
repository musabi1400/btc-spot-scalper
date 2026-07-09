"""
tests/test_risk_manager.py
=========================
Unit tests for RiskManager — SL/TP, position sizing, trailing stop, circuit breaker.
Uses in-memory SQLite to avoid file artifacts.
"""
import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.models import Base, DailyStats, build_session_factory
from risk.manager import RiskManager
from core.config import RISK, ROUND_TRIP_FEE


@pytest.fixture
def db_factory():
    engine = create_engine("sqlite://", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def risk_mgr(db_factory):
    return RiskManager(db_factory)


class TestCalculateSLTP:
    def test_default_sl_tp(self, risk_mgr):
        result = risk_mgr.calculate_sl_tp(100000.0)
        assert result["sl_price"] < 100000.0
        assert result["tp_price"] > 100000.0
        assert result["rr_ratio"] >= 1.5
        assert result["sl_pct"] >= 0.3
        assert result["sl_pct"] <= 0.5

    def test_sl_clamped_to_range(self, risk_mgr):
        result = risk_mgr.calculate_sl_tp(100000.0)
        sl_pct = (100000.0 - result["sl_price"]) / 100000.0 * 100
        assert sl_pct >= RISK.sl_min_pct * 100 - 0.01
        assert sl_pct <= RISK.sl_max_pct * 100 + 0.01

    def test_tp_at_least_min_profit(self, risk_mgr):
        result = risk_mgr.calculate_sl_tp(100000.0)
        tp_pct = (result["tp_price"] - 100000.0) / 100000.0 * 100
        assert tp_pct >= RISK.min_gross_profit_pct * 100

    def test_breakeven_includes_fees(self, risk_mgr):
        result = risk_mgr.calculate_sl_tp(100000.0)
        expected_be = 100000.0 * (1 + ROUND_TRIP_FEE)
        assert result["breakeven_price"] == pytest.approx(expected_be, rel=1e-6)

    def test_swing_low_provided(self, risk_mgr):
        result = risk_mgr.calculate_sl_tp(100000.0, recent_swing_low=99600.0)
        assert result["sl_price"] < 100000.0
        assert result["sl_price"] > 99000.0


class TestPositionSizing:
    def test_basic_sizing(self, risk_mgr):
        result = risk_mgr.calculate_position_size(10000.0, 100000.0, 99600.0)
        assert result["size_usdt"] > 0
        assert result["quantity_btc"] > 0
        assert result["risk_usdt"] > 0
        assert result["risk_pct"] <= RISK.max_risk_per_trade_pct * 100 + 0.01

    def test_respects_max_position_pct(self, risk_mgr):
        result = risk_mgr.calculate_position_size(10000.0, 100000.0, 99600.0)
        assert result["size_usdt"] <= 10000.0 * RISK.max_position_pct + 0.01

    def test_zero_balance(self, risk_mgr):
        result = risk_mgr.calculate_position_size(0.0, 100000.0, 99600.0)
        assert result["size_usdt"] == 0
        assert result["quantity_btc"] == 0

    def test_risk_capped_at_1pct(self, risk_mgr):
        result = risk_mgr.calculate_position_size(10000.0, 100000.0, 99600.0)
        assert result["risk_pct"] <= 1.0 + 0.01


class TestTrailingStop:
    def test_no_update_below_1r(self, risk_mgr):
        entry = 100000.0
        sl = 99600.0  # 0.4% below
        be = entry * (1 + ROUND_TRIP_FEE)
        # Price at 0.2% profit (below 1R)
        result = risk_mgr.update_trailing_stop(entry, 100200.0, sl, be)
        assert result is None

    def test_move_to_breakeven_at_1r(self, risk_mgr):
        entry = 100000.0
        sl = 99600.0  # 1R = 400
        be = entry * (1 + ROUND_TRIP_FEE)
        # Price at 1R = 100400
        result = risk_mgr.update_trailing_stop(entry, 100400.0, sl, be)
        assert result is not None
        assert result == pytest.approx(be, rel=1e-6)

    def test_trail_beyond_breakeven(self, risk_mgr):
        entry = 100000.0
        sl = 99600.0
        be = entry * (1 + ROUND_TRIP_FEE)
        # First move to BE
        result = risk_mgr.update_trailing_stop(entry, 100400.0, sl, be, original_sl_price=sl)
        assert result == pytest.approx(be, rel=1e-6)
        # Now price goes higher — should trail
        new_sl = result
        result2 = risk_mgr.update_trailing_stop(entry, 101000.0, new_sl, be, original_sl_price=sl)
        assert result2 is not None
        assert result2 > new_sl  # SL moved up

    def test_never_move_sl_down(self, risk_mgr):
        entry = 100000.0
        sl = 99600.0
        be = entry * (1 + ROUND_TRIP_FEE)
        # Price below 1R — no update
        result = risk_mgr.update_trailing_stop(entry, 99800.0, sl, be)
        assert result is None


class TestCircuitBreaker:
    def test_can_trade_initially(self, risk_mgr):
        can, reason = risk_mgr.can_trade()
        assert can is True
        assert reason == "OK"

    def test_circuit_breaker_triggers(self, risk_mgr, db_factory):
        for i in range(RISK.max_daily_losses):
            risk_mgr.record_trade_result(-10.0)
        can, reason = risk_mgr.can_trade()
        assert can is False
        assert "halted" in reason.lower()

    def test_win_resets_consecutive_losses(self, risk_mgr):
        risk_mgr.record_trade_result(-10.0)
        risk_mgr.record_trade_result(-10.0)
        assert risk_mgr._consecutive_losses == 2
        risk_mgr.record_trade_result(10.0)
        assert risk_mgr._consecutive_losses == 0
        can, _ = risk_mgr.can_trade()
        assert can is True