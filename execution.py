"""
execution.py — backwards-compatibility shim.
Redirects to execution.engine for existing imports.
"""
from execution.engine import *  # noqa: F401, F403