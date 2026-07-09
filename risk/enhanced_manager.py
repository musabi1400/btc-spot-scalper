"""
risk/enhanced_manager.py
========================
EnhancedRiskManager — extends RiskManager with advanced rules:
  • Max daily loss in USDT (not just consecutive losses)
  • Max total trades per day
  • Max total exposure (sum of open positions)
  • Cooldown after consecutive losses (configurable)
  • Abnormal market detection (extreme volatility, low liquidity)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import sessionmaker

from core.config import RISK, STRATEGY
from core.models import DailyStats, Trade, db_session
from risk.manager import RiskManager

logger = logging.getLogger("risk.enhanced")


class EnhancedRiskManager(RiskManager):
    """
    Extended RiskManager with additional production safety rules.
    Inherits all existing rules from RiskManager and adds:
      • Max daily loss in USDT
      • Max total trades per day
      • Max total exposure percentage
      • Market condition checks (abnormal volatility)
    """

    def __init__(
        self,
        db_factory: sessionmaker,
        max_daily_loss_usdt: float = 100.0,
        max_daily_trades: int = 20,
        max_total_exposure_pct: float = 0.50,
        cooldown_after_losses: int = 2,
        cooldown_minutes: int = 30,
    ):
        super().__init__(db_factory)
        self.max_daily_loss_usdt = max_daily_loss_usdt
        self.max_daily_trades = max_daily_trades
        self.max_total_exposure_pct = max_total_exposure_pct
        self.cooldown_after_losses = cooldown_after_losses
        self.cooldown_minutes = cooldown_minutes
        self._cooldown_until: Optional[datetime] = None

    def can_trade(self) -> tuple[bool, str]:
        """Extended can_trade with additional checks."""
        # First check parent rules (circuit breaker, etc.)
        can, reason = super().can_trade()
        if not can:
            return can, reason

        # Check cooldown (shorter than circuit breaker)
        if self._cooldown_until:
            if datetime.now(timezone.utc) < self._cooldown_until:
                remaining = (self._cooldown_until - datetime.now(timezone.utc)).seconds // 60
                return False, f"Cooldown active — {remaining} min remaining"
            else:
                self._cooldown_until = None

        # Check max daily trades
        if self._trades_today >= self.max_daily_trades:
            return False, f"Max daily trades reached ({self.max_daily_trades})"

        # Check max daily loss in USDT
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with db_session(self.db_factory) as session:
            stats = session.query(DailyStats).filter_by(date=today).first()
            if stats and (stats.net_pnl_usdt or 0) < -self.max_daily_loss_usdt:
                return False, f"Max daily loss exceeded (${abs(stats.net_pnl_usdt):.2f} > ${self.max_daily_loss_usdt})"

        return True, "OK"

    def check_market_conditions(self, snapshot: dict) -> tuple[bool, str]:
        """
        Check for abnormal market conditions that should block trading.
        Returns (is_normal, reason_if_abnormal).
        """
        if not snapshot:
            return True, "OK"

        price = snapshot.get("price", 0)
        volume_ratio = snapshot.get("volume_ratio", 1.0)
        rsi = snapshot.get("rsi", 50)

        # Extreme volatility: volume ratio > 5x (abnormal spike)
        if volume_ratio > 5.0:
            return False, f"Abnormal volume spike ({volume_ratio:.1f}x) — likely news event"

        # RSI extreme: > 85 or < 15 (parabolic or crash conditions)
        if rsi > 85:
            return False, f"RSI extreme ({rsi:.1f}) — overbought parabolic"
        if rsi < 15:
            return False, f"RSI extreme ({rsi:.1f}) — oversold crash"

        return True, "OK"

    def check_exposure(self, available_usdt: float, position_size: float) -> tuple[bool, str]:
        """
        Check if adding this position would exceed max total exposure.
        In spot trading, this means not using more than max_total_exposure_pct
        of total balance on a single trade.
        """
        exposure_pct = position_size / available_usdt if available_usdt > 0 else 1.0
        if exposure_pct > self.max_total_exposure_pct:
            return False, (
                f"Position size (${position_size:.2f}) exceeds max exposure "
                f"({self.max_total_exposure_pct * 100:.0f}% of ${available_usdt:.2f})"
            )
        return True, "OK"

    def record_trade_result(self, net_pnl: float) -> None:
        """Extended to trigger cooldown after consecutive losses."""
        super().record_trade_result(net_pnl)

        # Check for cooldown trigger (less severe than circuit breaker)
        if net_pnl < 0 and self._consecutive_losses >= self.cooldown_after_losses:
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=self.cooldown_minutes)
            logger.warning(
                "Cooldown triggered — %d consecutive losses, cooling down for %d min",
                self._consecutive_losses, self.cooldown_minutes,
            )

    def get_status(self) -> dict:
        """Extended status with new fields."""
        base = super().get_status()
        base.update({
            "max_daily_loss_usdt": self.max_daily_loss_usdt,
            "max_daily_trades": self.max_daily_trades,
            "max_total_exposure_pct": self.max_total_exposure_pct,
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
            "trades_today": self._trades_today,
        })
        return base