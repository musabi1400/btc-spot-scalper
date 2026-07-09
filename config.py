"""
config.py
=========
Central configuration for the BTC Spot Scalper application.
All tunable parameters (strategy thresholds, risk limits, exchange settings)
live here so they can be hot-reloaded from the database at runtime.

Environment variables (read at import time):
    BINANCE_API_KEY        — production API key
    BINANCE_API_SECRET     — production API secret
    BINANCE_TESTNET_KEY    — testnet API key
    BINANCE_TESTNET_SECRET — testnet API secret
    ENCRYPTION_KEY         — 32-byte hex key for encrypting stored credentials
    APP_ENV                — "demo" | "live"  (default: demo)
    DSN                    — database path or URL (default: sqlite:///btc_scalper.db)
    USE_BNB_FEE            — "true"/"false"  (default: true)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TradingMode(str, Enum):
    """Switch between testnet (paper) and production (real funds)."""
    DEMO = "demo"
    LIVE = "live"


# ──────────────────────────────────────────────
#  Exchange & API
# ──────────────────────────────────────────────

EXCHANGE_ID = "binance"
SYMBOL = "BTC/USDT"
TIMEFRAME_EXECUTION = "5m"   # primary timeframe for entries
TIMEFRAME_CONTEXT = "15m"    # higher TF for trend context

# Binance fee tiers (Spot).  When BNB discount is active, fees are 25% off.
TAKER_FEE_RATE = 0.001       # 0.1 %
MAKER_FEE_RATE = 0.001       # 0.1 %
BNB_DISCOUNT = 0.25          # 25 % off when paying fees in BNB
USE_BNB_FEE = os.getenv("USE_BNB_FEE", "true").lower() == "true"

# Effective maker fee after BNB discount
EFFECTIVE_MAKER_FEE = MAKER_FEE_RATE * (1 - BNB_DISCOUNT if USE_BNB_FEE else 1)
# Round-trip fee (buy + sell)
ROUND_TRIP_FEE = EFFECTIVE_MAKER_FEE * 2


# ──────────────────────────────────────────────
#  Strategy Parameters
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class StrategyConfig:
    # EMAs
    ema_fast: int = 9
    ema_mid: int = 21
    ema_slow: int = 50

    # VWAP — session-based (reset at 00:00 UTC)
    vwap_reset_hour_utc: int = 0

    # Volume spike detection
    volume_sma_period: int = 20
    volume_spike_multiplier: float = 1.5

    # RSI
    rsi_period: int = 14
    rsi_lower: float = 40.0
    rsi_upper: float = 60.0

    # Order-book depth analysis
    order_book_depth_pct: float = 0.01   # 1 % range around mid-price
    bid_wall_min_pct: float = 0.001      # 0.1 % below price
    bid_wall_max_pct: float = 0.003      # 0.3 % below price

    # Confluence — need at least N of M conditions
    min_confluence_score: int = 3
    total_conditions: int = 5

    # OHLCV buffer sizes
    ohlcv_buffer_5m: int = 200
    ohlcv_buffer_15m: int = 100


# ──────────────────────────────────────────────
#  Risk Management
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class RiskConfig:
    # Position sizing
    max_risk_per_trade_pct: float = 0.01   # 1 % of total balance
    max_position_pct: float = 0.30         # never use more than 30 % of available USDT in one trade

    # Stop-loss
    sl_min_pct: float = 0.003              # 0.3 % below entry
    sl_max_pct: float = 0.005              # 0.5 % below entry
    sl_default_pct: float = 0.004          # 0.4 % (midpoint)

    # Take-profit
    min_rr_ratio: float = 1.5              # TP >= 1.5 × SL distance
    min_gross_profit_pct: float = 0.005    # 0.5 % minimum gross profit

    # Trailing stop
    trailing_trigger_r: float = 1.0        # move to break-even at 1R profit
    trailing_breakeven_buffer_pct: float = ROUND_TRIP_FEE  # add fees to BE price

    # Circuit breaker
    max_daily_losses: int = 3
    cooldown_hours: int = 24

    # Concurrency
    max_concurrent_trades: int = 1

    # Order timeout
    order_fill_timeout_sec: int = 60


# ──────────────────────────────────────────────
#  Application / Runtime
# ──────────────────────────────────────────────

@dataclass
class AppConfig:
    # Mode
    mode: TradingMode = TradingMode.DEMO

    # Database
    dsn: str = "sqlite:///btc_scalper.db"

    # API credentials (held in memory only — persisted encrypted in DB)
    api_key: Optional[str] = None
    api_secret: Optional[str] = None

    # Encryption key for storing credentials in DB
    encryption_key: Optional[str] = field(
        default_factory=lambda: os.getenv("ENCRYPTION_KEY")
    )

    # WebSocket poll intervals (seconds)
    ws_heartbeat_sec: int = 15
    strategy_tick_sec: int = 5          # how often the strategy loop evaluates

    # Whether the bot should be actively trading (master switch)
    auto_trade_enabled: bool = False

    # Web server
    host: str = "0.0.0.0"
    port: int = 8000
    ws_port: int = 8000                 # same FastAPI instance serves WS


# ──────────────────────────────────────────────
#  Singleton helpers
# ──────────────────────────────────────────────

STRATEGY = StrategyConfig()
RISK = RiskConfig()
APP = AppConfig(
    mode=TradingMode(os.getenv("APP_ENV", "demo")),
    dsn=os.getenv("DSN", "sqlite:///btc_scalper.db"),
)

# Pull initial API keys from env if present
if APP.mode == TradingMode.LIVE:
    APP.api_key = os.getenv("BINANCE_API_KEY")
    APP.api_secret = os.getenv("BINANCE_API_SECRET")
else:
    APP.api_key = os.getenv("BINANCE_TESTNET_KEY")
    APP.api_secret = os.getenv("BINANCE_TESTNET_SECRET")