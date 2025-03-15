"""Compare many strategy configurations on the same panel.

Runs a parameter grid for every strategy family plus the benchmarks, prints
the ranked table, checks the leaders on an in-sample / out-of-sample split
and plots the equity curves of the leaders against buy-and-hold.

    python scripts/compare_strategies.py --exchange hyperliquid --symbols BTC ETH SOL --interval 4h --start 2024-07-20
    python scripts/compare_strategies.py --exchange synthetic        # offline demo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import RESULTS, add_data_args, add_engine_args, build_engine_config, load_panel  # noqa: E402

from perplexify.backtest import StrategySpec, compare_strategies, param_grid, walk_forward  # noqa: E402

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


def default_grid(multi_asset: bool) -> list[StrategySpec]:
    specs: list[StrategySpec] = []
    specs += param_grid("funding_carry", {"entry_apr": [0.05, 0.08, 0.12, 0.20], "exit_apr": [0.0, 0.03], "lookback_bars": [6, 24]})
    specs += param_grid("funding_carry", {"entry_apr": [0.08, 0.12], "exit_apr": [0.03]}, fixed={"allow_negative_carry": True})
    if multi_asset:
        specs += param_grid("cross_sectional_carry", {"top_n": [1, 2, 3], "min_apr": [0.05, 0.10], "rebalance_bars": [6, 24]})
    specs += param_grid("funding_contrarian", {"z_entry": [1.5, 2.0, 2.5], "z_exit": [0.5], "hedge_ratio": [0.0, 0.5]})
    specs += param_grid("funding_momentum", {"entry_apr": [0.20, 0.40], "exit_apr": [0.05]})
    specs += [
        StrategySpec(name="always_carry", label="always_carry"),
        StrategySpec(name="buy_and_hold", label="buy_and_hold_spot"),
        StrategySpec(name="buy_and_hold", params={"leg": "perp"}, label="buy_and_hold_perp"),
    ]
    return specs


def plot(results: dict, labels: list[str], path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 6))
    for label in labels:
        res = results.get(label)
        if res is None:
            continue
        ax.plot(res.equity.index, res.equity / res.equity.iloc[0], lw=1.2, label=label)
    ax.set_ylabel("equity (x initial)")
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    print(f"saved {path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_data_args(p)
    add_engine_args(p)
    p.add_argument("--sort-by", default="sharpe")
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--split", type=float, default=0.6, help="in-sample fraction for the walk-forward check")
    p.add_argument("--no-plot", action="store_true")
    args = p.parse_args()

    md = load_panel(args)
    cfg = build_engine_config(args)
    print(f"{md.exchange}: {len(md)} bars x {md.symbols}, {md.start.date()} -> {md.end.date()}, bar {md.bar_hours}h")
    print(md.summary().to_string(index=False), "\n")

    specs = default_grid(multi_asset=len(md.symbols) > 1)
    table, results = compare_strategies(md, specs, cfg, sort_by=args.sort_by)
    show = ["label", "annualized_return", "max_drawdown", "sharpe", "calmar", "funding_yield_annualized", "fee_drag_annualized", "time_in_market", "n_trades"]
    print(f"{len(specs)} configurations ranked by {args.sort_by}:\n")
    print(table[show].head(args.top).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    benchmarks = table[table["strategy"].isin(["buy_and_hold", "always_carry"])]
    print("\nbenchmarks:\n", benchmarks[show].to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"comparison_{md.exchange}_{args.interval}.csv"
    table.to_csv(out, index=False)
    print(f"\nsaved {out}")

    leaders = [l for l in table["label"].head(5) if l in results]
    rows = []
    for label in leaders:
        spec = next(s for s in specs if s.resolved_label() == label)
        wf = walk_forward(md, spec, args.split, cfg)
        for part, m in wf.items():
            rows.append({"label": label, "sample": part, "start": m["start"].date(), "end": m["end"].date(),
                         "annualized_return": m["annualized_return"], "max_drawdown": m["max_drawdown"], "sharpe": m["sharpe"]})
    wf_table = pd.DataFrame(rows)
    print(f"\nwalk-forward check (first {args.split:.0%} in-sample, rest out-of-sample) for the top {len(leaders)}:\n")
    print(wf_table.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    wf_table.to_csv(RESULTS / f"walkforward_{md.exchange}_{args.interval}.csv", index=False)

    if not args.no_plot:
        plot(results, leaders + ["buy_and_hold_spot", "always_carry"], RESULTS / f"comparison_{md.exchange}_{args.interval}.png",
             f"{md.exchange} {md.symbols} {args.interval}: top configurations vs benchmarks")

    write_markdown(md, cfg, table, benchmarks, wf_table, show, args, RESULTS / f"comparison_{md.exchange}_{args.interval}.md")


def write_markdown(md, cfg, table, benchmarks, wf_table, show, args, path: Path) -> None:
    pct_cols = {"annualized_return", "max_drawdown", "funding_yield_annualized", "fee_drag_annualized", "time_in_market"}

    def fmt(col, v):
        if isinstance(v, float):
            return f"{v * 100:.1f}%" if col in pct_cols else f"{v:.2f}"
        return str(v)

    def md_table(df, cols):
        nl = chr(10)
        head = "| " + " | ".join(cols) + " |" + nl + "|" + "|".join(["---"] * len(cols)) + "|" + nl
        rows = ["| " + " | ".join(fmt(c, r[c]) for c in cols) + " |" for r in df[cols].to_dict("records")]
        return head + nl.join(rows) + nl

    risk = cfg.risk
    lines = [
        f"# {md.exchange} {args.interval} comparison",
        "",
        f"Panel: **{', '.join(md.symbols)}**, {len(md)} bars of {md.bar_hours:g}h, {md.start.date()} to {md.end.date()}.",
        f"Costs: perp fee {cfg.perp_fee * 1e4:.1f} bp, hedge fee {cfg.hedge_fee * 1e4:.1f} bp, slippage {cfg.slippage_bps:g} bp per leg. "
        f"Risk: gross leverage <= {risk.max_gross_leverage:g}x, per-leg <= {risk.max_position_weight:g}x, "
        f"drawdown kill switch {risk.max_drawdown * 100 if risk.max_drawdown else 0:.0f}%"
        + (f", stop loss {risk.stop_loss * 100:.0f}%" if risk.stop_loss else "") + ".",
        "",
        f"## Top {args.top} of {len(table)} configurations (by {args.sort_by})",
        "",
        md_table(table.head(args.top), show),
        "## Benchmarks",
        "",
        md_table(benchmarks, show),
        f"## Walk-forward (first {args.split:.0%} in-sample, rest out-of-sample)",
        "",
        md_table(wf_table, list(wf_table.columns)) if len(wf_table) else "_n/a_",
        "",
        f"![equity curves](comparison_{md.exchange}_{args.interval}.png)",
        "",
    ]
    path.write_text(chr(10).join(lines), encoding="utf-8")
    print(f"saved {path}")


if __name__ == "__main__":
    main()
