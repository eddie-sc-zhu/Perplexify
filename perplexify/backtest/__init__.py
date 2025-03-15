"""Backtesting: engine, risk constraints, metrics and multi-config comparison."""

from perplexify.backtest.compare import StrategySpec, compare_strategies, param_grid, walk_forward  # noqa: F401
from perplexify.backtest.engine import BacktestResult, EngineConfig, run_backtest  # noqa: F401
from perplexify.backtest.metrics import KEY_METRICS, compute_metrics  # noqa: F401
from perplexify.backtest.risk import RiskConfig, RiskManager  # noqa: F401
