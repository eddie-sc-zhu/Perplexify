# Perplexify

Crypto perpetual-futures **funding-rate trading system**: Python, FastAPI, Pandas, NumPy.

Perpetual swaps never expire, so exchanges keep the perp price pinned to spot with a
periodic *funding payment* between longs and shorts. When the market is crowded long,
longs pay shorts a positive rate every settlement; when it is crowded short the sign
flips. Perplexify finds those payments, harvests them with delta-neutral and
directional strategies, backtests every configuration on real exchange history with
funding, fees and risk limits modelled, and serves everything over an HTTP API with a
paper-trading bot on top.

```
                      +-----------------------+
   Hyperliquid  ----> |  data providers       |      +-------------------+
   dYdX v4      ----> |  (one interface)      | ---> | parquet cache     |
   OKX          ----> +-----------------------+      +-------------------+
                                 |                             |
                                 v                             v
                      +-----------------------+      +-------------------+
                      | MarketData panel      | ---> | strategies        |
                      | funding / perp / index|      | targets per bar   |
                      +-----------------------+      +-------------------+
                                 |                             |
                                 v                             v
   FastAPI  <-------- +-----------------------+ <--- +-------------------+
   paper bot <------- | backtest engine       |      | risk manager      |
   CLI scripts <----- | fees, funding, trades |      | dd kill switch,   |
                      | metrics, comparison   |      | stops, leverage   |
                      +-----------------------+      +-------------------+
```

## What is in the box

| Area | Module | Highlights |
| --- | --- | --- |
| Data | `perplexify/data/providers/` | Hyperliquid, dYdX v4 and OKX public APIs behind one `FundingProvider` interface: funding history, candles, index prices, live market snapshots. Hyperliquid also exposes the cross-venue `predictedFundings` feed (Binance / Bybit / Hyperliquid). |
| Data | `perplexify/data/storage.py`, `dataset.py` | Parquet cache with incremental refresh; `MarketData` panel (funding paid per bar, perp close, index close) built at any bar size with right-closed bins so nothing leaks from the future. |
| Strategies | `perplexify/strategies/` | `funding_carry` (delta-neutral short perp / long index), `cross_sectional_carry` (rotate into the top-N richest funding), `funding_contrarian` (fade z-score extremes), `funding_momentum`, plus `buy_and_hold` and `always_carry` benchmarks. All parameters are validated Pydantic models. |
| Backtest | `perplexify/backtest/engine.py` | Vectorised features + per-bar accounting loop: price P&L on both legs, funding received or paid, taker fees and slippage on every rebalance, borrow cost on short hedges, trade ledger with per-trade attribution. |
| Risk | `perplexify/backtest/risk.py` | Gross-leverage cap, per-symbol cap, concentration cap, portfolio drawdown kill switch with cooldown, per-position stop loss with block-out, minimum holding period. |
| Metrics | `perplexify/backtest/metrics.py` | Annualised return (calendar time), vol, Sharpe, Sortino, max drawdown and duration, Calmar, funding yield vs fee drag, trade stats. |
| Comparison | `perplexify/backtest/compare.py` | `param_grid` + `compare_strategies` rank dozens of configurations; `walk_forward` re-runs a config on in-sample / out-of-sample halves. |
| API | `perplexify/api/` | FastAPI service: live funding, opportunity scanners (single venue and cross-venue), backtest and comparison endpoints, bot control. OpenAPI docs at `/docs`. |
| Bot | `perplexify/bot/paper_trader.py` | Paper trader that re-evaluates the strategy on a schedule against live funding and prices, accrues real settlements, pays fees, persists its ledger. No orders are ever sent. |

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows; use source .venv/bin/activate elsewhere
pip install -e ".[dev]"
pytest                                              # offline unit + API tests

# offline demo on synthetic data
python scripts/compare_strategies.py --exchange synthetic --symbols BTC ETH SOL --interval 8h

# real data (dYdX serves unlimited hourly history; Hyperliquid keeps the latest 5000 candles per bar size)
python scripts/fetch_data.py --exchange dydx --symbols BTC ETH SOL DOGE AVAX LINK --interval 1h --start 2024-01-01
python scripts/compare_strategies.py --exchange dydx --symbols BTC ETH SOL DOGE AVAX LINK --interval 4h --start 2024-01-01
python scripts/run_backtest.py --exchange dydx --symbols BTC ETH SOL --interval 4h --start 2024-01-01 \
    --strategy funding_carry --param entry_apr=0.08 --param exit_apr=0.03 --max-drawdown 0.10 --plot

# API
uvicorn perplexify.api.app:app --reload
curl "localhost:8000/opportunities/hyperliquid?min_apr=0.2"
curl "localhost:8000/opportunities/cross-venue/scan?min_spread_apr=0.05"
curl -X POST localhost:8000/backtest -H "content-type: application/json" -d '{
  "exchange": "dydx", "symbols": ["BTC","ETH"], "interval": "4h", "start": "2024-01-01",
  "strategy": {"name": "funding_carry", "params": {"entry_apr": 0.08, "exit_apr": 0.03}},
  "engine": {"risk": {"max_drawdown": 0.10}} }'

