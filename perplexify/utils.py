"""Small shared helpers: interval parsing, time conversion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

HOURS_PER_YEAR = 365 * 24

_INTERVAL_HOURS = {
    "1h": 1.0,
    "2h": 2.0,
    "4h": 4.0,
    "8h": 8.0,
    "12h": 12.0,
    "1d": 24.0,
}


def interval_to_hours(interval: str) -> float:
    try:
        return _INTERVAL_HOURS[interval.lower()]
    except KeyError as exc:
        raise ValueError(f"Unsupported interval {interval!r}; choose one of {sorted(_INTERVAL_HOURS)}") from exc


def interval_to_timedelta(interval: str) -> pd.Timedelta:
    return pd.Timedelta(hours=interval_to_hours(interval))


def bars_per_year(bar_hours: float) -> float:
    return HOURS_PER_YEAR / bar_hours


def to_utc(ts: datetime | str | pd.Timestamp | None) -> pd.Timestamp | None:
    """Coerce anything date-like to a tz-aware UTC pandas Timestamp."""
    if ts is None:
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("UTC")


def utc_now() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def ms(ts: pd.Timestamp) -> int:
    return int(ts.value // 1_000_000)


def from_ms(value: int | float | str) -> pd.Timestamp:
    return pd.Timestamp(int(value), unit="ms", tz="UTC")


def floor_to(ts: pd.Timestamp, delta: pd.Timedelta) -> pd.Timestamp:
    return ts.floor(delta)


def days_ago(days: float) -> pd.Timestamp:
    return utc_now() - timedelta(days=days)
