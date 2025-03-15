"""Reference portfolios used to put the funding strategies in context."""

from __future__ import annotations

from typing import Literal

import pandas as pd
from pydantic import Field

from perplexify.data.dataset import MarketData
from perplexify.strategies.base import Strategy, StrategyParams, Targets, register


class BuyAndHoldParams(StrategyParams):
    leg: Literal["hedge", "perp"] = Field("hedge", description="'hedge' = buy spot/index, 'perp' = long the perpetual (pays funding)")
    leverage: float = Field(1.0, gt=0, description="Total notional as a fraction of equity, split equally across symbols")


@register
class BuyAndHold(Strategy):
    name = "buy_and_hold"
    description = "Benchmark: equal-weight long in every symbol from the first bar."
    Params = BuyAndHoldParams

    def generate_targets(self, data: MarketData) -> Targets:
        p: BuyAndHoldParams = self.params
        w = pd.DataFrame(p.leverage / len(data.symbols), index=data.index, columns=data.symbols)
        zero = w * 0.0
        return Targets(perp=w, hedge=zero) if p.leg == "perp" else Targets(perp=zero, hedge=w)


class AlwaysShortPerpParams(StrategyParams):
    leverage: float = Field(1.0, gt=0, description="Notional per leg as a fraction of equity")


@register
class AlwaysCarry(Strategy):
    name = "always_carry"
    description = "Benchmark: permanently short perp / long index, collecting funding whatever its sign."
    Params = AlwaysShortPerpParams

    def generate_targets(self, data: MarketData) -> Targets:
        p: AlwaysShortPerpParams = self.params
        w = pd.DataFrame(p.leverage, index=data.index, columns=data.symbols)
        return Targets(perp=-w, hedge=w)
