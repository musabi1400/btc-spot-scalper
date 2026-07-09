"""
tests/test_alerts.py
====================
Unit tests for AlertManager — alert creation, thresholds, acknowledgement.
"""
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.models import Base
from notifications.alerts import AlertManager, Alert, AlertLevel, AlertCategory


@pytest.fixture
def db_factory():
    engine = create_engine("sqlite://", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def alert_mgr(db_factory):
    return AlertManager(db_factory)


class TestCreateAlert:
    @pytest.mark.asyncio
    async def test_create_basic_alert(self, alert_mgr):
        result = await alert_mgr.create_alert(
            AlertLevel.WARN, AlertCategory.RISK,
            "Test Alert", "This is a test message",
            {"key": "value"},
        )
        assert result["level"] == "WARN"
        assert result["title"] == "Test Alert"
        assert result["id"] > 0

    @pytest.mark.asyncio
    async def test_alert_stored_in_db(self, alert_mgr, db_factory):
        await alert_mgr.create_alert(
            AlertLevel.ERROR, AlertCategory.SYSTEM,
            "DB Test", "Stored in database",
        )
        alerts = alert_mgr.get_recent_alerts(limit=10)
        assert len(alerts) == 1
        assert alerts[0]["title"] == "DB Test"
        assert alerts[0]["level"] == "ERROR"


class TestTradeAlerts:
    @pytest.mark.asyncio
    async def test_trade_entry_alert(self, alert_mgr):
        await alert_mgr.alert_trade_entry({
            "quantity_btc": 0.01,
            "entry_price": 100000.0,
        })
        alerts = alert_mgr.get_recent_alerts()
        assert len(alerts) == 1
        assert "Trade Opened" in alerts[0]["title"]

    @pytest.mark.asyncio
    async def test_trade_exit_profit(self, alert_mgr):
        await alert_mgr.alert_trade_exit({
            "net_pnl_usdt": 5.0,
            "return_pct": 0.5,
            "exit_reason": "take_profit",
        })
        alerts = alert_mgr.get_recent_alerts()
        assert "PROFIT" in alerts[0]["title"]

    @pytest.mark.asyncio
    async def test_trade_exit_loss(self, alert_mgr):
        await alert_mgr.alert_trade_exit({
            "net_pnl_usdt": -3.0,
            "return_pct": -0.3,
            "exit_reason": "stop_loss",
        })
        alerts = alert_mgr.get_recent_alerts()
        assert "LOSS" in alerts[0]["title"]
        assert alerts[0]["level"] == "WARN"


class TestCircuitBreakerAlert:
    @pytest.mark.asyncio
    async def test_circuit_breaker(self, alert_mgr):
        await alert_mgr.alert_circuit_breaker("2026-01-01T00:00:00+00:00")
        alerts = alert_mgr.get_recent_alerts()
        assert len(alerts) == 1
        assert alerts[0]["level"] == "CRITICAL"
        assert "Circuit Breaker" in alerts[0]["title"]


class TestRiskThresholds:
    @pytest.mark.asyncio
    async def test_daily_loss_exceeded(self, alert_mgr):
        alert_mgr.set_threshold("max_daily_loss_usdt", 50.0)
        alerts = await alert_mgr.check_risk_thresholds(
            daily_pnl=-60.0, balance=10000.0,
            consecutive_losses=1, win_rate=60.0, drawdown_pct=5.0,
        )
        assert any(a["title"] == "Daily Loss Threshold" for a in alerts)

    @pytest.mark.asyncio
    async def test_low_balance(self, alert_mgr):
        alert_mgr.set_threshold("min_balance_usdt", 200.0)
        alerts = await alert_mgr.check_risk_thresholds(
            daily_pnl=0, balance=150.0,
            consecutive_losses=0, win_rate=60.0, drawdown_pct=5.0,
        )
        assert any(a["title"] == "Low Balance" for a in alerts)

    @pytest.mark.asyncio
    async def test_consecutive_losses(self, alert_mgr):
        alerts = await alert_mgr.check_risk_thresholds(
            daily_pnl=0, balance=10000.0,
            consecutive_losses=3, win_rate=60.0, drawdown_pct=5.0,
        )
        assert any(a["title"] == "Consecutive Losses" for a in alerts)

    @pytest.mark.asyncio
    async def test_no_alerts_when_healthy(self, alert_mgr):
        alerts = await alert_mgr.check_risk_thresholds(
            daily_pnl=10.0, balance=10000.0,
            consecutive_losses=0, win_rate=60.0, drawdown_pct=2.0,
        )
        assert len(alerts) == 0


class TestAcknowledge:
    @pytest.mark.asyncio
    async def test_acknowledge_single(self, alert_mgr):
        result = await alert_mgr.create_alert(
            AlertLevel.WARN, AlertCategory.RISK, "Test", "Message",
        )
        success = alert_mgr.acknowledge_alert(result["id"])
        assert success is True
        unack = alert_mgr.get_recent_alerts(unacknowledged_only=True)
        assert len(unack) == 0

    @pytest.mark.asyncio
    async def test_acknowledge_all(self, alert_mgr):
        for i in range(3):
            await alert_mgr.create_alert(
                AlertLevel.WARN, AlertCategory.RISK, f"Test {i}", "Message",
            )
        count = alert_mgr.acknowledge_all()
        assert count == 3
        unack = alert_mgr.get_recent_alerts(unacknowledged_only=True)
        assert len(unack) == 0


class TestPushCallback:
    @pytest.mark.asyncio
    async def test_push_callback_fired(self, alert_mgr):
        pushed = []
        async def callback(data):
            pushed.append(data)

        alert_mgr.set_push_callback(callback)
        await alert_mgr.create_alert(
            AlertLevel.INFO, AlertCategory.TRADE, "Push Test", "Message",
        )
        assert len(pushed) == 1
        assert pushed[0]["title"] == "Push Test"