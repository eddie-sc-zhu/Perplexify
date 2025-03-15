"""dYdX v4 indexer (https://indexer.dydx.trade/v4).

Hourly funding with the oracle price attached to every settlement, and
unlimited candle history, so it is the best source for long hourly backtests.
"""

from __future__ import annotations

import pandas as pd

from perplexify.data.http import HttpClient
from perplexify.data.providers.base import (
    MARKET_COLUMNS,
    FundingProvider,
    UnsupportedInterval,
    annualize_rate,
    empty_candles,
    empty_funding,
    finalize_candles,
    finalize_funding,
)

_RESOLUTIONS = {"1h": "1HOUR", "4h": "4HOURS", "1d": "1DAY"}
_PAGE = 1000


def _iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


class DydxProvider(FundingProvider):
    name = "dydx"
    funding_interval_hours = 1.0
    candle_intervals = tuple(_RESOLUTIONS)
    candle_history_days = {k: None for k in _RESOLUTIONS}

    def __init__(self, base_url: str = "https://indexer.dydx.trade/v4") -> None:
        self._http = HttpClient(base_url)

    def normalize_symbol(self, symbol: str) -> str:
        s = symbol.upper().strip()
        for suffix in ("-USDT-SWAP", "USDT", "-USD", "USD", "-PERP"):
            if s.endswith(suffix) and len(s) > len(suffix):
                s = s[: -len(suffix)]
                break
        return f"{s}-USD"

    def list_markets(self) -> pd.DataFrame:
        raw = self._http.get("/perpetualMarkets", params={"limit": 500})
        rows = []
        for ticker, m in raw["markets"].items():
            if m.get("status") != "ACTIVE":
                continue
            rate = float(m.get("nextFundingRate") or 0.0)
            price = float(m.get("oraclePrice") or "nan")
            rows.append(
                {
                    "symbol": ticker.replace("-USD", ""),
                    "mark_price": price,
                    "index_price": price,
                    "funding_rate": rate,
                    "funding_interval_hours": self.funding_interval_hours,
                    "funding_apr": annualize_rate(rate, self.funding_interval_hours),
                    "next_funding_time": pd.NaT,
                    "open_interest": float(m.get("openInterest") or 0.0) * (price if price == price else 0.0),
                    "volume_24h": float(m.get("volume24H") or 0.0),
                }
            )
        df = pd.DataFrame(rows, columns=MARKET_COLUMNS)
        return df.sort_values("volume_24h", ascending=False).reset_index(drop=True)

    def fetch_funding_history(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        ticker = self.normalize_symbol(symbol)
        cursor = end
        frames = []
        while cursor > start:
            raw = self._http.get(
                f"/historicalFunding/{ticker}", params={"limit": _PAGE, "effectiveBeforeOrAt": _iso(cursor)}
            )
            rows = raw.get("historicalFunding", [])
            if not rows:
                break
            df = pd.DataFrame(rows)
            frames.append(df)
            oldest = pd.to_datetime(df["effectiveAt"], utc=True).min()
            if len(rows) < _PAGE or oldest >= cursor:
                break
            cursor = oldest - pd.Timedelta(milliseconds=1)
        if not frames:
            return empty_funding()
        df = pd.concat(frames, ignore_index=True)
        out = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(df["effectiveAt"], utc=True).dt.floor("h"),
                "funding_rate": df["rate"].astype(float),
                "index_price": df["price"].astype(float),
            }
        )
        return finalize_funding(out, start, end)

    def fetch_candles(self, symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        if interval not in _RESOLUTIONS:
            raise UnsupportedInterval(interval)
        ticker = self.normalize_symbol(symbol)
        cursor = end
        frames = []
        while cursor > start:
            raw = self._http.get(
                f"/candles/perpetualMarkets/{ticker}",
                params={
                    "resolution": _RESOLUTIONS[interval],
                    "limit": _PAGE,
                    "fromISO": _iso(start),
                    "toISO": _iso(cursor),
                },
            )
            rows = raw.get("candles", [])
            if not rows:
                break
            df = pd.DataFrame(rows)
            frames.append(df)
            oldest = pd.to_datetime(df["startedAt"], utc=True).min()
            if len(rows) < _PAGE or oldest >= cursor:
                break
            cursor = oldest
        if not frames:
            return empty_candles()
        df = pd.concat(frames, ignore_index=True)
        out = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(df["startedAt"], utc=True),
                "open": df["open"].astype(float),
                "high": df["high"].astype(float),
                "low": df["low"].astype(float),
                "close": df["close"].astype(float),
                "volume": df["baseTokenVolume"].astype(float),
            }
        )
        return finalize_candles(out, start, end)

    def close(self) -> None:
        self._http.close()
