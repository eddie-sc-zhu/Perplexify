"""Canonical backtest panel and the builder that assembles it from a provider.

``MarketData`` holds three aligned wide frames (rows = bar close timestamps,
columns = symbols):

* ``funding``     funding paid over the bar, i.e. the sum of every settlement
                  in (t - bar, t]; a short perp *receives* this amount
* ``perp_close``  perpetual price at the bar close
* ``index_close`` spot/index price at the bar close (hedge leg); equals
                  ``perp_close`` when the venue exposes no index series

Decisions made at row t are held over (t, t+1] and settle at row t+1, which
is what keeps the backtester free of look-ahead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from perplexify.data.providers.base import FundingProvider, UnsupportedInterval
from perplexify.data.storage import DataStore
from perplexify.utils import interval_to_hours, interval_to_timedelta, to_utc, utc_now

log = logging.getLogger(__name__)


@dataclass
class MarketData:
    funding: pd.DataFrame
    perp_close: pd.DataFrame
    index_close: pd.DataFrame
    bar_hours: float
    exchange: str = "unknown"
    has_index: bool = False
    meta: dict = field(default_factory=dict)

    # -- basic accessors ---------------------------------------------------
    @property
    def symbols(self) -> list[str]:
        return list(self.funding.columns)

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.funding.index

    @property
    def start(self) -> pd.Timestamp:
        return self.index[0]

    @property
    def end(self) -> pd.Timestamp:
        return self.index[-1]

    def __len__(self) -> int:
        return len(self.funding)

    def _with(self, mask) -> "MarketData":
        return MarketData(
            self.funding[mask], self.perp_close[mask], self.index_close[mask],
            self.bar_hours, self.exchange, self.has_index, dict(self.meta),
        )

    def slice(self, start=None, end=None) -> "MarketData":
        s, e = to_utc(start), to_utc(end)
        mask = np.ones(len(self), dtype=bool)
        if s is not None:
            mask &= np.asarray(self.index >= s)
        if e is not None:
            mask &= np.asarray(self.index <= e)
        return self._with(mask)

    def select(self, symbols: list[str]) -> "MarketData":
        return MarketData(
            self.funding[symbols], self.perp_close[symbols], self.index_close[symbols],
            self.bar_hours, self.exchange, self.has_index, dict(self.meta),
        )

    def split(self, at) -> tuple["MarketData", "MarketData"]:
        """In-sample / out-of-sample split at a timestamp or at a fraction of the rows."""
        if isinstance(at, (int, float)) and 0 < at < 1:
            cut = self.index[int(len(self) * at)]
        else:
            cut = to_utc(at)
        mask = np.asarray(self.index < cut)
        return self._with(mask), self._with(~mask)

    def resample(self, bar_hours: float) -> "MarketData":
        """Aggregate to a coarser bar: funding summed, prices taken at the bar close."""
        if bar_hours < self.bar_hours:
            raise ValueError(f"cannot resample {self.bar_hours}h bars to finer {bar_hours}h bars")
        if bar_hours == self.bar_hours:
            return self
        bar = pd.Timedelta(hours=bar_hours)
        rs = lambda df: df.resample(bar, closed="right", label="right")  # noqa: E731
        funding = rs(self.funding).sum()
        perp = rs(self.perp_close).last()
        index = rs(self.index_close).last()
        keep = perp.notna().any(axis=1)
        return MarketData(funding[keep], perp[keep], index[keep], bar_hours, self.exchange, self.has_index, dict(self.meta))

    # -- conversions -------------------------------------------------------
    def to_long(self) -> pd.DataFrame:
        f = self.funding.stack(future_stack=True).rename("funding_rate")
        p = self.perp_close.stack(future_stack=True).rename("perp_close")
        i = self.index_close.stack(future_stack=True).rename("index_close")
        out = pd.concat([f, p, i], axis=1).reset_index()
        out.columns = ["timestamp", "symbol", "funding_rate", "perp_close", "index_close"]
        return out

    @classmethod
    def from_long(cls, df: pd.DataFrame, bar_hours: float, exchange: str = "unknown") -> "MarketData":
        df = df.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

        def piv(col: str) -> pd.DataFrame:
            return df.pivot(index="timestamp", columns="symbol", values=col).sort_index()

        funding = piv("funding_rate").fillna(0.0)
        perp = piv("perp_close")
        has_index = "index_close" in df and df["index_close"].notna().any()
        index = piv("index_close").fillna(perp) if has_index else perp.copy()
        return cls(funding, perp, index, bar_hours, exchange, has_index)

    def summary(self) -> pd.DataFrame:
        yearly = 365 * 24 / self.bar_hours
        rows = []
        for s in self.symbols:
            f = self.funding[s]
            p = self.perp_close[s].dropna()
            rows.append(
                {
                    "symbol": s,
                    "bars": int(p.shape[0]),
                    "start": p.index[0] if len(p) else pd.NaT,
                    "end": p.index[-1] if len(p) else pd.NaT,
                    "mean_funding_apr": float(f.mean() * yearly),
                    "pct_bars_positive_funding": float((f > 0).mean()),
                    "price_return": float(p.iloc[-1] / p.iloc[0] - 1) if len(p) > 1 else np.nan,
                }
            )
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
def _close_at_bar_end(candles: pd.DataFrame, candle_interval: str, bar: pd.Timedelta) -> pd.Series:
    """Series of close prices indexed by bar-close timestamp."""
    if candles is None or candles.empty:
        return pd.Series(dtype="float64")
    close_ts = candles["timestamp"] + interval_to_timedelta(candle_interval)
    s = pd.Series(candles["close"].to_numpy(), index=pd.DatetimeIndex(close_ts))
    if interval_to_timedelta(candle_interval) == bar:
        return s
    # finer candles: the last close in (T - bar, T] is the price at T
    return s.resample(bar, closed="right", label="right").last().dropna()


def _funding_per_bar(funding: pd.DataFrame, bar: pd.Timedelta) -> pd.DataFrame:
    cols = ["funding_rate", "premium", "index_price", "n_settlements"]
    if funding.empty:
        return pd.DataFrame(columns=cols, dtype="float64")
    f = funding.set_index("timestamp").sort_index()
    rs = lambda col: f[col].resample(bar, closed="right", label="right")  # noqa: E731
    return pd.DataFrame(
        {
            "funding_rate": rs("funding_rate").sum(),
            "premium": rs("premium").last(),
            "index_price": rs("index_price").last(),
            "n_settlements": rs("funding_rate").count(),
        }
    )


def drop_frozen_oracle(funding: pd.DataFrame, min_repeats: int = 2) -> pd.DataFrame:
    """Blank out ``index_price`` values that repeat the previous settlement exactly.

    Oracle feeds update every settlement; the same price for ``min_repeats``
    consecutive settlements means the feed was frozen (dYdX, 2025-10-10/11).
    The first value of a run is kept, later repeats become NaN so the bar
    builder falls back to the perp price.
    """
    if funding.empty or "index_price" not in funding or funding["index_price"].isna().all():
        return funding
    f = funding.copy()
    px = f["index_price"]
    repeat = px.eq(px.shift()) & px.notna()
    run = repeat.groupby((~repeat).cumsum()).cumsum()
    frozen = run >= min_repeats - 1
    if frozen.any():
        log.info("%d settlements with a frozen oracle price dropped", int(frozen.sum()))
        f.loc[frozen, "index_price"] = float("nan")
    return f


def build_symbol_frame(
    provider: FundingProvider,
    symbol: str,
    interval: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    store: DataStore,
    refresh: bool = False,
) -> pd.DataFrame | None:
    bar = interval_to_timedelta(interval)
    # candles: native interval when available, else 1h resampled
    candle_interval = interval if interval in provider.candle_intervals else "1h"
    try:
        candles = store.get_candles(provider, symbol, candle_interval, start, end, refresh)
    except UnsupportedInterval:
        candle_interval = "1h"
        candles = store.get_candles(provider, symbol, candle_interval, start, end, refresh)
    if candles.empty:
        log.warning("%s: no candles for %s", provider.name, symbol)
        return None
    perp = _close_at_bar_end(candles, candle_interval, bar).rename("perp_close")

    funding = store.get_funding(provider, symbol, start, end, refresh)
    funding = drop_frozen_oracle(funding)
    fb = _funding_per_bar(funding, bar)

    index: pd.Series | None = None
    idx_candles = store.get_index_candles(provider, symbol, candle_interval, start, end, refresh)
    if idx_candles is not None and not idx_candles.empty:
        index = _close_at_bar_end(idx_candles, candle_interval, bar)
    elif fb["index_price"].notna().any():
        index = fb["index_price"]
    elif fb["premium"].notna().any():
        # Hyperliquid: premium = (mark - oracle) / oracle at the settlement
        index = perp / (1.0 + fb["premium"].reindex(perp.index).ffill().fillna(0.0))

    out = pd.DataFrame({"perp_close": perp})
    out["funding_rate"] = fb["funding_rate"].reindex(out.index).fillna(0.0)
    # A missing or frozen oracle (exchange halt) is not a price: mark the hedge at the
    # perp (zero basis) for that bar rather than carrying a stale value forward.
    out["index_close"] = index.reindex(out.index) if index is not None else out["perp_close"]
    out["index_close"] = out["index_close"].fillna(out["perp_close"])
    out = out[(out.index >= start) & (out.index <= end)]
    out.index.name = "timestamp"
    return out


def build_market_data(
    exchange: str | FundingProvider,
    symbols: list[str],
    interval: str = "4h",
    start=None,
    end=None,
    store: DataStore | None = None,
    refresh: bool = False,
) -> MarketData:
    from perplexify.data.providers import get_provider

    provider = exchange if isinstance(exchange, FundingProvider) else get_provider(exchange)
    store = store or DataStore()
    end_ts = to_utc(end) or utc_now().floor(interval_to_timedelta(interval))
    start_ts = to_utc(start) or end_ts - pd.Timedelta(days=365)
    frames: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        key = provider.normalize_symbol(sym)
        label = sym.upper()
        try:
            frame = build_symbol_frame(provider, key, interval, start_ts, end_ts, store, refresh)
        except Exception as exc:  # keep going with the other symbols
            log.error("%s: failed to build %s: %s", provider.name, sym, exc)
            continue
        if frame is not None and not frame.empty:
            frames[label] = frame
    if not frames:
        raise RuntimeError(f"No data could be built for {symbols} on {provider.name}")
    funding = pd.DataFrame({s: f["funding_rate"] for s, f in frames.items()}).sort_index()
    perp = pd.DataFrame({s: f["perp_close"] for s, f in frames.items()}).sort_index()
    index = pd.DataFrame({s: f["index_close"] for s, f in frames.items()}).sort_index()
    has_index = any(not f["index_close"].equals(f["perp_close"]) for f in frames.values())
    md = MarketData(
        funding.fillna(0.0), perp, index.fillna(perp), interval_to_hours(interval), provider.name, has_index,
        meta={"interval": interval, "requested_start": str(start_ts), "requested_end": str(end_ts)},
    )
    log.info("built %s panel: %d bars x %d symbols (%s -> %s)", provider.name, len(md), len(md.symbols), md.start, md.end)
    return md
