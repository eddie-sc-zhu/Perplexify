"""Event-loop backtester with funding payments, fees, hedge leg and risk limits.

Accounting per bar (t-1 -> t), for every symbol i with weights held over the bar
(fractions of equity, signed):

    price_pnl_i   = E * (w_perp_i * r_perp_i + w_hedge_i * r_index_i)
    funding_pnl_i = -E * w_perp_i * funding_i      (shorts receive positive funding)
    borrow_i      =  E * min(w_hedge_i, 0) * borrow_rate_per_bar
    fees_i        =  E * (|dw_perp_i| * (perp_fee + slip) + |dw_hedge_i| * (hedge_fee + slip))

Targets produced by the strategy at row t use only data up to t and are held
over (t, t+1]; the risk manager can shrink, block or flatten them first.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from perplexify.backtest.metrics import compute_metrics
from perplexify.backtest.risk import RiskConfig, RiskManager
from perplexify.data.dataset import MarketData
from perplexify.strategies.base import Strategy
from perplexify.utils import HOURS_PER_YEAR

_EPS = 1e-12


class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_equity: float = Field(100_000.0, gt=0)
    perp_fee: float = Field(0.00035, ge=0, description="Taker fee on the perp leg, fraction of traded notional")
    hedge_fee: float = Field(0.0005, ge=0, description="Taker fee on the spot/hedge leg")
    slippage_bps: float = Field(1.0, ge=0, description="Extra cost per leg in basis points of traded notional")
    hedge_borrow_apr: float = Field(0.0, ge=0, description="Annual cost of holding a short hedge (borrowed spot)")
    rebalance_band: float = Field(
        0.0, ge=0,
        description="Skip re-trimming an open position while both legs are within this weight distance of target "
                    "(0 = rebalance to the exact target every bar). Opening and closing always execute.",
    )
    risk: RiskConfig = Field(default_factory=RiskConfig)


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    config: EngineConfig
    equity: pd.Series
    returns: pd.Series
    positions_perp: pd.DataFrame
    positions_hedge: pd.DataFrame
    pnl: pd.DataFrame
    symbol_pnl: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict
    events: list[dict]

    def summary(self) -> pd.Series:
        return pd.Series(self.metrics)

    def drawdown(self) -> pd.Series:
        return self.equity / self.equity.cummax() - 1.0


class _TradeTracker:
    """Opens/closes per-symbol trades and attributes P&L and fees to them."""

    def __init__(self, symbols: list[str], index: pd.DatetimeIndex) -> None:
        self.symbols = symbols
        self.index = index
        n = len(symbols)
        self.open = np.zeros(n, dtype=bool)
        self.entry_bar = np.zeros(n, dtype=int)
        self.entry_equity = np.ones(n)
        self.pnl = np.zeros(n)
        self.funding = np.zeros(n)
        self.price = np.zeros(n)
        self.fees = np.zeros(n)
        self.entry_perp = np.zeros(n)
        self.entry_hedge = np.zeros(n)
        self.records: list[dict] = []

    def accrue(self, price: np.ndarray, funding: np.ndarray, borrow: np.ndarray) -> None:
        self.pnl += np.where(self.open, price + funding + borrow, 0.0)
        self.price += np.where(self.open, price + borrow, 0.0)
        self.funding += np.where(self.open, funding, 0.0)

    def pnl_fraction(self) -> np.ndarray:
        return np.where(self.open, self.pnl / self.entry_equity, 0.0)

    def bars_held(self, t: int) -> np.ndarray:
        return np.where(self.open, t - self.entry_bar, 0)

    @staticmethod
    def _direction(p: float, h: float) -> str:
        if abs(p) > _EPS and abs(h) > _EPS:
            return "short_perp_long_hedge" if p < 0 else "long_perp_short_hedge"
        if abs(p) > _EPS:
            return "short_perp" if p < 0 else "long_perp"
        return "long_hedge" if h > 0 else "short_hedge"

    def _close(self, i: int, t: int, fee: float, still_open: bool = False) -> None:
        self.pnl[i] -= fee
        self.fees[i] += fee
        self.records.append(
            {
                "symbol": self.symbols[i],
                "direction": self._direction(self.entry_perp[i], self.entry_hedge[i]),
                "entry_time": self.index[self.entry_bar[i]],
                "exit_time": self.index[t],
                "bars": int(t - self.entry_bar[i]),
                "entry_perp_weight": float(self.entry_perp[i]),
                "entry_hedge_weight": float(self.entry_hedge[i]),
                "pnl": float(self.pnl[i]),
                "return": float(self.pnl[i] / self.entry_equity[i]),
                "funding_pnl": float(self.funding[i]),
                "price_pnl": float(self.price[i]),
                "fees": float(self.fees[i]),
                "open_at_end": still_open,
            }
        )
        self.open[i] = False

    def _open(self, i: int, t: int, p: float, h: float, fee: float, equity: float) -> None:
        self.open[i] = True
        self.entry_bar[i] = t
        self.entry_equity[i] = equity
        self.pnl[i] = -fee
        self.fees[i] = fee
        self.funding[i] = 0.0
        self.price[i] = 0.0
        self.entry_perp[i] = p
        self.entry_hedge[i] = h

    def rebalance(self, t: int, old_p: np.ndarray, old_h: np.ndarray, new_p: np.ndarray, new_h: np.ndarray,
                  fee: np.ndarray, equity: float) -> None:
        for i in range(len(self.symbols)):
            was = abs(old_p[i]) > _EPS or abs(old_h[i]) > _EPS
            now = abs(new_p[i]) > _EPS or abs(new_h[i]) > _EPS
            if not was and not now:
                continue
            flip = was and now and (
                (abs(old_p[i]) > _EPS and abs(new_p[i]) > _EPS and np.sign(old_p[i]) != np.sign(new_p[i]))
                or (abs(old_p[i]) <= _EPS and abs(old_h[i]) > _EPS and abs(new_h[i]) > _EPS and np.sign(old_h[i]) != np.sign(new_h[i]))
            )
            if was and not now:
                self._close(i, t, fee[i])
            elif not was and now:
                self._open(i, t, new_p[i], new_h[i], fee[i], equity)
            elif flip:
                old_sz, new_sz = abs(old_p[i]) + abs(old_h[i]), abs(new_p[i]) + abs(new_h[i])
                close_fee = fee[i] * old_sz / (old_sz + new_sz)
                self._close(i, t, close_fee)
                self._open(i, t, new_p[i], new_h[i], fee[i] - close_fee, equity)
            else:  # resize
                self.pnl[i] -= fee[i]
                self.fees[i] += fee[i]

    def close_all(self, t: int) -> None:
        for i in np.flatnonzero(self.open):
            self._close(i, t, 0.0, still_open=True)

    def frame(self) -> pd.DataFrame:
        cols = ["symbol", "direction", "entry_time", "exit_time", "bars", "entry_perp_weight", "entry_hedge_weight",
                "pnl", "return", "funding_pnl", "price_pnl", "fees", "open_at_end"]
        return pd.DataFrame(self.records, columns=cols)


def run_backtest(data: MarketData, strategy: Strategy, config: EngineConfig | None = None) -> BacktestResult:
    cfg = config or EngineConfig()
    symbols = data.symbols
    index = data.index
    T, N = len(index), len(symbols)
    if T < 2:
        raise ValueError("Need at least two bars to run a backtest")

    perp = data.perp_close.to_numpy(dtype=float)
    idx_px = data.index_close.to_numpy(dtype=float)
    fund = np.nan_to_num(data.funding.to_numpy(dtype=float))

    targets = strategy.generate_targets(data)
    tp = targets.perp.reindex(index=index, columns=symbols).fillna(0.0).to_numpy(dtype=float)
    th = targets.hedge.reindex(index=index, columns=symbols).fillna(0.0).to_numpy(dtype=float)

    tradable = np.isfinite(perp) & np.isfinite(idx_px)
    r_perp = np.zeros((T, N))
    r_idx = np.zeros((T, N))
    with np.errstate(invalid="ignore", divide="ignore"):
        r_perp[1:] = perp[1:] / perp[:-1] - 1.0
        r_idx[1:] = idx_px[1:] / idx_px[:-1] - 1.0
    r_perp = np.nan_to_num(r_perp)
    r_idx = np.nan_to_num(r_idx)

    fee_p = cfg.perp_fee + cfg.slippage_bps / 1e4
    fee_h = cfg.hedge_fee + cfg.slippage_bps / 1e4
    borrow_bar = cfg.hedge_borrow_apr * data.bar_hours / HOURS_PER_YEAR

    equity = np.zeros(T)
    price_pnl = np.zeros((T, N))
    funding_pnl = np.zeros((T, N))
    borrow_pnl = np.zeros((T, N))
    fees = np.zeros((T, N))
    pos_p = np.zeros((T, N))
    pos_h = np.zeros((T, N))
    events: list[dict] = []

    E = float(cfg.initial_equity)
    w_p = np.zeros(N)
    w_h = np.zeros(N)
    risk = RiskManager(cfg.risk, N)
    tracker = _TradeTracker(symbols, index)
    bankrupt_at: int | None = None

    for t in range(T):
        if t > 0 and bankrupt_at is None:
            E_prev = E
            pp = E_prev * (w_p * r_perp[t] + w_h * r_idx[t])
            fp = -E_prev * w_p * fund[t]
            bp = E_prev * np.minimum(w_h, 0.0) * borrow_bar
            E = E_prev + float(pp.sum() + fp.sum() + bp.sum())
            price_pnl[t], funding_pnl[t], borrow_pnl[t] = pp, fp, bp
            if E <= 0:
                bankrupt_at = t
                E = 0.0
                tracker.close_all(t)
                events.append({"bar": t, "type": "bankrupt"})
            else:
                # weights drift with prices between rebalances
                w_p = w_p * (1.0 + r_perp[t]) * E_prev / E
                w_h = w_h * (1.0 + r_idx[t]) * E_prev / E
                tracker.accrue(pp, fp, bp)

        if bankrupt_at is not None:
            equity[t] = 0.0
            continue

        tgt_p = np.where(tradable[t], tp[t], 0.0)
        tgt_h = np.where(tradable[t], th[t], 0.0)
        tgt_p, tgt_h, ev = risk.apply(t, tgt_p, tgt_h, w_p, w_h, E, tracker.pnl_fraction(), tracker.bars_held(t))
        for e in ev:
            e["time"] = index[t]
            if "symbol_index" in e:
                e["symbol"] = symbols[e.pop("symbol_index")]
        events.extend(ev)

        if cfg.rebalance_band > 0:
            # hold drifted weights instead of paying to re-trim small deviations
            was_open = (np.abs(w_p) > _EPS) | (np.abs(w_h) > _EPS)
            stays_open = (np.abs(tgt_p) > _EPS) | (np.abs(tgt_h) > _EPS)
            same_side = (np.sign(tgt_p) == np.sign(w_p)) & (np.sign(tgt_h) == np.sign(w_h))
            within = (np.abs(tgt_p - w_p) < cfg.rebalance_band) & (np.abs(tgt_h - w_h) < cfg.rebalance_band)
            hold = was_open & stays_open & same_side & within
            tgt_p = np.where(hold, w_p, tgt_p)
            tgt_h = np.where(hold, w_h, tgt_h)

        fee_vec = E * (np.abs(tgt_p - w_p) * fee_p + np.abs(tgt_h - w_h) * fee_h)
        E -= float(fee_vec.sum())
        fees[t] = -fee_vec
        tracker.rebalance(t, w_p, w_h, tgt_p, tgt_h, fee_vec, E)
        w_p, w_h = tgt_p.copy(), tgt_h.copy()
        equity[t] = E
        pos_p[t], pos_h[t] = w_p, w_h

    tracker.close_all(T - 1)

    eq = pd.Series(equity, index=index, name="equity")
    pnl = pd.DataFrame(
        {
            "price": price_pnl.sum(axis=1),
            "funding": funding_pnl.sum(axis=1),
            "fees": fees.sum(axis=1),
            "borrow": borrow_pnl.sum(axis=1),
        },
        index=index,
    )
    pnl["total"] = pnl.sum(axis=1)
    symbol_pnl = pd.DataFrame(price_pnl + funding_pnl + fees + borrow_pnl, index=index, columns=symbols)
    positions_perp = pd.DataFrame(pos_p, index=index, columns=symbols)
    positions_hedge = pd.DataFrame(pos_h, index=index, columns=symbols)
    trades = tracker.frame()
    metrics = compute_metrics(eq, data.bar_hours, pnl, trades, positions_perp, positions_hedge)
    metrics["kill_switch_triggers"] = sum(1 for e in events if e["type"] == "kill_switch")
    metrics["stop_loss_triggers"] = sum(1 for e in events if e["type"] == "stop_loss")

    return BacktestResult(
        strategy=strategy.name,
        params=strategy.params.model_dump(),
        config=cfg,
        equity=eq,
        returns=eq.pct_change().fillna(0.0),
        positions_perp=positions_perp,
        positions_hedge=positions_hedge,
        pnl=pnl,
        symbol_pnl=symbol_pnl,
        trades=trades,
        metrics=metrics,
        events=events,
    )
