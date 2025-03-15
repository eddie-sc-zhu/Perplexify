import numpy as np
import pandas as pd
import pytest

from perplexify.backtest import EngineConfig, RiskConfig, run_backtest
from perplexify.data.dataset import MarketData
from perplexify.data.synthetic import make_synthetic_market_data
from perplexify.strategies import Strategy, StrategyParams, Targets, make_strategy


def panel(perp, funding, index=None, bar_hours=8.0, symbol="BTC") -> MarketData:
    idx = pd.date_range("2024-01-01", periods=len(perp), freq=f"{int(bar_hours)}h", tz="UTC")
    p = pd.DataFrame({symbol: perp}, index=idx, dtype=float)
    f = pd.DataFrame({symbol: funding}, index=idx, dtype=float)
    i = pd.DataFrame({symbol: index}, index=idx, dtype=float) if index is not None else p.copy()
    return MarketData(f, p, i, bar_hours, "test", index is not None)


class Fixed(Strategy):
    """Constant weights from bar 0 (test helper)."""

    name = "fixed"

    class Params(StrategyParams):
        perp: float = -1.0
        hedge: float = 1.0

    def generate_targets(self, data):
        p = pd.DataFrame(self.params.perp, index=data.index, columns=data.symbols)
        h = pd.DataFrame(self.params.hedge, index=data.index, columns=data.symbols)
        return Targets(p, h)


NO_RISK = RiskConfig(max_drawdown=None, max_gross_leverage=10.0, max_position_weight=10.0)


def test_delta_neutral_carry_earns_exactly_funding_minus_fees():
    md = panel(perp=[100, 110, 99, 120], funding=[0.0, 0.001, -0.002, 0.003])
    cfg = EngineConfig(initial_equity=1000.0, perp_fee=0.001, hedge_fee=0.002, slippage_bps=0.0, risk=NO_RISK)
    res = run_backtest(md, Fixed(), cfg)
    # bar 0: enter -1 perp / +1 hedge, fees = 1000 * (0.001 + 0.002)
    e0 = 1000.0 - 3.0
    assert res.equity.iloc[0] == pytest.approx(e0)
    # price P&L cancels (index == perp); short perp receives +funding each bar; weights drift and are
    # restored to +/-1 each bar, which costs fees on the drift only.
    e = e0
    for t in range(1, 4):
        r = md.perp_close.iloc[t, 0] / md.perp_close.iloc[t - 1, 0] - 1
        e_prev = e
        e = e_prev + e_prev * (-1 * r + 1 * r) + e_prev * 1.0 * md.funding.iloc[t, 0]
        w = 1.0 * (1 + r) * e_prev / e  # drifted magnitude of each leg
        e -= e * (abs(-1 - -w) * 0.001 + abs(1 - w) * 0.002)
        assert res.equity.iloc[t] == pytest.approx(e, rel=1e-12)
    assert res.pnl["price"].abs().sum() == pytest.approx(0.0)
    assert res.pnl["funding"].sum() > 0


def test_rebalance_band_skips_small_trims_but_not_entries_or_exits():
    md = panel(perp=[100, 101, 99, 100, 100], funding=[0.0] * 5)
    base = dict(initial_equity=1000.0, perp_fee=0.001, hedge_fee=0.0, slippage_bps=0.0, risk=NO_RISK)
    exact = run_backtest(md, Fixed(perp=-1.0, hedge=0.0), EngineConfig(**base))
    banded = run_backtest(md, Fixed(perp=-1.0, hedge=0.0), EngineConfig(rebalance_band=0.05, **base))
    assert exact.pnl["fees"].iloc[1:].abs().sum() > 0  # exact engine re-trims drift every bar
    assert banded.pnl["fees"].iloc[1:].abs().sum() == 0  # 1-2% drift stays inside the band
    assert banded.pnl["fees"].iloc[0] == pytest.approx(-1.0)  # entry still paid
    assert banded.positions_perp.iloc[1, 0] != -1.0  # drifted weight is kept
    # a close must execute even inside the band
    class CloseLater(Fixed):
        def generate_targets(self, data):
            t = super().generate_targets(data)
            t.perp.iloc[3:] = 0.0
            return t
    res = run_backtest(md, CloseLater(perp=-1.0, hedge=0.0), EngineConfig(rebalance_band=0.5, **base))
    assert res.positions_perp.iloc[3, 0] == 0.0 and res.trades.iloc[0]["bars"] == 3


