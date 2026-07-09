"""
backtesting/__init__.py
=======================
Backtesting engine for the BTC Spot Scalper.

Public API:
    BacktestEngine   — run historical simulations
    MetricsCalculator — compute performance metrics
    DataLoader        — load / fetch OHLCV data
    BacktestReport    — generate text & HTML reports
"""
from __future__ import annotations

from backtesting.engine import BacktestEngine
from backtesting.metrics import MetricsCalculator
from backtesting.data_loader import DataLoader
from backtesting.report import BacktestReport

__all__ = [
    "BacktestEngine",
    "MetricsCalculator",
    "DataLoader",
    "BacktestReport",
]