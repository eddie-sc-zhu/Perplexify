"""Data access facade used by the API routes and the bot.

Keeps one provider instance per venue and the parquet cache behind a tiny
interface, so tests can swap in a fake without touching the routes.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import pandas as pd

from perplexify.data.dataset import MarketData, build_market_data
from perplexify.data.providers import get_provider, list_providers
from perplexify.data.providers.hyperliquid import HyperliquidProvider
from perplexify.data.storage import DataStore
from perplexify.data.synthetic import make_synthetic_market_data
from perplexify.utils import interval_to_hours, to_utc, utc_now

log = logging.getLogger(__name__)


class DataService:
    def __init__(self, store: DataStore | None = None) -> None:
        self.store = store or DataStore()

    # -- panels ------------------------------------------------------------
    def get_market_data(self, exchange: str, symbols: list[str], interval: str, start=None, end=None) -> MarketData:
        if exchange == "synthetic":
            hours = interval_to_hours(interval)
            end_ts = to_utc(end) or utc_now()
            start_ts = to_utc(start) or end_ts - pd.Timedelta(days=730)
            n_bars = max(int((end_ts - start_ts) / pd.Timedelta(hours=hours)) + 1, 50)
            md = make_synthetic_market_data(tuple(s.upper() for s in symbols), n_bars=n_bars, bar_hours=hours, end=end_ts)
            return md.slice(start_ts, end_ts)
        return build_market_data(exchange, symbols, interval, start, end, store=self.store)

    # -- live --------------------------------------------------------------
    def exchanges(self) -> list[str]:
        return list_providers() + ["synthetic"]

    def markets(self, exchange: str) -> pd.DataFrame:
        return get_provider(exchange).list_markets()

    def funding_history(self, exchange: str, symbol: str, days: float) -> tuple[pd.DataFrame, float]:
        provider = get_provider(exchange)
        end = utc_now()
        df = self.store.get_funding(provider, provider.normalize_symbol(symbol), end - pd.Timedelta(days=days), end)
        return df, provider.funding_interval_hours

    def predicted_fundings(self) -> pd.DataFrame:
        provider = get_provider("hyperliquid")
        assert isinstance(provider, HyperliquidProvider)
        return provider.predicted_fundings()


@lru_cache(maxsize=1)
def default_service() -> DataService:
    return DataService()
