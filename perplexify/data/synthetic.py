"""Synthetic funding/price panel for tests, demos and offline development.

Funding follows a regime-switching AR(1): the regime sets the mean level
(bullish crowding -> high positive funding, capitulation -> negative), and
price drift is mildly tied to the regime so directional strategies have
something to find. Premium (perp vs index basis) is proportional to funding
plus noise, which gives the delta-neutral carry trade realistic basis risk.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from perplexify.data.dataset import MarketData

_REGIMES = {
    # name: (mean funding per 8h, price drift per 8h, transition stickiness)
    "euphoria": (0.00030, 0.0015, 0.985),
    "neutral": (0.00008, 0.0002, 0.990),
    "fear": (-0.00010, -0.0012, 0.980),
}


def make_synthetic_market_data(
    symbols: tuple[str, ...] | list[str] = ("BTC", "ETH", "SOL"),
    n_bars: int = 3 * 365 * 3,
    bar_hours: float = 8.0,
    seed: int = 7,
    start: str = "2023-01-01",
    end=None,
    funding_vol: float = 0.00012,
    price_vol: float = 0.018,
) -> MarketData:
    rng = np.random.default_rng(seed)
    scale = bar_hours / 8.0
    names = list(_REGIMES)
    if end is not None:
        end_ts = pd.Timestamp(end).floor(pd.Timedelta(hours=bar_hours))
        idx = pd.date_range(end=end_ts, periods=n_bars, freq=pd.Timedelta(hours=bar_hours), tz="UTC")
    else:
        idx = pd.date_range(start, periods=n_bars, freq=pd.Timedelta(hours=bar_hours), tz="UTC")

    # one market-wide regime path shared by every symbol
    regime = np.zeros(n_bars, dtype=int)
    regime[0] = 1
    for t in range(1, n_bars):
        stay = _REGIMES[names[regime[t - 1]]][2]
        regime[t] = regime[t - 1] if rng.random() < stay else rng.integers(0, len(names))
    mean_f = np.array([_REGIMES[names[r]][0] for r in regime]) * scale
    drift = np.array([_REGIMES[names[r]][1] for r in regime]) * scale

    funding, perp, index = {}, {}, {}
    for k, sym in enumerate(symbols):
        beta = 1.0 + 0.35 * k  # alts carry more extreme funding and vol
        f = np.zeros(n_bars)
        for t in range(1, n_bars):
            f[t] = mean_f[t] * beta + 0.85 * (f[t - 1] - mean_f[t - 1] * beta) + funding_vol * beta * np.sqrt(scale) * rng.standard_normal()
        rets = drift * beta + price_vol * beta * np.sqrt(scale) * rng.standard_normal(n_bars)
        px = 100.0 * (1 + 0.5 * k) * np.exp(np.cumsum(rets))
        premium = 2.0 * f + 0.0004 * rng.standard_normal(n_bars)
        funding[sym] = f
        perp[sym] = px
        index[sym] = px / (1.0 + premium)

    return MarketData(
        pd.DataFrame(funding, index=idx),
        pd.DataFrame(perp, index=idx),
        pd.DataFrame(index, index=idx),
        bar_hours,
        exchange="synthetic",
        has_index=True,
        meta={"seed": seed, "regimes": names},
    )
