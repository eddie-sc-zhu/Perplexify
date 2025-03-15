"""Shared CLI helpers."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from perplexify.data.dataset import MarketData, build_market_data
from perplexify.data.synthetic import make_synthetic_market_data
from perplexify.utils import interval_to_hours

RESULTS = Path("results")


def add_data_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--exchange", default="hyperliquid", help="hyperliquid | dydx | okx | synthetic")
    p.add_argument("--symbols", nargs="+", default=["BTC", "ETH", "SOL"])
    p.add_argument("--interval", default="4h", help="bar size: 1h 4h 8h 1d")
    p.add_argument("--start", default=None, help="UTC date, e.g. 2024-07-01")
    p.add_argument("--end", default=None)
    p.add_argument("--panel", default=None, help="Load a saved long-format panel parquet instead of fetching")
    p.add_argument("--refresh", action="store_true", help="Ignore the parquet cache")
    p.add_argument("-v", "--verbose", action="store_true")


def load_panel(args) -> MarketData:
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.panel:
        df = pd.read_parquet(args.panel)
        ts = pd.to_datetime(df["timestamp"].drop_duplicates().sort_values(), utc=True)
        native_hours = float(ts.diff().median() / pd.Timedelta(hours=1))
        md = MarketData.from_long(df, native_hours, exchange=str(df["exchange"].iloc[0]) if "exchange" in df else "panel")
        md = md.resample(interval_to_hours(args.interval))  # panel is stored at its native bar; coarsen on demand
        return md.slice(args.start, args.end)
    if args.exchange == "synthetic":
        hours = interval_to_hours(args.interval)
        return make_synthetic_market_data(tuple(args.symbols), n_bars=int(2 * 365 * 24 / hours), bar_hours=hours)
    return build_market_data(args.exchange, args.symbols, args.interval, args.start, args.end, refresh=args.refresh)


def parse_params(items: list[str] | None) -> dict:
    out: dict = {}
    for item in items or []:
        k, _, v = item.partition("=")
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = int(v) if v.lstrip("-").isdigit() else float(v)
            except ValueError:
                out[k] = v
    return out


def fmt_metrics(m: dict) -> str:
    pct = lambda x: f"{x * 100:6.2f}%"  # noqa: E731
    lines = [
        f"period              {m['start'].date()} -> {m['end'].date()}  ({m['days']} days, {m['bars']} bars)",
        f"annualized return   {pct(m['annualized_return'])}",
        f"total return        {pct(m['total_return'])}",
        f"max drawdown        {pct(m['max_drawdown'])}  (longest {m['max_drawdown_days']} days under water)",
        f"annualized vol      {pct(m['annualized_volatility'])}",
        f"sharpe / sortino    {m['sharpe']:.2f} / {m['sortino']:.2f}   calmar {m['calmar']:.2f}",
        f"funding yield p.a.  {pct(m['funding_yield_annualized'])}   fee drag p.a. {pct(m['fee_drag_annualized'])}",
        f"pnl split           funding {m['funding_pnl']:,.0f}  price {m['price_pnl']:,.0f}  fees {m['fees_paid']:,.0f}",
        f"trades              {m['n_trades']}  win rate {m['win_rate']:.2f}  avg held {m['avg_bars_held']:.1f} bars  time in market {m['time_in_market']:.2f}",
        f"risk events         kill switch {m['kill_switch_triggers']}  stop loss {m['stop_loss_triggers']}",
    ]
    return "\n".join(lines)


def add_engine_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("engine / risk")
    g.add_argument("--equity", type=float, default=100_000.0)
    g.add_argument("--perp-fee", type=float, default=0.00035, help="taker fee on the perp leg")
    g.add_argument("--hedge-fee", type=float, default=0.0005, help="taker fee on the spot/index leg")
    g.add_argument("--slippage-bps", type=float, default=1.0)
    g.add_argument("--rebalance-band", type=float, default=0.0, help="hold drifted weights within this band instead of re-trimming (0 = exact)")
    g.add_argument("--max-gross-leverage", type=float, default=2.0)
    g.add_argument("--max-position", type=float, default=1.0)
    g.add_argument("--max-drawdown", type=float, default=0.15, help="kill switch; 0 disables")
    g.add_argument("--drawdown-cooldown-bars", type=int, default=None)
    g.add_argument("--stop-loss", type=float, default=None, help="per-position stop as a fraction of entry equity")
    g.add_argument("--min-holding-bars", type=int, default=0)


def build_engine_config(args):
    from perplexify.backtest import EngineConfig, RiskConfig

    risk = RiskConfig(
        max_gross_leverage=args.max_gross_leverage,
        max_position_weight=args.max_position,
        max_drawdown=args.max_drawdown or None,
        drawdown_cooldown_bars=args.drawdown_cooldown_bars,
        stop_loss=args.stop_loss,
        min_holding_bars=args.min_holding_bars,
    )
    return EngineConfig(
        initial_equity=args.equity, perp_fee=args.perp_fee, hedge_fee=args.hedge_fee,
        slippage_bps=args.slippage_bps, rebalance_band=args.rebalance_band, risk=risk,
    )
