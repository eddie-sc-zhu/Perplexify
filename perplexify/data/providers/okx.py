"""OKX public REST API (https://www.okx.com/api/v5).

Funding every 8h (some contracts 4h). Funding history is only served for the
last ~3 months, candles and index candles are unlimited. Good for the live
scanner and short-horizon backtests with a real spot index leg.
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
from perplexify.utils import from_ms, ms

_BARS = {"1h": "1H", "2h": "2H", "4h": "4H", "12h": "12H", "1d": "1Dutc"}
_PAGE = 100


class OkxProvider(FundingProvider):
    name = "okx"
    funding_interval_hours = 8.0
    candle_intervals = tuple(_BARS)
    candle_history_days = {k: None for k in _BARS}

    def __init__(self, base_url: str = "https://www.okx.com/api/v5") -> None:
        # OKX public endpoints allow 20 requests / 2 s
        self._http = HttpClient(base_url, min_interval=0.12)

    def normalize_symbol(self, symbol: str) -> str:
        s = symbol.upper().strip()
        if s.endswith("-SWAP"):
            return s
        for suffix in ("-USD-PERP", "-USD", "USDT", "USD", "-PERP"):
            if s.endswith(suffix) and len(s) > len(suffix):
                s = s[: -len(suffix)]
                break
        return f"{s}-USDT-SWAP"

    def _index_id(self, symbol: str) -> str:
        return self.normalize_symbol(symbol).replace("-SWAP", "")

    def _get(self, path: str, params: dict) -> list:
        raw = self._http.get(path, params=params)
        if str(raw.get("code")) != "0":
            raise RuntimeError(f"OKX error {raw.get('code')}: {raw.get('msg')}")
        return raw.get("data", [])

    def list_markets(self) -> pd.DataFrame:
        tickers = {t["instId"]: t for t in self._get("/market/tickers", {"instType": "SWAP"})}
        funding = self._get("/public/funding-rate", {"instId": "ANY"})
        rows = []
        for f in funding:
            inst = f["instId"]
            if not inst.endswith("-USDT-SWAP"):
                continue
            t = tickers.get(inst, {})
            hours = 8.0
            if f.get("fundingTime") and f.get("nextFundingTime"):
                hours = max((int(f["nextFundingTime"]) - int(f["fundingTime"])) / 3_600_000, 1.0)
            rate = float(f.get("fundingRate") or 0.0)
            price = float(t.get("last") or "nan")
            rows.append(
                {
                    "symbol": inst.replace("-USDT-SWAP", ""),
                    "mark_price": price,
                    "index_price": float("nan"),
                    "funding_rate": rate,
                    "funding_interval_hours": hours,
                    "funding_apr": annualize_rate(rate, hours),
                    "next_funding_time": from_ms(f["nextFundingTime"]) if f.get("nextFundingTime") else pd.NaT,
                    "open_interest": float("nan"),
                    "volume_24h": float(t.get("volCcy24h") or 0.0) * (price if price == price else 0.0),
                }
            )
        df = pd.DataFrame(rows, columns=MARKET_COLUMNS)
        return df.sort_values("volume_24h", ascending=False).reset_index(drop=True)

    def fetch_funding_history(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        inst = self.normalize_symbol(symbol)
        cursor = ms(end)
        frames = []
        while cursor > ms(start):
            rows = self._get("/public/funding-rate-history", {"instId": inst, "after": cursor, "limit": _PAGE})
            if not rows:
                break
            df = pd.DataFrame(rows)
            frames.append(df)
            oldest = int(df["fundingTime"].astype("int64").min())
            if len(rows) < _PAGE or oldest >= cursor:
                break
            cursor = oldest
        if not frames:
            return empty_funding()
        df = pd.concat(frames, ignore_index=True)
        realized = df.get("realizedRate", pd.Series([""] * len(df))).astype(str)
        rate = pd.Series(df["fundingRate"]).astype(str).where(realized == "", realized)
        out = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(df["fundingTime"].astype("int64"), unit="ms", utc=True),
                "funding_rate": rate.astype(float),
            }
        )
        return finalize_funding(out, start, end)

    def _candles(self, path: str, inst: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        if interval not in _BARS:
            raise UnsupportedInterval(interval)
        cursor = ms(end)
        frames = []
        while cursor > ms(start):
            rows = self._get(path, {"instId": inst, "bar": _BARS[interval], "after": cursor, "limit": _PAGE})
            if not rows:
                break
            df = pd.DataFrame(rows)
            frames.append(df)
            oldest = int(df[0].astype("int64").min())
            if len(rows) < _PAGE or oldest >= cursor:
                break
            cursor = oldest
        if not frames:
            return empty_candles()
        df = pd.concat(frames, ignore_index=True)
        out = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(df[0].astype("int64"), unit="ms", utc=True),
                "open": df[1].astype(float),
                "high": df[2].astype(float),
                "low": df[3].astype(float),
                "close": df[4].astype(float),
                "volume": df[5].astype(float) if df.shape[1] > 5 else 0.0,
            }
        )
        return finalize_candles(out, start, end)

    def fetch_candles(self, symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        return self._candles("/market/history-candles", self.normalize_symbol(symbol), interval, start, end)

    def fetch_index_candles(self, symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame | None:
        return self._candles("/market/history-index-candles", self._index_id(symbol), interval, start, end)

    def close(self) -> None:
        self._http.close()
