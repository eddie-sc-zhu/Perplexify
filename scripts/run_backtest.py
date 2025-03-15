"""Backtest one strategy configuration and report / plot the result.

    python scripts/run_backtest.py --exchange hyperliquid --symbols BTC ETH SOL --interval 4h \
        --start 2024-07-20 --strategy funding_carry --param entry_apr=0.10 --param exit_apr=0.03 --plot

Use ``--exchange synthetic`` to run without network access.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import RESULTS, add_data_args, add_engine_args, build_engine_config, fmt_metrics, load_panel, parse_params  # noqa: E402

from perplexify.backtest import run_backtest  # noqa: E402
from perplexify.strategies import REGISTRY, make_strategy  # noqa: E402


def plot(res, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True, gridspec_kw={"height_ratios": [3, 1, 2]})
    axes[0].plot(res.equity.index, res.equity / res.equity.iloc[0], color="#1f77b4", lw=1.2, label=res.strategy)
    axes[0].set_ylabel("equity (x initial)")
    axes[0].legend(loc="upper left")
    axes[0].grid(alpha=0.3)
    dd = res.drawdown()
    axes[1].fill_between(dd.index, dd * 100, 0, color="#d62728", alpha=0.5)
    axes[1].set_ylabel("drawdown %")
    axes[1].grid(alpha=0.3)
    cum = res.pnl[["funding", "price", "fees"]].cumsum()
    for col, color in (("funding", "#2ca02c"), ("price", "#ff7f0e"), ("fees", "#7f7f7f")):
        axes[2].plot(cum.index, cum[col], label=f"cum. {col}", color=color, lw=1.1)
    axes[2].set_ylabel("P&L attribution")
    axes[2].legend(loc="upper left")
    axes[2].grid(alpha=0.3)
    m = res.metrics
    fig.suptitle(
        f"{res.strategy} {res.params}\nann. return {m['annualized_return']*100:.1f}%  max DD {m['max_drawdown']*100:.1f}%  "
        f"sharpe {m['sharpe']:.2f}  trades {m['n_trades']}",
        fontsize=10,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    print(f"saved {path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_data_args(p)
    add_engine_args(p)
    p.add_argument("--strategy", default="funding_carry", choices=sorted(REGISTRY))
    p.add_argument("--param", action="append", help="strategy parameter, e.g. --param entry_apr=0.1 (repeatable)")
    p.add_argument("--plot", action="store_true")
    p.add_argument("--tag", default="", help="suffix for output files")
    args = p.parse_args()

    md = load_panel(args)
    strategy = make_strategy(args.strategy, parse_params(args.param))
    res = run_backtest(md, strategy, build_engine_config(args))

    print(f"\n{md.exchange} {md.symbols} {args.interval} bars, strategy {strategy}\n")
    print(fmt_metrics(res.metrics))

    RESULTS.mkdir(exist_ok=True)
    stem = f"backtest_{args.strategy}_{md.exchange}{('_' + args.tag) if args.tag else ''}"
    curve = res.pnl.copy()
    curve.insert(0, "equity", res.equity)
    curve.insert(1, "drawdown", res.drawdown())
    curve.to_csv(RESULTS / f"{stem}_curve.csv")
    res.trades.to_csv(RESULTS / f"{stem}_trades.csv", index=False)
    print(f"\nsaved {RESULTS / (stem + '_curve.csv')} and {RESULTS / (stem + '_trades.csv')}")
    if res.events:
        print(f"risk events: {res.events[:5]}{' ...' if len(res.events) > 5 else ''}")
    if args.plot:
        plot(res, RESULTS / f"{stem}.png")


if __name__ == "__main__":
    main()
