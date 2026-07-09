"""
notifications/alerts.py
=======================
AlertManager — manages alerts for important events and errors.
Supports: threshold-based alerts, error alerts, trade alerts.
Alerts are stored in DB and can be pushed via WebSocket.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Callable, Awaitable

from sqlalchemy import Column, DateTime, Integer, String, Text, Boolean
from sqlalchemy.orm import sessionmaker

from core.models import Base, db_session

logger = logging.getLogger("notifications")


# ──────────────────────────────────────────────
#  Alert ORM Model
# ──────────────────────────────────────────────

class Alert(Base):
    """Alert record stored in the database."""
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    level = Column(String(10), nullable=False)     # INFO / WARN / ERROR / CRITICAL
    category = Column(String(30), nullable=False)  # trade / risk / system / error
    title = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    context = Column(Text, nullable=True)           # JSON
    acknowledged = Column(Boolean, default=False, nullable=False)
    acknowledged_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "level": self.level,
            "category": self.category,
            "title": self.title,
            "message": self.message,
            "context": json.loads(self.context) if self.context else {},
            "acknowledged": self.acknowledged,
        }


# ──────────────────────────────────────────────
#  Alert Levels
# ──────────────────────────────────────────────

class AlertLevel(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AlertCategory(str, Enum):
    TRADE = "trade"
    RISK = "risk"
    SYSTEM = "system"
    ERROR = "error"


# ──────────────────────────────────────────────
#  Alert Manager
# ──────────────────────────────────────────────

class AlertManager:
    """
    Centralized alert system.
    Stores alerts in DB, optionally pushes via callback (e.g. WebSocket).
    """

    def __init__(self, db_factory: sessionmaker):
        self.db_factory = db_factory
        self._push_callback: Optional[Callable[[dict], Awaitable[None]]] = None
        self._thresholds: dict[str, float] = {
            "max_daily_loss_usdt": 100.0,
            "max_daily_loss_pct": 5.0,
            "max_drawdown_pct": 10.0,
            "min_win_rate_pct": 40.0,
            "max_consecutive_losses": 3,
            "min_balance_usdt": 100.0,
        }

    def set_push_callback(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        """Set an async callback to push alerts in real-time (e.g. WS broadcast)."""
        self._push_callback = callback

    def set_threshold(self, key: str, value: float) -> None:
        """Update an alert threshold."""
        self._thresholds[key] = value

    # ── Core: create alert ──

    async def create_alert(
        self,
        level: AlertLevel,
        category: AlertCategory,
        title: str,
        message: str,
        context: Optional[dict] = None,
    ) -> dict:
        """Create an alert, store in DB, and push if callback is set."""
        alert_data = {
            "type": "alert",
            "level": level.value,
            "category": category.value,
            "title": title,
            "message": message,
            "context": context or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Store in DB
        with db_session(self.db_factory) as session:
            alert = Alert(
                level=level.value,
                category=category.value,
                title=title,
                message=message,
                context=json.dumps(context) if context else None,
            )
            session.add(alert)
            session.flush()
            alert_data["id"] = alert.id

        # Log
        log_level = getattr(logging, level.value, logging.INFO)
        logger.log(log_level, f"[{category.value}] {title}: {message}")

        # Push via callback
        if self._push_callback:
            try:
                await self._push_callback(alert_data)
            except Exception as e:
                logger.error("Alert push failed: %s", e)

        return alert_data

    # ── Trade alerts ──

    async def alert_trade_entry(self, trade_data: dict) -> None:
        await self.create_alert(
            AlertLevel.INFO, AlertCategory.TRADE,
            "Trade Opened",
            f"BUY {trade_data.get('quantity_btc', 0)} BTC @ ${trade_data.get('entry_price', 0)}",
            trade_data,
        )

    async def alert_trade_exit(self, trade_data: dict) -> None:
        pnl = trade_data.get("net_pnl_usdt", 0)
        level = AlertLevel.INFO if pnl >= 0 else AlertLevel.WARN
        result = "PROFIT" if pnl >= 0 else "LOSS"
        await self.create_alert(
            level, AlertCategory.TRADE,
            f"Trade Closed ({result})",
            f"Net PnL: ${pnl:.4f} ({trade_data.get('return_pct', 0):.3f}%) | Reason: {trade_data.get('exit_reason', 'unknown')}",
            trade_data,
        )

    async def alert_circuit_breaker(self, halt_until: str) -> None:
        await self.create_alert(
            AlertLevel.CRITICAL, AlertCategory.RISK,
            "Circuit Breaker Triggered",
            f"Trading halted until {halt_until}",
            {"halt_until": halt_until},
        )

    # ── Risk alerts ──

    async def check_risk_thresholds(
        self,
        daily_pnl: float,
        balance: float,
        consecutive_losses: int,
        win_rate: float,
        drawdown_pct: float,
    ) -> list[dict]:
        """Check all risk thresholds and fire alerts if exceeded."""
        alerts = []

        if daily_pnl < -self._thresholds["max_daily_loss_usdt"]:
            alerts.append(await self.create_alert(
                AlertLevel.WARN, AlertCategory.RISK,
                "Daily Loss Threshold",
                f"Daily loss ${abs(daily_pnl):.2f} exceeds threshold ${self._thresholds['max_daily_loss_usdt']}",
                {"daily_pnl": daily_pnl},
            ))

        if balance < self._thresholds["min_balance_usdt"]:
            alerts.append(await self.create_alert(
                AlertLevel.ERROR, AlertCategory.RISK,
                "Low Balance",
                f"Balance ${balance:.2f} below minimum ${self._thresholds['min_balance_usdt']}",
                {"balance": balance},
            ))

        if consecutive_losses >= self._thresholds["max_consecutive_losses"]:
            alerts.append(await self.create_alert(
                AlertLevel.WARN, AlertCategory.RISK,
                "Consecutive Losses",
                f"{consecutive_losses} consecutive losses (threshold: {self._thresholds['max_consecutive_losses']})",
                {"consecutive_losses": consecutive_losses},
            ))

        if win_rate > 0 and win_rate < self._thresholds["min_win_rate_pct"]:
            alerts.append(await self.create_alert(
                AlertLevel.WARN, AlertCategory.RISK,
                "Low Win Rate",
                f"Win rate {win_rate:.1f}% below threshold {self._thresholds['min_win_rate_pct']}%",
                {"win_rate": win_rate},
            ))

        if drawdown_pct > self._thresholds["max_drawdown_pct"]:
            alerts.append(await self.create_alert(
                AlertLevel.ERROR, AlertCategory.RISK,
                "Max Drawdown Exceeded",
                f"Drawdown {drawdown_pct:.2f}% exceeds threshold {self._thresholds['max_drawdown_pct']}%",
                {"drawdown_pct": drawdown_pct},
            ))

        return alerts

    # ── System alerts ──

    async def alert_system_error(self, error: str, context: Optional[dict] = None) -> None:
        await self.create_alert(
            AlertLevel.ERROR, AlertCategory.ERROR,
            "System Error",
            error,
            context,
        )

    async def alert_engine_disconnected(self, engine_name: str) -> None:
        await self.create_alert(
            AlertLevel.CRITICAL, AlertCategory.SYSTEM,
            "Engine Disconnected",
            f"{engine_name} lost connection — auto-retry active",
            {"engine": engine_name},
        )

    # ── Query alerts ──

    def get_recent_alerts(self, limit: int = 50, unacknowledged_only: bool = False) -> list[dict]:
        """Get recent alerts from DB."""
        with db_session(self.db_factory) as session:
            query = session.query(Alert).order_by(Alert.timestamp.desc())
            if unacknowledged_only:
                query = query.filter(Alert.acknowledged == False)
            alerts = query.limit(limit).all()
            return [a.to_dict() for a in alerts]

    def acknowledge_alert(self, alert_id: int) -> bool:
        """Mark an alert as acknowledged."""
        with db_session(self.db_factory) as session:
            alert = session.get(Alert, alert_id)
            if alert:
                alert.acknowledged = True
                alert.acknowledged_at = datetime.now(timezone.utc)
                return True
            return False

    def acknowledge_all(self) -> int:
        """Acknowledge all unacknowledged alerts."""
        with db_session(self.db_factory) as session:
            count = session.query(Alert).filter(Alert.acknowledged == False).update(
                {Alert.acknowledged: True, Alert.acknowledged_at: datetime.now(timezone.utc)}
            )
            return count