def test_long_perp_pays_positive_funding():
    md = panel(perp=[100, 100, 100], funding=[0.0, 0.01, 0.01])
    cfg = EngineConfig(initial_equity=1000.0, perp_fee=0.0, hedge_fee=0.0, slippage_bps=0.0, risk=NO_RISK)
    res = run_backtest(md, Fixed(perp=1.0, hedge=0.0), cfg)
    assert res.equity.iloc[-1] == pytest.approx(1000.0 * 0.99 * 0.99)
    assert res.metrics["funding_pnl"] < 0


def test_pnl_components_reconcile_with_equity_and_trades():
    md = make_synthetic_market_data(("BTC", "ETH"), n_bars=600, bar_hours=8, seed=3)
    cfg = EngineConfig(risk=RiskConfig(max_drawdown=0.2, stop_loss=0.03, stop_loss_cooldown_bars=2))
    for name in ("funding_carry", "funding_contrarian", "cross_sectional_carry", "buy_and_hold"):
        res = run_backtest(md, make_strategy(name), cfg)
        assert res.equity.iloc[-1] - cfg.initial_equity == pytest.approx(res.pnl["total"].sum(), abs=1e-6)
        assert res.trades["pnl"].sum() == pytest.approx(res.pnl["total"].sum(), abs=1e-6)
        assert (res.positions_perp.abs().sum(axis=1) + res.positions_hedge.abs().sum(axis=1)).max() <= cfg.risk.max_gross_leverage + 1e-9


def test_no_lookahead_future_changes_do_not_alter_past_positions():
    md = make_synthetic_market_data(("BTC", "ETH", "SOL"), n_bars=800, bar_hours=8, seed=11)
    cut = 600
    altered = MarketData(md.funding.copy(), md.perp_close.copy(), md.index_close.copy(), md.bar_hours, "test", True)
    altered.funding.iloc[cut:] *= -3.0
    altered.perp_close.iloc[cut:] *= 0.5
    altered.index_close.iloc[cut:] *= 0.5
    cfg = EngineConfig(risk=RiskConfig(max_drawdown=None))
    for name in ("funding_carry", "funding_contrarian", "funding_momentum", "cross_sectional_carry"):
        a = run_backtest(md, make_strategy(name), cfg)
        b = run_backtest(altered, make_strategy(name), cfg)
        pd.testing.assert_frame_equal(a.positions_perp.iloc[:cut], b.positions_perp.iloc[:cut])
        pd.testing.assert_series_equal(a.equity.iloc[:cut], b.equity.iloc[:cut])


def test_untradable_bars_force_flat_and_bankruptcy_is_handled():
    perp = [100.0, np.nan, 100.0, 1.0, 1.0]
    md = panel(perp=perp, funding=[0.0] * 5)
    cfg = EngineConfig(initial_equity=100.0, perp_fee=0.0, hedge_fee=0.0, slippage_bps=0.0, risk=NO_RISK)
    res = run_backtest(md, Fixed(perp=5.0, hedge=0.0), cfg)
    assert res.positions_perp.iloc[1, 0] == 0.0  # NaN price -> no position
    assert res.equity.iloc[-1] == 0.0  # 5x long into a 99% crash wipes the account
    assert any(e["type"] == "bankrupt" for e in res.events)


def test_requires_two_bars():
    md = panel(perp=[100.0], funding=[0.0])
    with pytest.raises(ValueError):
        run_backtest(md, Fixed())
