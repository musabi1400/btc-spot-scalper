"""
analytics/performance.py
========================
PerformanceAnalyzer — synchronous performance analytics built on top of the
``trades`` and ``trade_analytics`` tables.

Every method uses the shared ``db_session`` context manager and queries
:class:`core.models.Trade` (and, where relevant, :class:`analytics.models
.TradeAnalytics`) via SQLAlchemy.

Metrics produced mirror the Phase-3 spec:

* ``daily_performance(date)``  — trades, wins, losses, net_pnl, win_rate,
  avg_trade, best_trade, worst_trade.
* ``monthly_performance(year, month)`` — same metrics aggregated monthly.
* ``trade_distribution(trades)`` — by hour, day-of-week, confluence score,
  exit reason.
* ``equity_curve(start_date, end_date)`` — list of (date, cumulative_pnl).
* ``streak_analysis(trades)`` — max win/loss streak and current streak.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

from analytics.models import TradeAnalytics
from core.models import Trade, db_session

logger = logging.getLogger("analytics.performance")


class PerformanceAnalyzer:
    """Synchronous trade-performance analytics.

    Parameters
    ----------
    db_factory
        A SQLAlchemy ``sessionmaker`` bound to the target database.
    """

    def __init__(self, db_factory: sessionmaker) -> None:
        self.db_factory = db_factory

    # ──────────────────────────────────────────────
    #  Internal helpers
    # ──────────────────────────────────────────────

    @staticmethod
    def _aggregate(trades: Sequence[Trade]) -> dict[str, Any]:
        """Compute the standard metric set from a sequence of closed trades."""
        pnls = [float(t.net_pnl_usdt or 0.0) for t in trades]
        total = len(trades)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        net_pnl = sum(pnls)
        win_rate = (len(wins) / total) if total else 0.0
        avg_trade = (net_pnl / total) if total else 0.0
        best = max(pnls) if pnls else 0.0
        worst = min(pnls) if pnls else 0.0
        return {
            "trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "net_pnl": round(net_pnl, 6),
            "win_rate": round(win_rate, 4),
            "avg_trade": round(avg_trade, 6),
            "best_trade": round(best, 6),
            "worst_trade": round(worst, 6),
        }

    @staticmethod
    def _to_datetime(d: Any) -> datetime:
        """Coerce *d* (str | date | datetime) to a timezone-aware datetime."""
        if isinstance(d, datetime):
            if d.tzinfo is None:
                return d.replace(tzinfo=timezone.utc)
            return d
        if isinstance(d, date):
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        if isinstance(d, str):
            # try ISO format first
            try:
                dt = datetime.fromisoformat(d)
            except ValueError:
                dt = datetime.strptime(d, "%Y-%m-%d")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        raise TypeError(f"Unsupported date type: {type(d)!r}")

    # ──────────────────────────────────────────────
    #  Public API
    # ──────────────────────────────────────────────

    def daily_performance(self, day: Any) -> dict[str, Any]:
        """Aggregate performance for a single UTC day.

        Parameters
        ----------
        day
            A ``date``, ``datetime``, or ISO ``str`` (``"YYYY-MM-DD"``).
        """
        dt = self._to_datetime(day)
        start = dt
        end = dt + timedelta(days=1)

        with db_session(self.db_factory) as session:
            trades = (
                session.query(Trade)
                .filter(Trade.status == "CLOSED")
                .filter(Trade.exit_time >= start)
                .filter(Trade.exit_time < end)
                .order_by(Trade.exit_time.asc())
                .all()
            )
            result = self._aggregate(trades)
            result["date"] = start.strftime("%Y-%m-%d")
        return result

    def monthly_performance(self, year: int, month: int) -> dict[str, Any]:
        """Aggregate performance for a whole calendar month."""
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

        with db_session(self.db_factory) as session:
            trades = (
                session.query(Trade)
                .filter(Trade.status == "CLOSED")
                .filter(Trade.exit_time >= start)
                .filter(Trade.exit_time < end)
                .order_by(Trade.exit_time.asc())
                .all()
            )
            result = self._aggregate(trades)
            result["year"] = year
            result["month"] = month
        return result

    def trade_distribution(self, trades: Sequence[Any]) -> dict[str, Any]:
        """Distribution stats for an arbitrary collection of trades.

        Parameters
        ----------
        trades
            Sequence of trade **dicts** (e.g. ``Trade.to_dict()`` output) or
            ORM ``Trade`` objects.

        Returns
        -------
        dict
            ``by_hour`` (24 buckets), ``by_day_of_week`` (7 buckets, Mon=0),
            ``by_confluence_score`` (0–5), ``by_exit_reason`` (free keys).
        """
        by_hour: dict[int, int] = {h: 0 for h in range(24)}
        by_dow: dict[int, int] = {d: 0 for d in range(7)}
        by_score: dict[int, int] = {s: 0 for s in range(6)}
        by_reason: dict[str, int] = {}

        for t in trades:
            # Accept both dicts and ORM objects.
            if isinstance(t, dict):
                exit_time_raw = t.get("exit_time")
                score = t.get("confluence_score")
                reason = t.get("exit_reason") or "unknown"
            else:
                exit_time_raw = getattr(t, "exit_time", None)
                score = getattr(t, "confluence_score", None)
                reason = getattr(t, "exit_reason", None) or "unknown"

            exit_dt = self._to_datetime(exit_time_raw) if exit_time_raw else None
            if exit_dt is not None:
                by_hour[exit_dt.hour] = by_hour.get(exit_dt.hour, 0) + 1
                by_dow[exit_dt.weekday()] = by_dow.get(exit_dt.weekday(), 0) + 1

            if score is not None:
                s = int(score)
                by_score[s] = by_score.get(s, 0) + 1

            r = str(reason)
            by_reason[r] = by_reason.get(r, 0) + 1

        return {
            "by_hour": by_hour,
            "by_day_of_week": by_dow,
            "by_confluence_score": by_score,
            "by_exit_reason": by_reason,
        }

    def equity_curve(
        self, start_date: Any, end_date: Any
    ) -> list[tuple[str, float]]:
        """Cumulative net PnL curve, one point per day.

        Parameters
        ----------
        start_date / end_date
            Inclusive bounds (``date``, ``datetime``, or ISO ``str``).

        Returns
        -------
        list[tuple[str, float]]
            List of ``(YYYY-MM-DD, cumulative_pnl)`` ordered ascending.
        """
        start = self._to_datetime(start_date)
        end = self._to_datetime(end_date)
        # include the whole end day
        end = end + timedelta(days=1)

        with db_session(self.db_factory) as session:
            trades = (
                session.query(Trade)
                .filter(Trade.status == "CLOSED")
                .filter(Trade.exit_time >= start)
                .filter(Trade.exit_time < end)
                .order_by(Trade.exit_time.asc())
                .all()
            )

        # Bucket PnL by day.
        daily_pnl: dict[str, float] = {}
        for t in trades:
            exit_dt = t.exit_time
            if exit_dt is None:
                continue
            if isinstance(exit_dt, datetime):
                if exit_dt.tzinfo is None:
                    exit_dt = exit_dt.replace(tzinfo=timezone.utc)
            else:
                exit_dt = self._to_datetime(exit_dt)
            day_key = exit_dt.strftime("%Y-%m-%d")
            daily_pnl[day_key] = daily_pnl.get(day_key, 0.0) + float(t.net_pnl_usdt or 0.0)

        # Build a continuous date range so every day appears (cumulative).
        curve: list[tuple[str, float]] = []
        cumulative = 0.0
        cur = start
        last_day = end - timedelta(days=1)
        while cur <= last_day:
            day_key = cur.strftime("%Y-%m-%d")
            cumulative += daily_pnl.get(day_key, 0.0)
            curve.append((day_key, round(cumulative, 6)))
            cur += timedelta(days=1)
        return curve

    def streak_analysis(self, trades: Sequence[Any]) -> dict[str, Any]:
        """Compute winning / losing streaks from a chronological trade list.

        Parameters
        ----------
        trades
            Sequence of trade **dicts** or ORM objects, already ordered by
            exit time ascending (caller's responsibility).

        Returns
        -------
        dict
            ``max_win_streak``, ``max_loss_streak``, ``current_streak``
            (positive int for an active win streak, negative for a loss
            streak, 0 if no trades).
        """
        if not trades:
            return {
                "max_win_streak": 0,
                "max_loss_streak": 0,
                "current_streak": 0,
            }

        max_win = 0
        max_loss = 0
        cur_win = 0
        cur_loss = 0

        for t in trades:
            if isinstance(t, dict):
                pnl = float(t.get("net_pnl_usdt", 0.0))
            else:
                pnl = float(getattr(t, "net_pnl_usdt", 0.0) or 0.0)

            if pnl > 0:
                cur_win += 1
                cur_loss = 0
                if cur_win > max_win:
                    max_win = cur_win
            else:
                cur_loss += 1
                cur_win = 0
                if cur_loss > max_loss:
                    max_loss = cur_loss

        if cur_win > 0:
            current = cur_win
        elif cur_loss > 0:
            current = -cur_loss
        else:
            current = 0

        return {
            "max_win_streak": max_win,
            "max_loss_streak": max_loss,
            "current_streak": current,
        }

    # ──────────────────────────────────────────────
    #  Convenience: fetch closed trades as dicts
    # ──────────────────────────────────────────────

    def get_closed_trades(
        self, start_date: Optional[Any] = None, end_date: Optional[Any] = None
    ) -> list[dict]:
        """Return closed trades (as dicts) ordered by exit time ascending."""
        with db_session(self.db_factory) as session:
            q = session.query(Trade).filter(Trade.status == "CLOSED")
            if start_date is not None:
                q = q.filter(Trade.exit_time >= self._to_datetime(start_date))
            if end_date is not None:
                q = q.filter(Trade.exit_time < self._to_datetime(end_date) + timedelta(days=1))
            q = q.order_by(Trade.exit_time.asc())
            return [t.to_dict() for t in q.all()]