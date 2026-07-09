"""
core/exceptions.py
==================
Custom exception hierarchy for the BTC Scalper application.
Provides granular error types for better handling and logging.
"""
from __future__ import annotations


class ScalperError(Exception):
    """Base exception for all bot errors."""
    pass


# ── Config / Settings ──

class ConfigError(ScalperError):
    """Configuration is missing or invalid."""
    pass


# ── Exchange / API ──

class ExchangeError(ScalperError):
    """Generic exchange communication error."""
    pass


class ExchangeAuthError(ExchangeError):
    """API credentials are invalid or unauthorized."""
    pass


class ExchangeConnectionError(ExchangeError):
    """Cannot connect to the exchange (network/timeout)."""
    pass


# ── Order Execution ──

class OrderError(ScalperError):
    """Generic order placement / management error."""
    pass


class OrderRejectedError(OrderError):
    """Exchange rejected the order."""
    pass


class OrderTimeoutError(OrderError):
    """Order was not filled within the timeout period."""
    pass


class InsufficientFundsError(OrderError):
    """Not enough balance to place the order."""
    pass


# ── Strategy ──

class StrategyError(ScalperError):
    """Strategy computation or evaluation error."""
    pass


class InsufficientDataError(StrategyError):
    """Not enough OHLCV data to compute indicators."""
    pass


# ── Risk Management ──

class RiskError(ScalperError):
    """Risk rule violation."""
    pass


class CircuitBreakerError(RiskError):
    """Circuit breaker is active — trading is halted."""
    pass


class MaxExposureError(RiskError):
    """Maximum exposure limit exceeded."""
    pass


# ── Database ──

class DatabaseError(ScalperError):
    """Database operation failed."""
    pass


# ── Security ──

class EncryptionError(ScalperError):
    """Credential encryption / decryption failed."""
    pass