import numpy as np
import pandas as pd
import pytest

from perplexify.data.dataset import MarketData
from perplexify.data.synthetic import make_synthetic_market_data
from perplexify.strategies import REGISTRY, make_strategy, strategy_catalog
from perplexify.strategies.features import hysteresis_state, zscore


def df(values, cols=("A",)):
    return pd.DataFrame(np.asarray(values, dtype=float).reshape(len(values), -1), columns=list(cols))


def test_hysteresis_state_machine():
    x = df([0, 3, 2, 1, 0, -3, -1, 0, 3])
    s = hysteresis_state(enter_pos=x > 2, exit_pos=x < 1, enter_neg=x < -2, exit_neg=x > -1)
    assert s["A"].tolist() == [0, 1, 1, 1, 0, -1, -1, 0, 1]


def test_hysteresis_ignores_nan_and_can_flip_directly():
    x = df([np.nan, np.nan, 3, -3, 3])
    s = hysteresis_state(enter_pos=x > 2, exit_pos=x < 1, enter_neg=x < -2, exit_neg=x > -1)
    assert s["A"].tolist() == [0, 0, 1, -1, 1]


def test_zscore_is_nan_during_warmup_and_zero_mean_after():
    x = df(np.arange(50))
    z = zscore(x, 10)
    assert z["A"].iloc[:9].isna().all() and np.isfinite(z["A"].iloc[9:]).all()


def carry_panel(apr_path, bar_hours=8.0):
    per_bar = np.asarray(apr_path) * bar_hours / (365 * 24)
    idx = pd.date_range("2024-01-01", periods=len(per_bar), freq="8h", tz="UTC")
    f = pd.DataFrame({"BTC": per_bar}, index=idx)
    p = pd.DataFrame({"BTC": 100.0}, index=idx)
    return MarketData(f, p, p.copy(), bar_hours, "test")


def test_funding_carry_is_flat_in_warmup_and_delta_neutral():
    md = carry_panel([0.30] * 10 + [0.01] * 5)
    t = make_strategy("funding_carry", {"lookback_bars": 3, "entry_apr": 0.10, "exit_apr": 0.03, "leverage": 0.8}).generate_targets(md)
    assert (t.perp.iloc[:2] == 0).all().all()  # warm-up: no mirror trades from NaN
    assert t.perp.iloc[2, 0] == -0.8 and t.hedge.iloc[2, 0] == 0.8
    pd.testing.assert_frame_equal(t.perp, -t.hedge)
    assert t.perp.iloc[-1, 0] == 0.0  # funding decayed below exit


def test_funding_carry_negative_side_only_when_allowed():
    md = carry_panel([-0.40] * 10)
    off = make_strategy("funding_carry", {"lookback_bars": 2, "entry_apr": 0.1}).generate_targets(md)
    on = make_strategy("funding_carry", {"lookback_bars": 2, "entry_apr": 0.1, "allow_negative_carry": True}).generate_targets(md)
    assert (off.perp == 0).all().all()
    assert on.perp.iloc[-1, 0] == 1.0 and on.hedge.iloc[-1, 0] == -1.0


def test_contrarian_shorts_a_funding_spike():
    base = [0.0001] * 60 + [0.002] * 5 + [0.0001] * 10
    idx = pd.date_range("2024-01-01", periods=len(base), freq="8h", tz="UTC")
    f = pd.DataFrame({"BTC": base}, index=idx)
    f.iloc[:60, 0] += 1e-5 * np.where(np.arange(60) % 2 == 0, 1.0, -1.0)  # bounded |z| <= 1 before the spike
    p = pd.DataFrame({"BTC": 100.0}, index=idx)
    md = MarketData(f, p, p.copy(), 8.0, "test")
    t = make_strategy("funding_contrarian", {"z_window": 40, "smoothing_bars": 1, "z_entry": 2.0, "leverage": 0.5, "hedge_ratio": 0.5}).generate_targets(md)
    assert t.perp.iloc[62, 0] == -0.5 and t.hedge.iloc[62, 0] == 0.25
    assert t.perp.iloc[:60].abs().max().max() == 0.0


def test_cross_sectional_picks_top_n_and_respects_budget():
    md = make_synthetic_market_data(("A", "B", "C", "D"), n_bars=300, bar_hours=8, seed=1)
    t = make_strategy("cross_sectional_carry", {"top_n": 2, "min_apr": -10, "leverage": 1.0, "lookback_bars": 3, "rebalance_bars": 4}).generate_targets(md)
    active = (t.perp != 0).sum(axis=1)
    assert active.iloc[10:].max() == 2
    assert t.hedge.abs().sum(axis=1).iloc[10:].max() == pytest.approx(1.0)
    pd.testing.assert_frame_equal(t.perp, -t.hedge)
    # weights only change on rebalance bars
    changes = (t.perp.diff().abs().sum(axis=1) > 0).to_numpy()
    assert not changes[np.arange(len(changes)) % 4 != 0].any()


def test_registry_and_param_validation():
    assert {"funding_carry", "funding_contrarian", "funding_momentum", "cross_sectional_carry", "buy_and_hold", "always_carry"} <= set(REGISTRY)
    with pytest.raises(KeyError):
        make_strategy("nope")
    with pytest.raises(ValueError):
        make_strategy("funding_carry", {"not_a_param": 1})
    cat = {c["name"]: c for c in strategy_catalog()}
    assert cat["funding_carry"]["params"]["entry_apr"]["default"] == 0.10