# paper trading
python scripts/run_bot.py --exchange dydx --symbols BTC ETH --interval 1h --strategy funding_carry --once
curl -X POST localhost:8000/bot/start -H "content-type: application/json" -d '{"exchange":"dydx","symbols":["BTC","ETH"]}'
```

Exchanges that geo-block your region simply fail to connect; every script accepts
`--exchange synthetic` and the API accepts `"exchange": "synthetic"` for offline work.

## How the backtest works

One row per bar close `t`. A strategy sees everything up to and including `t` and
emits two signed weights per symbol, `perp` and `hedge` (fractions of equity). The
engine holds them over `(t, t+1]` and at `t+1` books

```
price_pnl   = E * (w_perp * r_perp + w_hedge * r_index)
funding_pnl = -E * w_perp * funding_paid_in_bar       # short perp receives positive funding
borrow      =  E * min(w_hedge, 0) * borrow_rate       # short hedge borrows spot
fees        =  E * (|dw_perp| * (perp_fee + slip) + |dw_hedge| * (hedge_fee + slip))
```

Funding paid in a bar is the sum of every settlement in `(t, t+1]` (hourly venues are
aggregated, 8-hour venues map one-to-one). The index leg uses the venue's oracle
price (dYdX), the premium-implied oracle (Hyperliquid) or index candles (OKX), so the
delta-neutral trade carries real basis risk rather than being free money.

Before execution every target passes through the `RiskManager`: drawdown kill switch,
stop losses, holding-period rules, then the per-symbol, concentration and gross
leverage caps. Default caps are 1.0x per leg and 2.0x gross, i.e. the perp notional
and the spot hedge each equal the account's equity.

Defaults: perp taker fee 3.5 bp, hedge fee 5 bp, 1 bp slippage per leg, no borrow
cost. Change them on the `EngineConfig` or the CLI flags.

## Strategies

| Name | Idea | Key parameters |
| --- | --- | --- |
| `funding_carry` | Short perp + long index while smoothed annualised funding is above `entry_apr`, exit below `exit_apr`. Price risk cancels; income is funding minus fees and basis drift. | `lookback_bars`, `entry_apr`, `exit_apr`, `leverage`, `allow_negative_carry` |
| `cross_sectional_carry` | Every `rebalance_bars`, hold the `top_n` coins by smoothed funding above `min_apr`, equal weight, delta-neutral. | `top_n`, `min_apr`, `rebalance_bars`, `leverage` |
| `funding_contrarian` | Rolling z-score of funding; short the perp when the crowd is extremely long (collect funding, bet on reversion), long when extremely short. Optional partial hedge. | `z_window`, `z_entry`, `z_exit`, `leverage`, `hedge_ratio` |
| `funding_momentum` | Go with persistent funding (long when strongly positive). Included as a foil. | `lookback_bars`, `entry_apr`, `exit_apr` |
| `always_carry` / `buy_and_hold` | Benchmarks: permanent carry, equal-weight spot or perp long. | `leverage`, `leg` |

## Results

Real dYdX v4 history, six markets (BTC, ETH, SOL, DOGE, AVAX, LINK), 2024-01-01 to
2026-10-27, taker fees and slippage on every rebalance, 2x gross leverage cap, 15%
drawdown kill switch:

| Strategy | Bars | Annualised return | Max drawdown | Sharpe | Funding yield p.a. | Fee drag p.a. |
| --- | --- | --- | --- | --- | --- | --- |
| `funding_carry` (entry 8%, exit 3%) | 8h | **9.8%** | **-5.9%** | 0.77 | 15.4% | 6.1% |
| `funding_carry` (entry 20%, exit 3%) | 4h | 7.9% | -6.3% | 0.60 | 14.3% | 4.7% |
| `always_carry` benchmark | 8h | 7.0% | -4.4% | 1.09 | 10.2% | 2.2% |
| buy and hold spot | 8h | -3.8% | -16.6% | -0.34 | 0 | 0 |

In-sample (2024 to Sep 2025) the carry made 14.7% a year with a 0.8% drawdown; out-of-sample
(Sep 2025 to Oct 2026) it made 2.7% with a 5.9% drawdown as funding dried up. Full tables,
walk-forward, the rebalance-band sensitivity and the data-quality notes are in
[RESULTS.md](RESULTS.md).

## Layout

```
perplexify/
  config.py            settings (PERPLEXIFY_* env vars)
  utils.py             interval / time helpers
  data/                providers, cache, dataset builder, synthetic generator
  strategies/          base + registry, features, strategy implementations
  backtest/            engine, risk, metrics, comparison
  api/                 FastAPI app, routes, schemas, data service
  bot/                 paper trader
scripts/               fetch_data, run_backtest, compare_strategies, run_bot
tests/                 pytest suite (offline)
```

## Limits and honest caveats

* Simulated fills at the bar close with a flat fee + slippage model; no order book,
  no partial fills, no liquidation engine beyond the equity floor.
* Spot-hedge availability is assumed; shorting spot (negative carry) is only modelled
  through `hedge_borrow_apr`.
* Hyperliquid serves only the latest 5000 candles per bar size and OKX only ~3 months
  of funding history; dYdX is the deepest free source.
* Past funding is not future funding. The walk-forward table exists for a reason.
