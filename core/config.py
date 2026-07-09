"""
core/config.py
==============
Unified configuration module — all tunable parameters in one place.
Replaces the flat config.py with a layered settings structure.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TradingMode(str, Enum):
    DEMO = "demo"
    LIVE = "live"


# ──────────────────────────────────────────────
#  Exchange constants
# ──────────────────────────────────────────────

EXCHANGE_ID = "binance"
SYMBOL = "BTC/USDT"
TIMEFRAME_EXECUTION = "5m"
TIMEFRAME_CONTEXT = "15m"

TAKER_FEE_RATE = 0.001
MAKER_FEE_RATE = 0.001
BNB_DISCOUNT = 0.25
USE_BNB_FEE = os.getenv("USE_BNB_FEE", "true").lower() == "true"

EFFECTIVE_MAKER_FEE = MAKER_FEE_RATE * (1 - BNB_DISCOUNT if USE_BNB_FEE else 1)
ROUND_TRIP_FEE = EFFECTIVE_MAKER_FEE * 2


# ──────────────────────────────────────────────
#  Strategy Parameters
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class StrategyConfig:
    ema_fast: int = 9
    ema_mid: int = 21
    ema_slow: int = 50
    vwap_reset_hour_utc: int = 0
    volume_sma_period: int = 20
    volume_spike_multiplier: float = 1.5
    rsi_period: int = 14
    rsi_lower: float = 40.0
    rsi_upper: float = 60.0
    order_book_depth_pct: float = 0.01
    bid_wall_min_pct: float = 0.001
    bid_wall_max_pct: float = 0.003
    min_confluence_score: int = 3
    total_conditions: int = 5
    ohlcv_buffer_5m: int = 200
    ohlcv_buffer_15m: int = 100


# ──────────────────────────────────────────────
#  Risk Management
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class RiskConfig:
    max_risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.30
    sl_min_pct: float = 0.003
    sl_max_pct: float = 0.005
    sl_default_pct: float = 0.004
    min_rr_ratio: float = 1.5
    min_gross_profit_pct: float = 0.005
    trailing_trigger_r: float = 1.0
    trailing_breakeven_buffer_pct: float = ROUND_TRIP_FEE
    max_daily_losses: int = 3
    cooldown_hours: int = 24
    max_concurrent_trades: int = 1
    order_fill_timeout_sec: int = 60


# ──────────────────────────────────────────────
#  Application
# ──────────────────────────────────────────────

@dataclass
class AppConfig:
    mode: TradingMode = TradingMode.DEMO
    dsn: str = "sqlite:///btc_scalper.db"
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    encryption_key: Optional[str] = field(
        default_factory=lambda: os.getenv("ENCRYPTION_KEY")
    )
    ws_heartbeat_sec: int = 15
    strategy_tick_sec: int = 5
    auto_trade_enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    ws_port: int = 8000
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    log_dir: str = field(default_factory=lambda: os.getenv("LOG_DIR", "logs"))
    log_max_bytes: int = field(default_factory=lambda: int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024))))
    log_backup_count: int = field(default_factory=lambda: int(os.getenv("LOG_BACKUP_COUNT", "5")))


# ──────────────────────────────────────────────
#  Singletons
# ──────────────────────────────────────────────

STRATEGY = StrategyConfig()
RISK = RiskConfig()
APP = AppConfig(
    mode=TradingMode(os.getenv("APP_ENV", "demo")),
    dsn=os.getenv("DSN", "sqlite:///btc_scalper.db"),
)

if APP.mode == TradingMode.LIVE:
    APP.api_key = os.getenv("BINANCE_API_KEY")
    APP.api_secret = os.getenv("BINANCE_API_SECRET")
else:
    APP.api_key = os.getenv("BINANCE_TESTNET_KEY")
    APP.api_secret = os.getenv("BINANCE_TESTNET_SECRET")