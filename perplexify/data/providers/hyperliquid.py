"""Hyperliquid public info API (https://api.hyperliquid.xyz/info).

* Funding settles hourly; full history is available (paged 500 rows).
* Candles: only the most recent 5000 bars per interval are served, so pick a
  coarser interval for longer backtests (4h ~ 2.3 years, 1d = full history).
* ``predictedFundings`` exposes the next funding rate for the same coin on
  Binance, Bybit and Hyperliquid: the basis for the cross-venue scanner.
"""

from __future__ import annotations

import logging

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
from perplexify.utils import HOURS_PER_YEAR, from_ms, ms

log = logging.getLogger(__name__)

_INTERVALS = {"1h": 1, "2h": 2, "4h": 4, "8h": 8, "12h": 12, "1d": 24}
_MAX_CANDLES = 5000
_FUNDING_PAGE = 500


class HyperliquidProvider(FundingProvider):
    name = "hyperliquid"
    funding_interval_hours = 1.0
    candle_intervals = tuple(_INTERVALS)
    candle_history_days = {k: _MAX_CANDLES * h / 24 for k, h in _INTERVALS.items()}

    def __init__(self, base_url: str = "https://api.hyperliquid.xyz") -> None:
        self._http = HttpClient(base_url)

    def normalize_symbol(self, symbol: str) -> str:
        s = symbol.upper().strip()
        for suffix in ("-USD-PERP", "-USDT-SWAP", "-USD", "USDT", "USD", "-PERP"):
            if s.endswith(suffix) and len(s) > len(suffix):
                s = s[: -len(suffix)]
                break
        return s

    # -- live --------------------------------------------------------------
    def list_markets(self) -> pd.DataFrame:
        meta, ctxs = self._http.post("/info", json={"type": "metaAndAssetCtxs"})
        rows = []
        for asset, ctx in zip(meta["universe"], ctxs):
            if asset.get("isDelisted"):
                continue
            rate = float(ctx.get("funding") or 0.0)
            mark = float(ctx.get("markPx") or "nan")
            rows.append(
                {
                    "symbol": asset["name"],
                    "mark_price": mark,
                    "index_price": float(ctx.get("oraclePx") or "nan"),
                    "funding_rate": rate,
                    "funding_interval_hours": self.funding_interval_hours,
                    "funding_apr": annualize_rate(rate, self.funding_interval_hours),
                    "next_funding_time": pd.NaT,
                    "open_interest": float(ctx.get("openInterest") or 0.0) * (mark if mark == mark else 0.0),
                    "volume_24h": float(ctx.get("dayNtlVlm") or 0.0),
                    "max_leverage": asset.get("maxLeverage"),
                }
            )
        df = pd.DataFrame(rows, columns=MARKET_COLUMNS + ["max_leverage"])
        return df.sort_values("volume_24h", ascending=False).reset_index(drop=True)

    def predicted_fundings(self) -> pd.DataFrame:
        """Next funding rate per coin on each venue Hyperliquid tracks (Binance, Bybit, HL)."""
        raw = self._http.post("/info", json={"type": "predictedFundings"})
        venue_names = {"BinPerp": "binance", "BybitPerp": "bybit", "HlPerp": "hyperliquid"}
        rows = []
        for coin, venues in raw:
            for venue, info in venues:
                if not info:
                    continue
                hours = float(info.get("fundingIntervalHours") or 8)
                rate = float(info.get("fundingRate") or 0.0)
                rows.append(
                    {
                        "symbol": coin,
                        "venue": venue_names.get(venue, venue),
                        "funding_rate": rate,
                        "funding_interval_hours": hours,
                        "funding_apr": rate * HOURS_PER_YEAR / hours,
                        "next_funding_time": from_ms(info["nextFundingTime"]) if info.get("nextFundingTime") else pd.NaT,
                    }
                )
        return pd.DataFrame(rows)

    # -- history -----------------------------------------------------------
    def fetch_funding_history(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        coin = self.normalize_symbol(symbol)
        cursor = ms(start)
        end_ms = ms(end)
        frames = []
        while cursor <= end_ms:
            page = self._http.post(
                "/info", json={"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end_ms}
            )
            if not page:
                break
            df = pd.DataFrame(page)
            frames.append(df)
            last = int(df["time"].max())
            if len(page) < _FUNDING_PAGE or last <= cursor:
                break
            cursor = last + 1
        if not frames:
            return empty_funding()
        df = pd.concat(frames, ignore_index=True)
        out = pd.DataFrame(
            {
                # settlement timestamps carry a few ms of jitter; snap to the hour
                "timestamp": pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True).dt.floor("h"),
                "funding_rate": df["fundingRate"].astype(float),
                "premium": df["premium"].astype(float),
            }
        )
        return finalize_funding(out, start, end)

    def fetch_candles(self, symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        if interval not in _INTERVALS:
            raise UnsupportedInterval(interval)
        coin = self.normalize_symbol(symbol)
        cursor = ms(start)
        end_ms = ms(end)
        frames = []
        while cursor < end_ms:
            page = self._http.post(
                "/info",
                json={
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": interval, "startTime": cursor, "endTime": end_ms},
                },
            )
            if not page:
                break
            df = pd.DataFrame(page)
            frames.append(df)
            last_close = int(df["T"].max())
            if len(page) < _MAX_CANDLES or last_close <= cursor:
                break
            cursor = last_close + 1
        if not frames:
            return empty_candles()
        df = pd.concat(frames, ignore_index=True)
        out = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(df["t"].astype("int64"), unit="ms", utc=True),
                "open": df["o"].astype(float),
                "high": df["h"].astype(float),
                "low": df["l"].astype(float),
                "close": df["c"].astype(float),
                "volume": df["v"].astype(float),
            }
        )
        out = finalize_candles(out, start, end)
        if not out.empty and out["timestamp"].iloc[0] > start + pd.Timedelta(hours=48):
            log.warning(
                "hyperliquid serves only the latest %d %s candles for %s; history starts %s (requested %s)",
                _MAX_CANDLES, interval, coin, out["timestamp"].iloc[0].date(), start.date(),
            )
        return out

    def close(self) -> None:
        self._http.close()
