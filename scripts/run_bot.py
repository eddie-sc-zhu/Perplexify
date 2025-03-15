"""Run the paper-trading bot from the terminal (no API server needed).

    python scripts/run_bot.py --exchange hyperliquid --symbols BTC ETH --interval 1h \
        --strategy funding_carry --param entry_apr=0.08 --poll-seconds 300
    python scripts/run_bot.py --once          # single evaluation, then exit
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import add_engine_args, build_engine_config, parse_params  # noqa: E402

from perplexify.api.service import DataService  # noqa: E402
from perplexify.backtest import StrategySpec  # noqa: E402
from perplexify.bot import BotConfig, PaperTrader  # noqa: E402
from perplexify.strategies import REGISTRY  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--exchange", default="hyperliquid")
    p.add_argument("--symbols", nargs="+", default=["BTC", "ETH"])
    p.add_argument("--interval", default="1h")
    p.add_argument("--strategy", default="funding_carry", choices=sorted(REGISTRY))
    p.add_argument("--param", action="append")
    p.add_argument("--poll-seconds", type=int, default=300)
    p.add_argument("--history-days", type=int, default=30)
    p.add_argument("--once", action="store_true")
    p.add_argument("--fresh", action="store_true", help="ignore a saved state file")
    p.add_argument("-v", "--verbose", action="store_true")
    add_engine_args(p)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    cfg = BotConfig(
        exchange=args.exchange, symbols=args.symbols, interval=args.interval,
        strategy=StrategySpec(name=args.strategy, params=parse_params(args.param)),
        engine=build_engine_config(args), poll_seconds=args.poll_seconds, history_days=args.history_days,
    )
    bot = PaperTrader(cfg, DataService()) if args.fresh else PaperTrader.load(cfg, DataService())
    print(f"paper trading {cfg.strategy.name} on {cfg.exchange} {cfg.symbols} every {cfg.poll_seconds}s (state: {bot.state_path})")
    while True:
        row = bot.tick()
        print(json.dumps({k: row[k] for k in ("time", "equity", "funding_pnl", "price_pnl", "fees", "fills")}, default=str))
        if args.once:
            print(json.dumps(bot.status(), indent=2, default=str))
            break
        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
