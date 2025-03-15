"""Run many strategy configurations over the same panel and tabulate them."""

from __future__ import annotations

import itertools
import logging
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from perplexify.backtest.engine import BacktestResult, EngineConfig, run_backtest
from perplexify.backtest.metrics import KEY_METRICS
from perplexify.data.dataset import MarketData
from perplexify.strategies import make_strategy

log = logging.getLogger(__name__)


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    params: dict[str, Any] = Field(default_factory=dict)
    label: str | None = None

    def resolved_label(self) -> str:
        if self.label:
            return self.label
        if not self.params:
            return self.name
        inner = ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.name}[{inner}]"


def param_grid(name: str, grid: dict[str, list[Any]], fixed: dict[str, Any] | None = None) -> list[StrategySpec]:
    """Cartesian product of parameter lists -> one spec per combination."""
    keys = list(grid)
    specs = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = dict(fixed or {})
        params.update(dict(zip(keys, combo)))
        specs.append(StrategySpec(name=name, params=params))
    return specs


def compare_strategies(
    data: MarketData,
    specs: list[StrategySpec],
    config: EngineConfig | None = None,
    sort_by: str = "sharpe",
) -> tuple[pd.DataFrame, dict[str, BacktestResult]]:
    """Backtest every spec; return a metrics table (one row per spec) and the results."""
    config = config or EngineConfig()
    rows, results = [], {}
    for spec in specs:
        label = spec.resolved_label()
        try:
            res = run_backtest(data, make_strategy(spec.name, spec.params), config)
        except Exception as exc:
            log.error("%s failed: %s", label, exc)
            rows.append({"label": label, "strategy": spec.name, "error": str(exc)})
            continue
        results[label] = res
        row = {"label": label, "strategy": spec.name, **{f"p_{k}": v for k, v in spec.params.items()}}
        row.update({k: res.metrics[k] for k in KEY_METRICS})
        rows.append(row)
    table = pd.DataFrame(rows)
    if sort_by in table:
        table = table.sort_values(sort_by, ascending=False, na_position="last")
    return table.reset_index(drop=True), results


def walk_forward(
    data: MarketData,
    spec: StrategySpec,
    split: float | str = 0.7,
    config: EngineConfig | None = None,
) -> dict[str, dict]:
    """Fit-free robustness check: the same config on the in-sample and out-of-sample halves."""
    ins, oos = data.split(split)
    out = {}
    for name, part in (("in_sample", ins), ("out_of_sample", oos)):
        res = run_backtest(part, make_strategy(spec.name, spec.params), config)
        out[name] = {k: res.metrics[k] for k in KEY_METRICS + ["start", "end"]}
    return out
