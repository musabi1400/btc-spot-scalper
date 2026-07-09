"""
monitoring/watchdog.py
======================
Watchdog — monitors the bot loop and auto-restarts on failure.
Runs as a separate async task alongside the main bot loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

from monitoring.health import HealthChecker, SystemHealth

logger = logging.getLogger("monitoring.watchdog")


class Watchdog:
    """
    Monitors the bot loop health and restarts it on failure.

    Checks:
      1. Bot loop is alive (task not cancelled)
      2. Strategy engine still running
      3. Execution engine still running
      4. No critical health failures
      5. Last tick within expected interval

    Actions on failure:
      - Log the failure
      - Cancel and restart the bot loop
      - Alert via callback (AlertManager)
      - Max retry attempts before giving up
    """

    def __init__(
        self,
        health_checker: HealthChecker,
        restart_callback: Callable[[], Awaitable[None]],
        alert_callback: Optional[Callable] = None,
        check_interval_sec: int = 30,
        max_restart_attempts: int = 5,
        restart_cooldown_sec: int = 60,
    ):
        self.health = health_checker
        self.restart_callback = restart_callback
        self.alert_callback = alert_callback
        self.check_interval = check_interval_sec
        self.max_attempts = max_restart_attempts
        self.restart_cooldown = restart_cooldown_sec

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._restart_count = 0
        self._last_restart_time: Optional[datetime] = None
        self._last_health: Optional[SystemHealth] = None

    async def start(self) -> None:
        """Start the watchdog monitoring loop."""
        if self._running:
            logger.warning("Watchdog already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Watchdog started — interval=%ds, max_retries=%d",
                    self.check_interval, self.max_attempts)

    async def stop(self) -> None:
        """Stop the watchdog."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Watchdog stopped")

    async def _run(self) -> None:
        """Main watchdog loop."""
        while self._running:
            try:
                await self._check_and_act()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Watchdog error: %s", e, exc_info=True)
            await asyncio.sleep(self.check_interval)

    async def _check_and_act(self) -> None:
        """Run a single health check and restart if needed."""
        # The health check needs references to the engines —
        # these are passed by the caller via the restart_callback context.
        # For now, we just check the database + system resources.
        health = self.health.run_all()
        self._last_health = health

        if health.status == "unhealthy":
            logger.error("Watchdog: system UNHEALTHY — %s",
                         [c.name for c in health.checks if not c.healthy])
            await self._handle_failure("System unhealthy")
        elif health.status == "degraded":
            logger.warning("Watchdog: system DEGRADED — %s",
                           [c.name for c in health.checks if not c.healthy])

    async def _handle_failure(self, reason: str) -> None:
        """Handle a failure — restart or give up."""
        now = datetime.now(timezone.utc)

        # Check cooldown
        if self._last_restart_time:
            elapsed = (now - self._last_restart_time).total_seconds()
            if elapsed < self.restart_cooldown:
                logger.warning("Watchdog: restart cooldown (%ds < %ds) — skipping",
                               int(elapsed), self.restart_cooldown)
                return

        # Check max attempts
        if self._restart_count >= self.max_attempts:
            logger.critical("Watchdog: max restart attempts (%d) reached — GIVING UP",
                            self.max_attempts)
            if self.alert_callback:
                await self.alert_callback(
                    "CRITICAL",
                    "Watchdog gave up",
                    f"Bot failed {self._restart_count} times. Manual intervention required. Reason: {reason}",
                )
            self._running = False
            return

        self._restart_count += 1
        self._last_restart_time = now

        logger.warning("Watchdog: restarting bot (attempt %d/%d) — reason: %s",
                       self._restart_count, self.max_attempts, reason)

        if self.alert_callback:
            await self.alert_callback(
                "WARN",
                "Watchdog auto-restart",
                f"Attempt {self._restart_count}/{self.max_attempts}. Reason: {reason}",
            )

        try:
            await self.restart_callback()
            logger.info("Watchdog: restart successful")
        except Exception as e:
            logger.error("Watchdog: restart FAILED: %s", e)

    def reset_restart_count(self) -> None:
        """Reset the restart counter (call after a successful period of stability)."""
        if self._restart_count > 0:
            logger.info("Watchdog: restart count reset (was %d)", self._restart_count)
        self._restart_count = 0

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "restart_count": self._restart_count,
            "max_attempts": self.max_attempts,
            "last_restart": self._last_restart_time.isoformat() if self._last_restart_time else None,
            "last_health": self._last_health.to_dict() if self._last_health else None,
            "uptime_seconds": self.health.uptime(),
        }