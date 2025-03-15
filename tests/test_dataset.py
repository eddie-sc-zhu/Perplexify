import numpy as np
import pandas as pd
import pytest

from perplexify.data.dataset import MarketData, _close_at_bar_end, _funding_per_bar, build_market_data
from perplexify.data.providers.base import FundingProvider
from perplexify.data.storage import DataStore
from perplexify.data.synthetic import make_synthetic_market_data


def test_close_at_bar_end_resamples_finer_candles():
    ts = pd.date_range("2024-01-01 00:00", periods=8, freq="1h", tz="UTC")
    candles = pd.DataFrame({"timestamp": ts, "open": 0.0, "high": 0.0, "low": 0.0, "close": range(8), "volume": 0.0})
    s = _close_at_bar_end(candles, "1h", pd.Timedelta(hours=4))
    # candle opening 03:00 closes at 04:00 -> price at the 04:00 bar close is 3; 07:00 candle -> 08:00 bar = 7
    assert s.index.tolist() == [pd.Timestamp("2024-01-01 04:00", tz="UTC"), pd.Timestamp("2024-01-01 08:00", tz="UTC")]
    assert s.tolist() == [3.0, 7.0]


def test_funding_per_bar_sums_settlements_in_right_closed_bins():
    ts = pd.date_range("2024-01-01 01:00", periods=8, freq="1h", tz="UTC")  # 01:00 .. 08:00
    f = pd.DataFrame({"timestamp": ts, "funding_rate": 1.0, "premium": range(8), "index_price": float("nan")})
    fb = _funding_per_bar(f, pd.Timedelta(hours=4))
    assert fb.loc[pd.Timestamp("2024-01-01 04:00", tz="UTC"), "funding_rate"] == 4.0  # 01,02,03,04
    assert fb.loc[pd.Timestamp("2024-01-01 08:00", tz="UTC"), "funding_rate"] == 4.0  # 05..08
    assert fb.loc[pd.Timestamp("2024-01-01 04:00", tz="UTC"), "premium"] == 3.0


def test_long_roundtrip_slice_split():
    md = make_synthetic_market_data(("BTC", "ETH"), n_bars=100, bar_hours=8)
    back = MarketData.from_long(md.to_long(), 8.0)
    pd.testing.assert_frame_equal(back.funding, md.funding, check_names=False, check_freq=False)
    pd.testing.assert_frame_equal(back.index_close, md.index_close, check_names=False, check_freq=False)
    a, b = md.split(0.7)
    assert len(a) == 70 and len(b) == 30 and a.end < b.start
    s = md.slice(start=md.index[10], end=md.index[19])
    assert len(s) == 10 and s.start == md.index[10]


class FakeProvider(FundingProvider):
    """Hourly funding with premium (Hyperliquid-like), 1h candles only."""

    name = "fake"
    funding_interval_hours = 1.0
    candle_intervals = ("1h",)

    def __init__(self):
        self.calls = []

    def normalize_symbol(self, symbol):
        return symbol.upper()

    def list_markets(self):
        return pd.DataFrame()

    def fetch_funding_history(self, symbol, start, end):
        self.calls.append(("funding", start, end))
        ts = pd.date_range(start.ceil("h"), end, freq="1h", tz="UTC")
        return pd.DataFrame({"timestamp": ts, "funding_rate": 0.0001, "premium": 0.001, "index_price": float("nan")})

    def fetch_candles(self, symbol, interval, start, end):
        self.calls.append(("candles", start, end))
        ts = pd.date_range(start.ceil("h"), end - pd.Timedelta(hours=1), freq="1h", tz="UTC")
        px = 100.0 + pd.Series(range(len(ts)), dtype=float)
        return pd.DataFrame({"timestamp": ts, "open": px, "high": px, "low": px, "close": px, "volume": 1.0})


def test_build_market_data_aligns_and_derives_index(tmp_path):
    prov = FakeProvider()
    store = DataStore(tmp_path)
    start, end = pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC")
    md = build_market_data(prov, ["btc"], "4h", start, end, store=store)
    assert md.symbols == ["BTC"] and md.bar_hours == 4.0 and md.has_index
    assert md.funding.iloc[1, 0] == pytest.approx(0.0004)  # four hourly settlements per 4h bar
    assert md.index_close.iloc[1, 0] == pytest.approx(md.perp_close.iloc[1, 0] / 1.001)
    n_calls = len(prov.calls)
    # second build is served from the cache without new requests
    build_market_data(prov, ["BTC"], "4h", start, end, store=store)
    assert len(prov.calls) == n_calls
    # extending the window only fetches the tail
    build_market_data(prov, ["BTC"], "4h", start, end + pd.Timedelta(days=1), store=store)
    assert all(c[1] >= end - pd.Timedelta(hours=3) for c in prov.calls[n_calls:])  # one-bar overlap by design


def test_frozen_oracle_is_dropped_and_hedge_marked_at_perp():
    from perplexify.data.dataset import drop_frozen_oracle

    ts = pd.date_range("2025-10-10 20:00", periods=8, freq="1h", tz="UTC")
    f = pd.DataFrame({"timestamp": ts, "funding_rate": 0.0, "premium": float("nan"),
                      "index_price": [116.0, 114.0, 106.0, 106.0, 106.0, 106.0, 112.0, 111.0]})
    out = drop_frozen_oracle(f)
    assert out["index_price"].tolist()[:3] == [116.0, 114.0, 106.0]  # first value of the run is kept
    assert out["index_price"].iloc[3:6].isna().all()  # frozen repeats dropped
    assert out["index_price"].tolist()[6:] == [112.0, 111.0]

    class FrozenProvider(FakeProvider):
        def fetch_funding_history(self, symbol, start, end):
            t = pd.date_range(start.ceil("h"), end, freq="1h", tz="UTC")
            px = pd.Series(100.0 + np.arange(len(t), dtype=float))
            px.iloc[10:16] = px.iloc[9]  # oracle halts for six hours
            return pd.DataFrame({"timestamp": t, "funding_rate": 0.0001, "premium": float("nan"), "index_price": px})

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        md = build_market_data(FrozenProvider(), ["BTC"], "1h", pd.Timestamp("2024-01-01", tz="UTC"),
                               pd.Timestamp("2024-01-02", tz="UTC"), store=DataStore(d))
    idx, perp = md.index_close["BTC"], md.perp_close["BTC"]
    t = lambda h: pd.Timestamp(f"2024-01-01 {h:02d}:00", tz="UTC")  # noqa: E731
    frozen_bars = idx.index[(idx.index >= t(10)) & (idx.index <= t(15))]  # settlements that repeated 09:00's price
    assert (idx.loc[frozen_bars] == perp.loc[frozen_bars]).all()  # hedge marked at perp while the oracle is frozen
    assert idx.loc[t(9)] != perp.loc[t(9)] and idx.loc[t(16)] != perp.loc[t(16)]  # genuine oracle values kept
