"""
analytics/models.py
===================
SQLAlchemy ORM model for trade-analytics data.

The ``TradeAnalytics`` table stores per-trade execution analytics that are
not part of the core :class:`core.models.Trade` record — indicators snapshot,
confluence details, market state at entry, execution timing, slippage,
latency, and any errors encountered during the trade lifecycle.

The model re-uses the **same** declarative ``Base`` imported from
``core.models`` so that every table lives in one ``Base.metadata`` registry
and can be created with a single ``Base.metadata.create_all(engine)`` call.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Index,
    Engine,
)

from core.models import Base


# ──────────────────────────────────────────────
#  TradeAnalytics
# ──────────────────────────────────────────────

class TradeAnalytics(Base):
    """Per-trade analytics record — one row per executed trade.

    Captures everything that the strategy, execution, and risk layers
    produced for a single round-trip trade so that it can be replayed,
    analysed, and used to tune signal scoring weights.

    Attributes
    ----------
    trade_id
        Foreign key to :class:`core.models.Trade.id`.
    entry_time / exit_time
        Timestamps for entry and exit fills.
    entry_price / exit_price
        Fill prices for entry and exit legs.
    indicators_snapshot
        JSON blob of the :class:`strategy.engine.IndicatorSnapshot` dict.
    confluence_score
        Raw confluence score (0–5) at entry.
    conditions_met
        JSON list/array of which confluence conditions were met.
    market_state
        JSON blob of additional market context (regime, spread, depth…).
    execution_time_ms
        Time taken to place + fill the entry order, in milliseconds.
    slippage_pct
        Slippage between intended and actual fill price, in percent.
    latency_ms
        Network/order round-trip latency in milliseconds.
    errors
        JSON list of error dicts recorded during the trade lifecycle.
    created_at
        Row creation timestamp (UTC).
    """

    __tablename__ = "trade_analytics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(Integer, ForeignKey("trades.id"), nullable=True)

    entry_time = Column(DateTime, nullable=True)
    exit_time = Column(DateTime, nullable=True)
    entry_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)

    indicators_snapshot = Column(Text, nullable=True)   # JSON
    confluence_score = Column(Integer, nullable=True)
    conditions_met = Column(Text, nullable=True)          # JSON
    market_state = Column(Text, nullable=True)            # JSON

    execution_time_ms = Column(Float, nullable=True)
    slippage_pct = Column(Float, nullable=True)
    latency_ms = Column(Float, nullable=True)

    exit_reason = Column(String(30), nullable=True)      # duplicated for convenience
    errors = Column(Text, nullable=True)                  # JSON list

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # ── Serialisation ──

    def to_dict(self) -> dict[str, Any]:
        """Serialise the row to a plain dict (JSON-safe)."""
        return {
            "id": self.id,
            "trade_id": self.trade_id,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "indicators_snapshot": json.loads(self.indicators_snapshot)
            if self.indicators_snapshot
            else {},
            "confluence_score": self.confluence_score,
            "conditions_met": json.loads(self.conditions_met)
            if self.conditions_met
            else [],
            "market_state": json.loads(self.market_state) if self.market_state else {},
            "execution_time_ms": self.execution_time_ms,
            "slippage_pct": self.slippage_pct,
            "latency_ms": self.latency_ms,
            "exit_reason": self.exit_reason,
            "errors": json.loads(self.errors) if self.errors else [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ──────────────────────────────────────────────
#  Indices
# ──────────────────────────────────────────────

Index("ix_trade_analytics_trade_id", TradeAnalytics.trade_id)
Index("ix_trade_analytics_entry_time", TradeAnalytics.entry_time)


# ──────────────────────────────────────────────
#  Table initialisation helper
# ──────────────────────────────────────────────

def init_analytics_tables(engine: Engine) -> None:
    """Create the ``trade_analytics`` table if it does not already exist.

    Uses ``Base.metadata.create_all`` which is idempotent, so calling this
    function multiple times is safe.  All other tables registered on the
    shared ``Base`` (the core models) will also be created if missing.

    Parameters
    ----------
    engine
        A SQLAlchemy ``Engine`` bound to the target database.
    """
    Base.metadata.create_all(engine)