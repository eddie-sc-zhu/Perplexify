"""Local parquet cache with incremental refresh.

Layout: <root>/<exchange>/<kind>_<symbol>[_<interval>].parquet plus a small
JSON sidecar remembering the earliest timestamp ever requested, so venues that
cap history (Hyperliquid candles) are not re-queried for ranges they cannot
serve.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from pathlib import Path

import pandas as pd

from perplexify.config import settings
from perplexify.data.providers.base import FundingProvider
from perplexify.utils import interval_to_timedelta, to_utc

log = logging.getLogger(__name__)


class DataStore:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else settings.data_dir
        self.root.mkdir(parents=True, exist_ok=True)
        # one lock per cache file: API threads, the bot and scripts sharing a
        # store must not interleave load / fetch / save on the same series
        self._locks: dict[Path, threading.RLock] = defaultdict(threading.RLock)
        self._locks_guard = threading.Lock()

    # -- paths -------------------------------------------------------------
    def path(self, kind: str, exchange: str, symbol: str, interval: str | None = None) -> Path:
        safe = symbol.replace("/", "_").replace(":", "_")
        name = f"{kind}_{safe}" + (f"_{interval}" if interval else "") + ".parquet"
        return self.root / exchange / name

    def load(self, kind: str, exchange: str, symbol: str, interval: str | None = None) -> pd.DataFrame | None:
        p = self.path(kind, exchange, symbol, interval)
        if not p.exists():
            return None
        df = pd.read_parquet(p)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df

    def save(self, df: pd.DataFrame, kind: str, exchange: str, symbol: str, interval: str | None = None) -> None:
        p = self.path(kind, exchange, symbol, interval)
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p, index=False)

    def _meta_path(self, p: Path) -> Path:
        return p.with_suffix(".meta.json")

    def _earliest_requested(self, p: Path) -> pd.Timestamp | None:
        mp = self._meta_path(p)
        if not mp.exists():
            return None
        try:
            return to_utc(json.loads(mp.read_text())["earliest_requested"])
        except Exception:  # pragma: no cover - corrupt sidecar
            return None

    def _remember_request(self, p: Path, start: pd.Timestamp) -> None:
        prev = self._earliest_requested(p)
        if prev is None or start < prev:
            p.parent.mkdir(parents=True, exist_ok=True)
            self._meta_path(p).write_text(json.dumps({"earliest_requested": start.isoformat()}))

    # -- incremental fetch -------------------------------------------------
    def _get(
        self,
        kind: str,
        provider: FundingProvider,
        symbol: str,
        interval: str | None,
        start: pd.Timestamp,
        end: pd.Timestamp,
        fetch,
        step: pd.Timedelta,
        refresh: bool,
    ) -> pd.DataFrame:
        p = self.path(kind, provider.name, symbol, interval)
        with self._locks_guard:
            lock = self._locks[p]
        with lock:
            return self._get_locked(kind, provider, symbol, interval, start, end, fetch, step, refresh, p)

    def _get_locked(self, kind, provider, symbol, interval, start, end, fetch, step, refresh, p: Path) -> pd.DataFrame:
        cached = None if refresh else self.load(kind, provider.name, symbol, interval)
        frames: list[pd.DataFrame] = []
        if cached is None or cached.empty:
            log.info("%s %s %s: fetching %s -> %s", provider.name, kind, symbol, start.date(), end.date())
            frames.append(fetch(symbol, start, end))
            self._remember_request(p, start)
        else:
            frames.append(cached)
            cmin, cmax = cached["timestamp"].min(), cached["timestamp"].max()
            earliest = self._earliest_requested(p)
            if start < cmin - step and (earliest is None or start < earliest):
                log.info("%s %s %s: backfilling %s -> %s", provider.name, kind, symbol, start.date(), cmin.date())
                frames.append(fetch(symbol, start, cmin))
                self._remember_request(p, start)
            if end > cmax + step:
                log.info("%s %s %s: updating %s -> %s", provider.name, kind, symbol, cmax.date(), end.date())
                frames.append(fetch(symbol, cmax - step, end))
        df = pd.concat([f for f in frames if f is not None and not f.empty], ignore_index=True) if any(
            f is not None and not f.empty for f in frames
        ) else frames[0]
        if not df.empty:
            df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
        self.save(df, kind, provider.name, symbol, interval)
        return df[(df["timestamp"] >= start) & (df["timestamp"] <= end)].reset_index(drop=True)

    def get_funding(self, provider: FundingProvider, symbol: str, start, end, refresh: bool = False) -> pd.DataFrame:
        start, end = to_utc(start), to_utc(end)
        step = pd.Timedelta(hours=provider.funding_interval_hours)
        return self._get("funding", provider, symbol, None, start, end, provider.fetch_funding_history, step, refresh)

    def get_candles(self, provider: FundingProvider, symbol: str, interval: str, start, end, refresh: bool = False) -> pd.DataFrame:
        start, end = to_utc(start), to_utc(end)
        fetch = lambda s, a, b: provider.fetch_candles(s, interval, a, b)  # noqa: E731
        return self._get("candles", provider, symbol, interval, start, end, fetch, interval_to_timedelta(interval), refresh)

    def get_index_candles(self, provider: FundingProvider, symbol: str, interval: str, start, end, refresh: bool = False) -> pd.DataFrame | None:
        start, end = to_utc(start), to_utc(end)
        probe = provider.fetch_index_candles.__func__ is not FundingProvider.fetch_index_candles
        if not probe:
            return None
        fetch = lambda s, a, b: provider.fetch_index_candles(s, interval, a, b)  # noqa: E731
        return self._get("index", provider, symbol, interval, start, end, fetch, interval_to_timedelta(interval), refresh)
