"""
monitoring/health.py
====================
HealthChecker — system health checks for the BTC Scalper.
Checks: DB connectivity, exchange connectivity, strategy engine, execution engine,
        risk manager state, memory usage, disk space, uptime.
"""
from __future__ import annotations

import logging
import os
import psutil
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from core.models import db_session

logger = logging.getLogger("monitoring")


@dataclass
class HealthStatus:
    """Result of a single health check."""
    name: str
    healthy: bool
    message: str
    details: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SystemHealth:
    """Overall system health report."""
    status: str  # "healthy" / "degraded" / "unhealthy"
    checks: list[HealthStatus] = field(default_factory=list)
    uptime_seconds: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "checks": [c.to_dict() for c in self.checks],
            "uptime_seconds": round(self.uptime_seconds, 2),
            "timestamp": self.timestamp,
        }


class HealthChecker:
    """
    Run health checks on all system components.
    Designed to be called by a /health API endpoint and by the watchdog.
    """

    def __init__(self, db_factory: sessionmaker):
        self.db_factory = db_factory
        self._start_time = time.time()

    def uptime(self) -> float:
        return time.time() - self._start_time

    def check_database(self) -> HealthStatus:
        """Check database connectivity."""
        try:
            with db_session(self.db_factory) as session:
                session.execute(text("SELECT 1"))
            return HealthStatus("database", True, "Database connection OK")
        except Exception as e:
            return HealthStatus("database", False, f"Database error: {e}")

    def check_strategy_engine(self, strategy) -> HealthStatus:
        """Check if the strategy engine is running and has data."""
        if not strategy:
            return HealthStatus("strategy_engine", False, "StrategyEngine not initialised")
        if not strategy._running:
            return HealthStatus("strategy_engine", False, "StrategyEngine not running")
        if strategy._ohlcv_5m.empty:
            return HealthStatus("strategy_engine", False, "No 5m OHLCV data")
        if strategy.exchange is None:
            return HealthStatus("strategy_engine", False, "Exchange connection closed")
        return HealthStatus(
            "strategy_engine", True, "StrategyEngine running",
            {"ohlcv_5m_count": len(strategy._ohlcv_5m), "ohlcv_15m_count": len(strategy._ohlcv_15m)},
        )

    def check_execution_engine(self, execution) -> HealthStatus:
        """Check if the execution engine is running."""
        if not execution:
            return HealthStatus("execution_engine", False, "ExecutionEngine not initialised")
        if not execution._running:
            return HealthStatus("execution_engine", False, "ExecutionEngine not running")
        if execution.exchange is None:
            return HealthStatus("execution_engine", False, "Exchange connection closed")
        return HealthStatus("execution_engine", True, "ExecutionEngine running")

    def check_risk_manager(self, risk_manager) -> HealthStatus:
        """Check risk manager state."""
        if not risk_manager:
            return HealthStatus("risk_manager", False, "RiskManager not initialised")
        can_trade, reason = risk_manager.can_trade()
        if can_trade:
            return HealthStatus("risk_manager", True, "RiskManager active",
                                risk_manager.get_status())
        return HealthStatus("risk_manager", False, f"Trading halted: {reason}",
                            risk_manager.get_status())

    def check_memory(self, threshold_mb: float = 500) -> HealthStatus:
        """Check memory usage."""
        try:
            process = psutil.Process()
            mem_mb = process.memory_info().rss / 1024 / 1024
            healthy = mem_mb < threshold_mb
            return HealthStatus(
                "memory", healthy,
                f"Memory: {mem_mb:.1f} MB" + (" (OK)" if healthy else f" (exceeds {threshold_mb} MB)"),
                {"rss_mb": round(mem_mb, 1), "threshold_mb": threshold_mb},
            )
        except Exception as e:
            return HealthStatus("memory", True, f"Cannot check memory: {e}")

    def check_disk(self, path: str = ".", threshold_pct: float = 90) -> HealthStatus:
        """Check disk space."""
        try:
            usage = psutil.disk_usage(path)
            pct = (usage.used / usage.total) * 100
            healthy = pct < threshold_pct
            return HealthStatus(
                "disk", healthy,
                f"Disk: {pct:.1f}% used" + (" (OK)" if healthy else f" (exceeds {threshold_pct}%)"),
                {"used_pct": round(pct, 1), "free_gb": round(usage.free / 1024**3, 1)},
            )
        except Exception as e:
            return HealthStatus("disk", True, f"Cannot check disk: {e}")

    def check_cpu(self, threshold_pct: float = 80) -> HealthStatus:
        """Check CPU usage."""
        try:
            cpu = psutil.cpu_percent(interval=1)
            healthy = cpu < threshold_pct
            return HealthStatus(
                "cpu", healthy,
                f"CPU: {cpu:.1f}%" + (" (OK)" if healthy else f" (exceeds {threshold_pct}%)"),
                {"cpu_pct": round(cpu, 1)},
            )
        except Exception as e:
            return HealthStatus("cpu", True, f"Cannot check CPU: {e}")

    def run_all(self, strategy=None, execution=None, risk_manager=None) -> SystemHealth:
        """Run all health checks and return aggregated status."""
        checks = [
            self.check_database(),
            self.check_strategy_engine(strategy),
            self.check_execution_engine(execution),
            self.check_risk_manager(risk_manager),
            self.check_memory(),
            self.check_disk(),
            self.check_cpu(),
        ]

        failed = [c for c in checks if not c.healthy]
        if not failed:
            status = "healthy"
        elif len(failed) <= 2:
            status = "degraded"
        else:
            status = "unhealthy"

        return SystemHealth(
            status=status,
            checks=checks,
            uptime_seconds=self.uptime(),
        )