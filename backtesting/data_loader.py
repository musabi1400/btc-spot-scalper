"""
backtesting/data_loader.py
==========================
Load, fetch, and cache OHLCV data for backtesting.

The loader is intentionally **synchronous** — it uses the REST flavour of
``ccxt`` (never ``ccxt.pro``) or plain CSV files, so it can be used inside a
plain backtest loop without an asyncio event loop.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from core.exceptions import ExchangeConnectionError, InsufficientDataError


# Canonical column order used throughout the backtesting package.
_OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class DataLoader:
    """Load OHLCV data from CSV files or sync ccxt REST endpoints."""

    # ── CSV ─────────────────────────────────────────────────────

    @staticmethod
    def load_from_csv(filepath: str) -> pd.DataFrame:
        """Load OHLCV data from a CSV file.

        The file may have a ``timestamp`` column expressed as either:
          * Unix milliseconds (int)
          * ISO-8601 string
          * Unix seconds (float/int)

        The returned DataFrame is indexed by a tz-aware ``timestamp`` column
        and contains ``open, high, low, close, volume`` columns sorted ascending.
        """
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"CSV file not found: {filepath}")

        df = pd.read_csv(filepath)

        # Normalise timestamp column
        if "timestamp" not in df.columns:
            raise ValueError(f"CSV must contain a 'timestamp' column: {filepath}")

        ts = df["timestamp"]
        if pd.api.types.is_numeric_dtype(ts):
            # Heuristic: milliseconds vs seconds.  Anything > 1e12 is ms.
            if ts.iloc[0] > 1e12:
                ts = pd.to_datetime(ts, unit="ms", utc=True)
            else:
                ts = pd.to_datetime(ts, unit="s", utc=True)
        else:
            ts = pd.to_datetime(ts, utc=True)

        df["timestamp"] = ts
        df = df.set_index("timestamp").sort_index()

        # Ensure expected columns exist
        for col in _OHLCV_COLUMNS[1:]:
            if col not in df.columns:
                raise ValueError(f"CSV missing required column '{col}': {filepath}")

        df = df[_OHLCV_COLUMNS[1:]].copy()
        df = df.astype(float)
        # Drop rows with NaN in critical columns
        df = df.dropna(subset=["open", "high", "low", "close", "volume"])
        return df

    # ── Exchange (sync REST) ─────────────────────────────────────

    @staticmethod
    def fetch_from_exchange(
        exchange_id: str,
        symbol: str,
        timeframe: str,
        start_date: str | datetime,
        end_date: str | datetime,
    ) -> pd.DataFrame:
        """Fetch historical OHLCV from a sync ccxt exchange.

        Parameters
        ----------
        exchange_id
            ccxt exchange id, e.g. ``"binance"``.
        symbol
            Market symbol, e.g. ``"BTC/USDT"``.
        timeframe
            ccxt timeframe string, e.g. ``"5m"``.
        start_date, end_date
            Inclusive date range (string or datetime).  Accepts ISO-8601.

        Returns
        -------
        pd.DataFrame
            Indexed by tz-aware ``timestamp``, columns ``open, high, low,
            close, volume``.
        """
        import ccxt  # imported lazily so the module can still be imported
                     # in environments without ccxt (e.g. CI with stubs)

        try:
            exchange_cls = getattr(ccxt, exchange_id)
        except AttributeError:
            raise ExchangeConnectionError(f"Unknown exchange: {exchange_id}")

        exchange = exchange_cls({"enableRateLimit": True})

        # Convert dates to Unix ms
        start_ts = DataLoader._to_ms(start_date)
        end_ts = DataLoader._to_ms(end_date)

        all_ohlcv: list[list] = []
        # ccxt binance limit per request is 1000; use 500 for safety across
        # exchanges.
        limit = 500

        since = start_ts
        while since < end_ts:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            except Exception as exc:  # noqa: BLE001
                raise ExchangeConnectionError(
                    f"Failed to fetch OHLCV from {exchange_id}: {exc}"
                ) from exc

            if not ohlcv:
                break

            all_ohlcv.extend(ohlcv)

            last_ts = ohlcv[-1][0]
            if last_ts <= since:
                # No forward progress — bail out to avoid infinite loop.
                break
            since = last_ts + 1

            # Respect rate limits
            try:
                exchange.sleep(exchange.rateLimit / 1000)
            except Exception:  # noqa: BLE001
                pass

        if not all_ohlcv:
            raise InsufficientDataError(
                f"No OHLCV data returned for {symbol} {timeframe} "
                f"in [{start_date}, {end_date}]"
            )

        df = pd.DataFrame(all_ohlcv, columns=_OHLCV_COLUMNS)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp").sort_index()

        # Filter to requested range
        start_dt = pd.Timestamp(start_ts, unit="ms", tz="UTC")
        end_dt = pd.Timestamp(end_ts, unit="ms", tz="UTC")
        df = df[(df.index >= start_dt) & (df.index <= end_dt)]

        df = df[_OHLCV_COLUMNS[1:]].astype(float).dropna(
            subset=["open", "high", "low", "close", "volume"]
        )
        return df

    # ── Cache ────────────────────────────────────────────────────

    @staticmethod
    def save_to_cache(df: pd.DataFrame, filepath: str) -> None:
        """Save *df* to *filepath* as a CSV (timestamp column restored)."""
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        out = df.copy()
        out = out.reset_index()
        # Ensure the index column is named 'timestamp'
        if "timestamp" not in out.columns:
            out.rename(columns={out.columns[0]: "timestamp"}, inplace=True)
        out.to_csv(filepath, index=False)

    @staticmethod
    def load_from_cache(filepath: str) -> pd.DataFrame:
        """Load a DataFrame previously saved by :meth:`save_to_cache`."""
        return DataLoader.load_from_csv(filepath)

    # ── helpers ─────────────────────────────────────────────────

    @staticmethod
    def _to_ms(date: str | datetime) -> int:
        """Convert a string / datetime to Unix milliseconds."""
        if isinstance(date, datetime):
            dt = date
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        dt = pd.to_datetime(date, utc=True)
        return int(dt.timestamp() * 1000)