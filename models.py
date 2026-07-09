"""
models.py
=========
SQLAlchemy ORM models for the BTC Scalper database.

Tables:
  - settings       : encrypted API credentials, mode, auto-trade flag
  - trades         : every executed trade with entry/exit/fees/PnL
  - bot_logs       : structured log entries (INFO/WARN/ERROR)
  - daily_stats    : per-day aggregate for circuit-breaker logic
  - signal_history : every strategy evaluation for auditability
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    Index,
)
from sqlalchemy.orm import sessionmaker, declarative_base, Session

from config import APP, TradingMode


Base = declarative_base()


# ──────────────────────────────────────────────
#  Settings  (single-row table)
# ──────────────────────────────────────────────

class Settings(Base):
    """Persistent application settings — encrypted credentials live here."""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, default=1)  # always row 1
    mode = Column(String(10), default=TradingMode.DEMO.value, nullable=False)
    api_key_encrypted = Column(Text, nullable=True)
    api_secret_encrypted = Column(Text, nullable=True)
    auto_trade = Column(Boolean, default=False, nullable=False)
    use_bnb_fee = Column(Boolean, default=True, nullable=False)
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ──────────────────────────────────────────────
#  Trades
# ──────────────────────────────────────────────

class Trade(Base):
    """Complete record of a single round-trip trade."""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Identifiers
    symbol = Column(String(20), default="BTC/USDT", nullable=False)
    order_id_buy = Column(String(64), nullable=True)
    order_id_sell = Column(String(64), nullable=True)

    # Entry
    side = Column(String(10), default="BUY", nullable=False)   # always BUY in spot
    entry_time = Column(DateTime, nullable=True)
    entry_price = Column(Float, nullable=True)
    quantity_btc = Column(Float, nullable=True)
    position_size_usdt = Column(Float, nullable=True)

    # Exit
    exit_time = Column(DateTime, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_reason = Column(String(30), nullable=True)  # take_profit / stop_loss / trailing / emergency

    # Risk params at entry
    stop_loss_price = Column(Float, nullable=True)
    take_profit_price = Column(Float, nullable=True)
    trailing_sl_price = Column(Float, nullable=True)
    sl_pct = Column(Float, nullable=True)
    tp_pct = Column(Float, nullable=True)

    # Fees & PnL
    fee_buy_usdt = Column(Float, default=0.0)
    fee_sell_usdt = Column(Float, default=0.0)
    gross_pnl_usdt = Column(Float, default=0.0)
    net_pnl_usdt = Column(Float, default=0.0)
    fees_total_usdt = Column(Float, default=0.0)
    return_pct = Column(Float, default=0.0)          # net % on position

    # Strategy context
    confluence_score = Column(Integer, nullable=True)
    conditions_met = Column(Text, nullable=True)     # JSON list of met conditions
    entry_5m_close = Column(Float, nullable=True)

    # Status
    status = Column(String(20), default="OPEN", nullable=False)
    # OPEN / FILLED_BUY / IN_TRADE / FILLED_SELL / CLOSED / CANCELLED / REJECTED

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        """Serialise for API/WebSocket responses."""
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "status": self.status,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "entry_price": self.entry_price,
            "quantity_btc": self.quantity_btc,
            "position_size_usdt": self.position_size_usdt,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "trailing_sl_price": self.trailing_sl_price,
            "net_pnl_usdt": self.net_pnl_usdt,
            "fees_total_usdt": self.fees_total_usdt,
            "return_pct": self.return_pct,
            "confluence_score": self.confluence_score,
            "conditions_met": json.loads(self.conditions_met) if self.conditions_met else [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ──────────────────────────────────────────────
#  Bot Logs
# ──────────────────────────────────────────────

class BotLog(Base):
    """Structured log entry for the dashboard's log feed."""
    __tablename__ = "bot_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    level = Column(String(10), default="INFO")   # INFO / WARN / ERROR / TRADE
    message = Column(Text, nullable=False)
    context = Column(Text, nullable=True)        # optional JSON blob

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "level": self.level,
            "message": self.message,
        }


# ──────────────────────────────────────────────
#  Daily Stats  (circuit-breaker tracking)
# ──────────────────────────────────────────────

class DailyStats(Base):
    """One row per UTC day — used by the circuit-breaker."""
    __tablename__ = "daily_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, unique=True)  # YYYY-MM-DD
    trades_total = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    consecutive_losses = Column(Integer, default=0)
    net_pnl_usdt = Column(Float, default=0.0)
    halted = Column(Boolean, default=False)        # circuit breaker triggered
    halt_until = Column(DateTime, nullable=True)   # resume trading after this time

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "trades_total": self.trades_total,
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
            "net_pnl_usdt": self.net_pnl_usdt,
            "halted": self.halted,
            "halt_until": self.halt_until.isoformat() if self.halt_until else None,
        }


# ──────────────────────────────────────────────
#  Signal History  (audit trail)
# ──────────────────────────────────────────────

class SignalHistory(Base):
    """Every strategy evaluation — whether it triggered a trade or not."""
    __tablename__ = "signal_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    price = Column(Float, nullable=False)
    confluence_score = Column(Integer, nullable=False)
    conditions_met = Column(Text, nullable=True)   # JSON
    conditions_detail = Column(Text, nullable=True) # JSON with per-condition values
    action = Column(String(20), nullable=False)    # EVAL / ENTER / SKIP / EXIT

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "price": self.price,
            "confluence_score": self.confluence_score,
            "conditions_met": json.loads(self.conditions_met) if self.conditions_met else [],
            "conditions_detail": json.loads(self.conditions_detail) if self.conditions_detail else {},
            "action": self.action,
        }


# ──────────────────────────────────────────────
#  Indices
# ──────────────────────────────────────────────

Index("ix_trades_status", Trade.status)
Index("ix_trades_created", Trade.created_at)
Index("ix_signal_timestamp", SignalHistory.timestamp)


# ──────────────────────────────────────────────
#  Database Engine & Session Factory
# ──────────────────────────────────────────────

def build_engine(dsn: Optional[str] = None):
    """Create the SQLAlchemy engine and create all tables."""
    dsn = dsn or APP.dsn
    # SQLite-specific pragmas for WAL mode + foreign keys
    connect_args = {}
    if dsn.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    engine = create_engine(dsn, connect_args=connect_args, echo=False, future=True)
    Base.metadata.create_all(engine)
    # Enable WAL for SQLite (better concurrency for web + bot)
    if dsn.startswith("sqlite"):
        with engine.connect() as conn:
            from sqlalchemy import text
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.commit()
    return engine


def build_session_factory(engine) -> sessionmaker:
    """Return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ──────────────────────────────────────────────
#  Convenience: get a DB session as context manager
# ──────────────────────────────────────────────

import contextlib

@contextlib.contextmanager
def db_session(factory: sessionmaker) -> Session:
    """Yield a session and auto-commit/rollback on exit."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()