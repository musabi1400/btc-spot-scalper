"""
tests/test_enhanced_risk.py
=========================
Unit tests for EnhancedRiskManager — additional rules beyond RiskManager.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.models import Base
from risk.enhanced_manager import EnhancedRiskManager


@pytest.fixture
def db_factory():
    engine = create_engine("sqlite://", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def enhanced_risk(db_factory):
    return EnhancedRiskManager(
        db_factory,
        max_daily_loss_usdt=50.0,
        max_daily_trades=5,
        max_total_exposure_pct=0.50,
        cooldown_after_losses=2,
        cooldown_minutes=1,
    )


class TestEnhancedRiskManager:
    def test_can_trade_initially(self, enhanced_risk):
        can, reason = enhanced_risk.can_trade()
        assert can is True

    def test_max_daily_trades(self, enhanced_risk):
        for _ in range(5):
            enhanced_risk.record_trade_result(1.0)
        can, reason = enhanced_risk.can_trade()
        assert can is False
        assert "max daily trades" in reason.lower()

    def test_max_daily_loss_usdt(self, enhanced_risk):
        enhanced_risk.record_trade_result(-60.0)
        can, reason = enhanced_risk.can_trade()
        assert can is False
        assert "max daily loss" in reason.lower()

    def test_cooldown_after_losses(self, enhanced_risk):
        enhanced_risk.record_trade_result(-10.0)
        enhanced_risk.record_trade_result(-10.0)
        can, reason = enhanced_risk.can_trade()
        assert can is False
        assert "cooldown" in reason.lower()

    def test_check_market_conditions_normal(self, enhanced_risk):
        is_normal, reason = enhanced_risk.check_market_conditions({
            "price": 100000,
            "volume_ratio": 1.5,
            "rsi": 50,
        })
        assert is_normal is True

    def test_check_market_conditions_extreme_volume(self, enhanced_risk):
        is_normal, reason = enhanced_risk.check_market_conditions({
            "price": 100000,
            "volume_ratio": 6.0,
            "rsi": 50,
        })
        assert is_normal is False
        assert "volume" in reason.lower()

    def test_check_market_conditions_overbought(self, enhanced_risk):
        is_normal, reason = enhanced_risk.check_market_conditions({
            "price": 100000,
            "volume_ratio": 1.0,
            "rsi": 90,
        })
        assert is_normal is False
        assert "overbought" in reason.lower()

    def test_check_market_conditions_oversold(self, enhanced_risk):
        is_normal, reason = enhanced_risk.check_market_conditions({
            "price": 100000,
            "volume_ratio": 1.0,
            "rsi": 10,
        })
        assert is_normal is False
        assert "oversold" in reason.lower()

    def test_check_exposure_ok(self, enhanced_risk):
        can, reason = enhanced_risk.check_exposure(10000.0, 3000.0)
        assert can is True

    def test_check_exposure_exceeds(self, enhanced_risk):
        can, reason = enhanced_risk.check_exposure(10000.0, 6000.0)
        assert can is False
        assert "exposure" in reason.lower()

    def test_get_status_has_new_fields(self, enhanced_risk):
        status = enhanced_risk.get_status()
        assert "max_daily_loss_usdt" in status
        assert "max_daily_trades" in status
        assert "max_total_exposure_pct" in status
        assert "cooldown_until" in status

    def test_inherits_circuit_breaker(self, enhanced_risk):
        # Circuit breaker should still work from parent
        for _ in range(3):
            enhanced_risk.record_trade_result(-10.0)
        can, reason = enhanced_risk.can_trade()
        assert can is False
        # Should trigger either circuit breaker or max daily loss
        assert any(x in reason.lower() for x in ["halted", "circuit", "max daily"])