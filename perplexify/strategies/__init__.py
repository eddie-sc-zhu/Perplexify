"""Funding-rate strategies. Importing the package registers every built-in."""

from perplexify.strategies import benchmark, carry, contrarian, cross_sectional, momentum  # noqa: F401
from perplexify.strategies.base import (  # noqa: F401
    REGISTRY,
    Strategy,
    StrategyParams,
    Targets,
    make_strategy,
    register,
    strategy_catalog,
)

__all__ = ["REGISTRY", "Strategy", "StrategyParams", "Targets", "make_strategy", "register", "strategy_catalog"]
