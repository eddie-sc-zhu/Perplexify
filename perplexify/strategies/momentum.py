"""Directional funding momentum ("ride the crowd").

Persistent positive funding signals aggressive long demand; the strategy goes
long with it (and pays funding), short when funding is persistently negative.
Included mostly as a foil: after funding and fees it rarely beats carry.
"""

from __future__ import annotations

from pydantic import Field

from perplexify.data.dataset import MarketData
from perplexify.strategies.base import Strategy, StrategyParams, Targets, register
from perplexify.strategies.features import annualized_funding, hysteresis_state, smoothed


class FundingMomentumParams(StrategyParams):
    lookback_bars: int = Field(24, ge=1, description="Bars of funding averaged before acting")
    entry_apr: float = Field(0.25, gt=0, description="|smoothed annualised funding| needed to enter")
    exit_apr: float = Field(0.05, ge=0, description="|smoothed annualised funding| at which to exit")
    leverage: float = Field(0.5, gt=0, description="Perp notional as a fraction of equity")


@register
class FundingMomentum(Strategy):
    name = "funding_momentum"
    description = "Directional: long perp while funding is strongly positive, short while strongly negative."
    Params = FundingMomentumParams

    def generate_targets(self, data: MarketData) -> Targets:
        p: FundingMomentumParams = self.params
        apr = smoothed(annualized_funding(data.funding, data.bar_hours), p.lookback_bars)
        state = hysteresis_state(
            enter_pos=apr > p.entry_apr,
            exit_pos=apr < p.exit_apr,
            enter_neg=apr < -p.entry_apr,
            exit_neg=apr > -p.exit_apr,
        ).astype(float)
        return Targets(perp=state * p.leverage, hedge=state * 0.0)

    def warmup_bars(self) -> int:
        return self.params.lookback_bars
