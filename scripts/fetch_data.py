"""Download (or refresh) funding + price history and save a backtest panel.

    python scripts/fetch_data.py --exchange hyperliquid --symbols BTC ETH SOL --interval 4h --start 2024-07-20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import RESULTS, add_data_args, load_panel  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_data_args(p)
    p.add_argument("--out", default=None, help="Output parquet (default results/panel_<exchange>_<interval>.parquet)")
    args = p.parse_args()
    md = load_panel(args)
    out = Path(args.out or RESULTS / f"panel_{md.exchange}_{args.interval}.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    df = md.to_long()
    df["exchange"] = md.exchange
    df.to_parquet(out, index=False)
    print(f"{md.exchange}: {len(md)} bars x {len(md.symbols)} symbols, {md.start} -> {md.end}")
    print(md.summary().to_string(index=False))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
