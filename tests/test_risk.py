import numpy as np
import pytest

from perplexify.backtest.risk import RiskConfig, RiskManager


def apply(rm, t, p, h, cur_p=None, cur_h=None, equity=100.0, pnl=None, held=None):
    n = rm.n
    p, h = np.asarray(p, float), np.asarray(h, float)
    cur_p = np.zeros(n) if cur_p is None else np.asarray(cur_p, float)
    cur_h = np.zeros(n) if cur_h is None else np.asarray(cur_h, float)
    pnl = np.zeros(n) if pnl is None else np.asarray(pnl, float)
    held = np.zeros(n, int) if held is None else np.asarray(held, int)
    return rm.apply(t, p, h, cur_p, cur_h, equity, pnl, held)


def test_gross_leverage_cap_scales_proportionally():
    rm = RiskManager(RiskConfig(max_gross_leverage=2.0, max_position_weight=5.0, max_drawdown=None), 2)
    p, h, _ = apply(rm, 0, [-2.0, -1.0], [2.0, 1.0])
    assert np.abs(p).sum() + np.abs(h).sum() == pytest.approx(2.0)
    assert p[0] / p[1] == pytest.approx(2.0)


def test_per_symbol_cap_clips():
    rm = RiskManager(RiskConfig(max_gross_leverage=10.0, max_position_weight=0.5, max_drawdown=None), 1)
    p, h, _ = apply(rm, 0, [-3.0], [3.0])
    assert p[0] == -0.5 and h[0] == 0.5


def test_kill_switch_flattens_and_optionally_resumes():
    rm = RiskManager(RiskConfig(max_drawdown=0.10, drawdown_cooldown_bars=2), 1)
    p, _, ev = apply(rm, 0, [-1.0], [1.0], equity=100.0)
    assert p[0] == -1.0 and not ev
    p, _, ev = apply(rm, 1, [-1.0], [1.0], equity=89.0)
    assert p[0] == 0.0 and ev[0]["type"] == "kill_switch"
    p, _, _ = apply(rm, 2, [-1.0], [1.0], equity=89.0)
    assert p[0] == 0.0  # still in cooldown
    p, _, ev = apply(rm, 3, [-1.0], [1.0], equity=89.0)
    assert p[0] == -1.0 and ev[0]["type"] == "resume"


def test_kill_switch_without_cooldown_halts_for_good():
    rm = RiskManager(RiskConfig(max_drawdown=0.05, drawdown_cooldown_bars=None), 1)
    apply(rm, 0, [1.0], [0.0], equity=100.0)
    apply(rm, 1, [1.0], [0.0], equity=90.0)
    p, _, _ = apply(rm, 500, [1.0], [0.0], equity=200.0)
    assert p[0] == 0.0


def test_stop_loss_closes_and_blocks_symbol():
    rm = RiskManager(RiskConfig(max_drawdown=None, stop_loss=0.02, stop_loss_cooldown_bars=2, max_gross_leverage=10.0), 2)
    p, h, ev = apply(rm, 5, [-1.0, -1.0], [1.0, 1.0], cur_p=[-1.0, -1.0], cur_h=[1.0, 1.0], pnl=[-0.03, 0.01], held=[3, 3])
    assert p[0] == 0.0 and h[0] == 0.0 and p[1] == -1.0
    assert ev[0]["type"] == "stop_loss" and ev[0]["symbol_index"] == 0
    p, _, _ = apply(rm, 7, [-1.0, -1.0], [1.0, 1.0])
    assert p[0] == 0.0  # blocked through the cooldown
    p, _, _ = apply(rm, 8, [-1.0, -1.0], [1.0, 1.0])
    assert p[0] == -1.0


def test_min_holding_keeps_young_positions():
    rm = RiskManager(RiskConfig(max_drawdown=None, min_holding_bars=3), 1)
    p, h, _ = apply(rm, 2, [0.0], [0.0], cur_p=[-0.8], cur_h=[0.8], held=[1])
    assert p[0] == -0.8 and h[0] == 0.8
    p, h, _ = apply(rm, 5, [0.0], [0.0], cur_p=[-0.8], cur_h=[0.8], held=[4])
    assert p[0] == 0.0 and h[0] == 0.0


def test_max_concurrent_keeps_largest():
    rm = RiskManager(RiskConfig(max_drawdown=None, max_concurrent_positions=2, max_gross_leverage=10), 3)
    p, h, _ = apply(rm, 0, [-0.5, -0.9, -0.7], [0.5, 0.9, 0.7])
    assert p[0] == 0.0 and p[1] == -0.9 and p[2] == -0.7
