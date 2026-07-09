"""
analytics/__init__.py
====================
Analytics layer for the BTC Spot Scalper.

Exports:
    - TradeAnalytics        : ORM model for per-trade analytics
    - init_analytics_tables : create the analytics table on a given engine
    - TradeRecorder         : record entry/exit/error analytics
    - PerformanceAnalyzer   : daily/monthly performance + distributions
    - SignalScorer          : dynamic weighted signal scoring
"""
from __future__ import annotations

from analytics.models import TradeAnalytics, init_analytics_tables
from analytics.recorder import TradeRecorder
from analytics.performance import PerformanceAnalyzer
from analytics.scoring import SignalScorer

__all__ = [
    "TradeAnalytics",
    "init_analytics_tables",
    "TradeRecorder",
    "PerformanceAnalyzer",
    "SignalScorer",
]