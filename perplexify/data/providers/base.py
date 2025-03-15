"""Common provider interface.

Every provider returns the same canonical frames so the dataset builder,
backtester, API and bot never need venue-specific code.

Funding history  : timestamp (UTC settlement time), funding_rate (decimal per
                   settlement, e.g. 0.0001 = 1 bp), premium (optional),
                   index_price (optional)
Candles          : timestamp (UTC bar OPEN), open, high, low, close, volume
Markets snapshot : symbol, mark_price, index_price, funding_rate (current, per
                   interval), funding_interval_hours, funding_apr,
                   next_funding_time, open_interest, volume_24h
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from perplexify.utils import HOURS_PER_YEAR

FUNDING_COLUMNS = ["timestamp", "funding_rate", "premium", "index_price"]
CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
MARKET_COLUMNS = [
    "symbol",
    "mark_price",
    "index_price",
    "funding_rate",
    "funding_interval_hours",
    "funding_apr",
    "next_funding_time",
    "open_interest",
    "volume_24h",
]


class UnsupportedInterval(ValueError):
    pass


def _empty(columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="float64") for c in columns})
    df["timestamp"] = pd.Series(dtype="datetime64[ns, UTC]")
    return df


def empty_funding() -> pd.DataFrame:
    return _empty(FUNDING_COLUMNS)


def empty_candles() -> pd.DataFrame:
    return _empty(CANDLE_COLUMNS)


def finalize_funding(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if df.empty:
        return empty_funding()
    for col in ("premium", "index_price"):
        if col not in df:
            df[col] = float("nan")
    df = df[FUNDING_COLUMNS].copy()
    df["funding_rate"] = df["funding_rate"].astype(float)
    df = df.drop_duplicates("timestamp").sort_values("timestamp")
    df = df[(df["timestamp"] >= start) & (df["timestamp"] <= end)]
    return df.reset_index(drop=True)


def finalize_candles(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if df.empty:
        return empty_candles()
    df = df[CANDLE_COLUMNS].copy()
    for col in CANDLE_COLUMNS[1:]:
        df[col] = df[col].astype(float)
    df = df.drop_duplicates("timestamp").sort_values("timestamp")
    df = df[(df["timestamp"] >= start) & (df["timestamp"] < end)]
    return df.reset_index(drop=True)


def annualize_rate(rate: float, interval_hours: float) -> float:
    return float(rate) * HOURS_PER_YEAR / float(interval_hours)


class FundingProvider(ABC):
    """Read-only market-data access for one perpetual-futures venue."""

    name: str = "base"
    #: native funding settlement interval in hours
    funding_interval_hours: float = 8.0
    #: candle intervals the venue serves natively (canonical strings)
    candle_intervals: tuple[str, ...] = ("1h",)
    #: how far back candles are available, in days (None = unlimited)
    candle_history_days: dict[str, float | None] = {}

    @abstractmethod
    def normalize_symbol(self, symbol: str) -> str:
        """Map a user symbol ("BTC", "BTC-USD", "BTCUSDT") to the venue's identifier."""

    @abstractmethod
    def list_markets(self) -> pd.DataFrame:
        """Snapshot of all perpetual markets with their current funding."""

    @abstractmethod
    def fetch_funding_history(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        ...

    @abstractmethod
    def fetch_candles(self, symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        ...

    def fetch_index_candles(self, symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame | None:
        """Spot/index price candles when the venue exposes them; None otherwise."""
        return None

    def close(self) -> None:
        pass
