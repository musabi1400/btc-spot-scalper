"""
tests/test_config.py
====================
Unit tests for core.config — verifies dataclass defaults and fee calculations.
"""
import pytest
from core.config import (
    StrategyConfig, RiskConfig, AppConfig, TradingMode,
    EFFECTIVE_MAKER_FEE, ROUND_TRIP_FEE, BNB_DISCOUNT, USE_BNB_FEE,
    MAKER_FEE_RATE, TAKER_FEE_RATE,
)


class TestStrategyConfig:
    def test_defaults(self):
        s = StrategyConfig()
        assert s.ema_fast == 9
        assert s.ema_mid == 21
        assert s.ema_slow == 50
        assert s.rsi_period == 14
        assert s.rsi_lower == 40.0
        assert s.rsi_upper == 60.0
        assert s.volume_spike_multiplier == 1.5
        assert s.min_confluence_score == 3
        assert s.total_conditions == 5

    def test_frozen(self):
        s = StrategyConfig()
        with pytest.raises(AttributeError):
            s.ema_fast = 100


class TestRiskConfig:
    def test_defaults(self):
        r = RiskConfig()
        assert r.max_risk_per_trade_pct == 0.01
        assert r.max_position_pct == 0.30
        assert r.sl_min_pct == 0.003
        assert r.sl_max_pct == 0.005
        assert r.min_rr_ratio == 1.5
        assert r.min_gross_profit_pct == 0.005
        assert r.max_daily_losses == 3
        assert r.cooldown_hours == 24
        assert r.max_concurrent_trades == 1

    def test_frozen(self):
        r = RiskConfig()
        with pytest.raises(AttributeError):
            r.max_daily_losses = 10


class TestFeeCalculation:
    def test_effective_maker_fee_with_bnb(self):
        if USE_BNB_FEE:
            expected = MAKER_FEE_RATE * (1 - BNB_DISCOUNT)
            assert EFFECTIVE_MAKER_FEE == pytest.approx(expected)
            assert EFFECTIVE_MAKER_FEE == pytest.approx(0.00075)

    def test_round_trip_fee(self):
        assert ROUND_TRIP_FEE == pytest.approx(EFFECTIVE_MAKER_FEE * 2)

    def test_taker_fee_unchanged(self):
        assert TAKER_FEE_RATE == 0.001


class TestTradingMode:
    def test_enum_values(self):
        assert TradingMode.DEMO.value == "demo"
        assert TradingMode.LIVE.value == "live"

    def test_string_equality(self):
        assert TradingMode("demo") == TradingMode.DEMO
        assert TradingMode("live") == TradingMode.LIVE