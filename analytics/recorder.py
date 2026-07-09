"""
analytics/recorder.py
=====================
TradeRecorder — persists per-trade analytics into the ``trade_analytics``
table.

The recorder is the single entry point used by the execution / risk layers
to store everything that happened around a trade: the indicator snapshot
and confluence result at entry, execution timing, slippage, latency, exit
reasons, and any errors encountered.

It uses the same ``db_session`` context manager and session factory pattern
as the rest of the codebase (:func:`core.models.db_session`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import sessionmaker

from analytics.models import TradeAnalytics
from core.models import db_session

logger = logging.getLogger("analytics.recorder")


class TradeRecorder:
    """Record entry/exit/error analytics for each trade.

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
    def _get_or_create(
        session: Any, trade_id: Optional[int]
    ) -> TradeAnalytics:
        """Return the existing analytics row for *trade_id* or a new one."""
        record: Optional[TradeAnalytics] = None
        if trade_id is not None:
            record = (
                session.query(TradeAnalytics)
                .filter_by(trade_id=trade_id)
                .order_by(TradeAnalytics.id.desc())
                .first()
            )
        if record is None:
            record = TradeAnalytics(trade_id=trade_id)
            session.add(record)
            # flush so the row gets an id and becomes queryable
            session.flush()
        return record

    @staticmethod
    def _ensure_list_str(value: Any) -> list[str]:
        """Coerce *value* to a JSON-serialisable list of strings."""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]

    # ──────────────────────────────────────────────
    #  Public API
    # ──────────────────────────────────────────────

    def record_entry(
        self,
        trade_id: Optional[int],
        snapshot: dict,
        confluence: dict,
        execution_time_ms: float,
        slippage_pct: float,
        entry_price: Optional[float] = None,
        market_state: Optional[dict] = None,
    ) -> TradeAnalytics:
        """Record the entry leg of a trade.

        Parameters
        ----------
        trade_id
            FK to ``trades.id`` (may be ``None`` for pre-trade snapshots).
        snapshot
            Indicator snapshot dict (from ``IndicatorSnapshot.to_dict()``).
        confluence
            Confluence result dict (from ``ConfluenceResult.to_dict()``).
        execution_time_ms
            Time from signal to entry fill, in milliseconds.
        slippage_pct
            Slippage between intended and actual fill price, in percent.
        entry_price
            Actual entry fill price.  If ``None`` we try to read it from the
            snapshot dict (``snapshot["price"]``).
        market_state
            Optional extra market context (regime, spread, depth…).

        Returns
        -------
        TradeAnalytics
            The persisted (and committed) analytics row.
        """
        price = entry_price
        if price is None:
            price = float(snapshot.get("price", 0.0)) if snapshot else 0.0

        conditions_met = confluence.get("conditions", {}) if confluence else {}
        confluence_score = int(confluence.get("score", 0)) if confluence else 0

        with db_session(self.db_factory) as session:
            record = self._get_or_create(session, trade_id)
            record.entry_time = datetime.now(timezone.utc)
            record.entry_price = price
            record.indicators_snapshot = json.dumps(snapshot or {}, default=str)
            record.confluence_score = confluence_score
            met_keys = [k for k, v in conditions_met.items() if v]
            record.conditions_met = json.dumps(met_keys)
            record.market_state = json.dumps(market_state or {}, default=str)
            record.execution_time_ms = float(execution_time_ms)
            record.slippage_pct = float(slippage_pct)
            session.flush()
            # detach so callers can use the object after session close
            session.refresh(record)
            result = record
        logger.info(
            "Recorded entry analytics for trade_id=%s price=%s exec=%.1fms slip=%.4f%%",
            trade_id, price, execution_time_ms, slippage_pct,
        )
        return result

    def record_exit(
        self,
        trade_id: Optional[int],
        exit_reason: str,
        execution_time_ms: float,
        slippage_pct: float,
        latency_ms: float,
        exit_price: Optional[float] = None,
    ) -> TradeAnalytics:
        """Record the exit leg of a trade.

        Parameters
        ----------
        trade_id
            FK to ``trades.id``.
        exit_reason
            Why the trade was closed (take_profit / stop_loss / trailing /
            emergency / manual…).
        execution_time_ms
            Time from exit signal to exit fill, in milliseconds.
        slippage_pct
            Slippage on the exit fill, in percent.
        latency_ms
            Order round-trip latency in milliseconds.
        exit_price
            Actual exit fill price (optional).

        Returns
        -------
        TradeAnalytics
            The updated analytics row.
        """
        with db_session(self.db_factory) as session:
            record = self._get_or_create(session, trade_id)
            record.exit_time = datetime.now(timezone.utc)
            record.exit_reason = exit_reason
            if exit_price is not None:
                record.exit_price = float(exit_price)
            # execution_time_ms is reused on exit to mean exit execution time.
            # If the record already has an entry execution_time, keep it and
            # store exit execution separately in latency_ms.
            record.latency_ms = float(latency_ms)
            record.slippage_pct = (
                float(slippage_pct)
                if record.slippage_pct is None
                else float(record.slippage_pct)
            )
            session.flush()
            session.refresh(record)
            result = record
        logger.info(
            "Recorded exit analytics for trade_id=%s reason=%s exec=%.1fms latency=%.1fms",
            trade_id, exit_reason, execution_time_ms, latency_ms,
        )
        return result

    def record_error(
        self,
        trade_id: Optional[int],
        error: str,
        context: dict,
    ) -> TradeAnalytics:
        """Append an error to the analytics row's ``errors`` JSON list.

        Parameters
        ----------
        trade_id
            FK to ``trades.id`` (may be ``None`` for un-attributed errors).
        error
            Error message / exception string.
        context
            Additional structured context (what was happening, inputs…).

        Returns
        -------
        TradeAnalytics
            The updated analytics row.
        """
        with db_session(self.db_factory) as session:
            record = self._get_or_create(session, trade_id)
            existing = (
                json.loads(record.errors) if record.errors else []
            )
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "error": str(error),
                "context": context or {},
            }
            existing.append(entry)
            record.errors = json.dumps(existing, default=str)
            session.flush()
            session.refresh(record)
            result = record
        logger.warning(
            "Recorded error for trade_id=%s: %s", trade_id, error
        )
        return result

    # ──────────────────────────────────────────────
    #  Read helpers
    # ──────────────────────────────────────────────

    def get_analytics(self, trade_id: int) -> Optional[dict]:
        """Return the analytics record for *trade_id* as a dict, or ``None``."""
        with db_session(self.db_factory) as session:
            record = (
                session.query(TradeAnalytics)
                .filter_by(trade_id=trade_id)
                .order_by(TradeAnalytics.id.desc())
                .first()
            )
            return record.to_dict() if record else None

    def list_analytics(
        self, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """Return the most recent analytics rows as dicts."""
        with db_session(self.db_factory) as session:
            rows = (
                session.query(TradeAnalytics)
                .order_by(TradeAnalytics.id.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            return [r.to_dict() for r in rows]