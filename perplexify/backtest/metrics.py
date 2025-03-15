"""Performance statistics for an equity curve plus its P&L attribution."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from perplexify.utils import HOURS_PER_YEAR


def _longest_run(mask: np.ndarray) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def compute_metrics(
    equity: pd.Series,
    bar_hours: float,
    pnl: pd.DataFrame,
    trades: pd.DataFrame,
    positions_perp: pd.DataFrame,
    positions_hedge: pd.DataFrame,
) -> dict:
    eq = equity.astype(float)
    initial = float(eq.iloc[0])
    final = float(eq.iloc[-1])
    rets = eq.pct_change().fillna(0.0)
    bpy = HOURS_PER_YEAR / bar_hours
    span_days = (eq.index[-1] - eq.index[0]).total_seconds() / 86400.0
    years = max(span_days / 365.25, 1e-9)

    total_return = final / initial - 1.0
    annualized = (final / initial) ** (1.0 / years) - 1.0 if final > 0 else -1.0
    std = float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
    vol = std * math.sqrt(bpy)
    sharpe = float(rets.mean()) / std * math.sqrt(bpy) if std > 0 else float("nan")
    downside = rets[rets < 0]
    dstd = float(np.sqrt((downside**2).mean())) if len(downside) else 0.0
    sortino = float(rets.mean()) / dstd * math.sqrt(bpy) if dstd > 0 else float("nan")

    dd = eq / eq.cummax() - 1.0
    max_dd = float(dd.min())
    dd_bars = _longest_run((dd < 0).to_numpy())
    calmar = annualized / abs(max_dd) if max_dd < 0 else float("nan")

    gross = positions_perp.abs().sum(axis=1) + positions_hedge.abs().sum(axis=1)
    in_market = gross > 1e-12

    funding_total = float(pnl["funding"].sum())
    price_total = float(pnl["price"].sum())
    fees_total = float(pnl["fees"].sum())
    borrow_total = float(pnl["borrow"].sum())

    n_trades = int(len(trades))
    if n_trades:
        wins = trades.loc[trades["pnl"] > 0, "pnl"].sum()
        losses = trades.loc[trades["pnl"] < 0, "pnl"].sum()
        win_rate = float((trades["pnl"] > 0).mean())
        profit_factor = float(wins / abs(losses)) if losses < 0 else float("inf") if wins > 0 else float("nan")
        avg_trade_return = float(trades["return"].mean())
        avg_bars_held = float(trades["bars"].mean())
    else:
        win_rate = profit_factor = avg_trade_return = avg_bars_held = float("nan")

    return {
        "start": eq.index[0],
        "end": eq.index[-1],
        "days": round(span_days, 1),
        "bars": int(len(eq)),
        "initial_equity": initial,
        "final_equity": final,
        "total_return": total_return,
        "annualized_return": annualized,
        "annualized_volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "max_drawdown_days": round(dd_bars * bar_hours / 24.0, 1),
        "calmar": calmar,
        "time_in_market": float(in_market.mean()),
        "avg_gross_leverage": float(gross[in_market].mean()) if in_market.any() else 0.0,
        "funding_pnl": funding_total,
        "price_pnl": price_total,
        "fees_paid": -fees_total,
        "borrow_cost": -borrow_total,
        "funding_yield_annualized": funding_total / initial / years,
        "fee_drag_annualized": -fees_total / initial / years,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_trade_return": avg_trade_return,
        "avg_bars_held": avg_bars_held,
    }


KEY_METRICS = [
    "annualized_return",
    "max_drawdown",
    "sharpe",
    "sortino",
    "calmar",
    "annualized_volatility",
    "total_return",
    "funding_yield_annualized",
    "fee_drag_annualized",
    "time_in_market",
    "n_trades",
    "win_rate",
]
