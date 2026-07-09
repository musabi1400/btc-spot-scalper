"""
strategy.py — backwards-compatibility shim.
Redirects to strategy.engine for existing imports.
"""
from strategy.engine import *  # noqa: F401, F403