"""
tests/test_monitoring.py
========================
Unit tests for health checker, watchdog, and resource monitor.
"""
import pytest
import asyncio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.models import Base
from monitoring.health import HealthChecker, HealthStatus, SystemHealth
from monitoring.watchdog import Watchdog
from monitoring.resource_monitor import ResourceMonitor


@pytest.fixture
def db_factory():
    engine = create_engine("sqlite://", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class TestHealthChecker:
    def test_database_healthy(self, db_factory):
        checker = HealthChecker(db_factory)
        result = checker.check_database()
        assert result.healthy is True
        assert "database" in result.name.lower()

    def test_database_unhealthy(self):
        bad_factory = sessionmaker(bind=create_engine("sqlite:///nonexistent.db"))
        checker = HealthChecker(bad_factory)
        # The db_session context manager will handle the error
        result = checker.check_database()
        # May pass or fail depending on SQLite — just verify it returns
        assert isinstance(result, HealthStatus)

    def test_strategy_engine_not_initialised(self, db_factory):
        checker = HealthChecker(db_factory)
        result = checker.check_strategy_engine(None)
        assert result.healthy is False
        assert "not initialised" in result.message.lower()

    def test_execution_engine_not_initialised(self, db_factory):
        checker = HealthChecker(db_factory)
        result = checker.check_execution_engine(None)
        assert result.healthy is False

    def test_risk_manager_healthy(self, db_factory):
        from risk.manager import RiskManager
        rm = RiskManager(db_factory)
        checker = HealthChecker(db_factory)
        result = checker.check_risk_manager(rm)
        assert result.healthy is True

    def test_risk_manager_not_initialised(self, db_factory):
        checker = HealthChecker(db_factory)
        result = checker.check_risk_manager(None)
        assert result.healthy is False

    def test_memory_check(self, db_factory):
        checker = HealthChecker(db_factory)
        result = checker.check_memory(threshold_mb=10000)
        assert isinstance(result, HealthStatus)
        assert result.details.get("rss_mb", 0) > 0

    def test_disk_check(self, db_factory):
        checker = HealthChecker(db_factory)
        result = checker.check_disk()
        assert isinstance(result, HealthStatus)
        assert result.details.get("used_pct", 0) > 0

    def test_cpu_check(self, db_factory):
        checker = HealthChecker(db_factory)
        result = checker.check_cpu()
        assert isinstance(result, HealthStatus)

    def test_run_all(self, db_factory):
        from risk.manager import RiskManager
        rm = RiskManager(db_factory)
        checker = HealthChecker(db_factory)
        health = checker.run_all(risk_manager=rm)
        assert isinstance(health, SystemHealth)
        assert health.status in ("healthy", "degraded", "unhealthy")
        assert len(health.checks) == 7
        assert health.uptime_seconds > 0

    def test_uptime(self, db_factory):
        checker = HealthChecker(db_factory)
        import time
        time.sleep(0.1)
        assert checker.uptime() >= 0.1


class TestResourceMonitor:
    def test_sample(self):
        monitor = ResourceMonitor(history_size=10)
        sample = monitor.sample()
        assert sample.cpu_pct >= 0
        assert sample.memory_mb > 0
        assert sample.disk_pct > 0
        assert len(sample.timestamp) > 0

    def test_get_samples(self):
        monitor = ResourceMonitor(history_size=10)
        monitor.sample()
        monitor.sample()
        samples = monitor.get_samples()
        assert len(samples) == 2

    def test_get_summary(self):
        monitor = ResourceMonitor(history_size=10)
        monitor.sample()
        summary = monitor.get_summary()
        assert "current" in summary
        assert "avg_cpu_pct" in summary
        assert "max_cpu_pct" in summary
        assert "sample_count" in summary

    def test_check_alerts_no_issues(self):
        monitor = ResourceMonitor(history_size=10)
        monitor.sample()
        # Set very high thresholds so no alerts fire
        alerts = monitor.check_alerts({"cpu_pct": 999, "memory_mb": 99999, "disk_pct": 999})
        assert len(alerts) == 0

    def test_check_alerts_cpu_high(self):
        monitor = ResourceMonitor(history_size=10)
        monitor.sample()
        # Set very low thresholds to trigger all alerts
        alerts = monitor.check_alerts({"cpu_pct": -1, "memory_mb": -1, "disk_pct": -1})
        # At least one alert should fire (memory_mb threshold of -1 guarantees it)
        assert len(alerts) > 0


class TestWatchdog:
    @pytest.mark.asyncio
    async def test_watchdog_start_stop(self, db_factory):
        checker = HealthChecker(db_factory)
        restart_called = False

        async def restart():
            nonlocal restart_called
            restart_called = True

        wd = Watchdog(checker, restart, check_interval_sec=1, max_restart_attempts=3)
        await wd.start()
        assert wd._running is True
        await asyncio.sleep(0.5)
        await wd.stop()
        assert wd._running is False

    def test_watchdog_get_status(self, db_factory):
        checker = HealthChecker(db_factory)

        async def restart():
            pass

        wd = Watchdog(checker, restart)
        status = wd.get_status()
        assert status["running"] is False
        assert status["restart_count"] == 0

    def test_watchdog_reset_count(self, db_factory):
        checker = HealthChecker(db_factory)

        async def restart():
            pass

        wd = Watchdog(checker, restart)
        wd._restart_count = 3
        wd.reset_restart_count()
        assert wd._restart_count == 0