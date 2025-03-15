"""Delta-neutral funding carry (cash-and-carry basis trade).

When smoothed annualised funding is above ``entry_apr`` the market is paying
shorts: short the perp and buy the same notional of spot/index. The position
earns every funding payment while price risk cancels, leaving only basis
drift and fees. Exit once funding decays below ``exit_apr`` (hysteresis keeps
fee churn down). Optionally run the mirror trade when funding is deeply
negative.
"""

from __future__ import annotations

import pandas as pd
from pydantic import Field

from perplexify.data.dataset import MarketData
from perplexify.strategies.base import Strategy, StrategyParams, Targets, register
from perplexify.strategies.features import annualized_funding, hysteresis_state, smoothed


class FundingCarryParams(StrategyParams):
    lookback_bars: int = Field(6, ge=1, description="Bars of funding averaged before acting")
    entry_apr: float = Field(0.10, description="Enter when smoothed annualised funding exceeds this")
    exit_apr: float = Field(0.03, description="Exit when smoothed annualised funding falls below this")
    leverage: float = Field(1.0, gt=0, description="Notional per leg as a fraction of equity")
    allow_negative_carry: bool = Field(False, description="Mirror the trade (long perp / short hedge) when funding < -entry_apr")


@register
class FundingCarry(Strategy):
    name = "funding_carry"
    description = "Delta-neutral: short perp + long index while funding is rich, flat otherwise."
    Params = FundingCarryParams

    def generate_targets(self, data: MarketData) -> Targets:
        p: FundingCarryParams = self.params
        apr = smoothed(annualized_funding(data.funding, data.bar_hours), p.lookback_bars)
        enter_pos = apr > p.entry_apr
        exit_pos = apr < p.exit_apr
        if p.allow_negative_carry:
            enter_neg = apr < -p.entry_apr
            exit_neg = apr > -p.exit_apr
        else:
            enter_neg = pd.DataFrame(False, index=apr.index, columns=apr.columns)
            exit_neg = enter_neg
        state = hysteresis_state(enter_pos, exit_pos, enter_neg, exit_neg).astype(float)
        return Targets(perp=-state * p.leverage, hedge=state * p.leverage)

    def warmup_bars(self) -> int:
        return self.params.lookback_bars
