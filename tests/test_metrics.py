import math

import numpy as np
import pandas as pd
import pytest

from perplexify.backtest.metrics import compute_metrics


def _frames(equity):
    idx = equity.index
    pnl = pd.DataFrame({"price": 0.0, "funding": equity.diff().fillna(0.0), "fees": 0.0, "borrow": 0.0}, index=idx)
    pnl["total"] = pnl.sum(axis=1)
    pos = pd.DataFrame({"BTC": -1.0}, index=idx)
    trades = pd.DataFrame({"pnl": [10.0, -4.0, 6.0], "return": [0.1, -0.04, 0.06], "bars": [5, 2, 9]})
    return pnl, trades, pos, -pos


def test_annualized_return_uses_calendar_time():
    idx = pd.date_range("2024-01-01", "2025-01-01", freq="1D", tz="UTC")  # exactly one leap year = 366 days
    eq = pd.Series(np.linspace(100.0, 200.0, len(idx)), index=idx)
    m = compute_metrics(eq, 24.0, *_frames(eq))
    years = 366 / 365.25
    assert m["total_return"] == pytest.approx(1.0)
    assert m["annualized_return"] == pytest.approx(2.0 ** (1 / years) - 1)
    assert m["max_drawdown"] == 0.0 and m["time_in_market"] == 1.0
    assert m["win_rate"] == pytest.approx(2 / 3) and m["profit_factor"] == pytest.approx(4.0)


def test_max_drawdown_and_duration():
    idx = pd.date_range("2024-01-01", periods=6, freq="8h", tz="UTC")
    eq = pd.Series([100, 120, 90, 100, 130, 125], index=idx, dtype=float)
    m = compute_metrics(eq, 8.0, *_frames(eq))
    assert m["max_drawdown"] == pytest.approx(90 / 120 - 1)
    assert m["max_drawdown_days"] == pytest.approx(round(2 * 8 / 24, 1))  # bars 2 and 3 under water
    assert m["sharpe"] > 0 and math.isfinite(m["sortino"])
