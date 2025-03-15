"""Directional funding mean-reversion ("fade the crowd").

Extreme positive funding means longs are crowded and paying up; the strategy
shorts the perp (collecting funding) and bets on the premium mean-reverting.
Extreme negative funding is faded with a long. Signals are rolling z-scores
of funding so thresholds adapt to each coin's regime. ``hedge_ratio`` lets
you neutralise part of the price exposure with the index leg.
"""

from __future__ import annotations

from pydantic import Field

from perplexify.data.dataset import MarketData
from perplexify.strategies.base import Strategy, StrategyParams, Targets, register
from perplexify.strategies.features import hysteresis_state, smoothed, zscore


class FundingContrarianParams(StrategyParams):
    z_window: int = Field(180, ge=5, description="Rolling window (bars) for the funding z-score")
    smoothing_bars: int = Field(3, ge=1, description="Average funding over this many bars before scoring")
    z_entry: float = Field(2.0, gt=0, description="|z| needed to open a position")
    z_exit: float = Field(0.5, ge=0, description="|z| at which the position is closed")
    leverage: float = Field(0.5, gt=0, description="Perp notional as a fraction of equity")
    hedge_ratio: float = Field(0.0, ge=0, le=1, description="Fraction of the perp notional hedged on the index leg")


@register
class FundingContrarian(Strategy):
    name = "funding_contrarian"
    description = "Directional: short perp when funding z-score is extreme positive, long when extreme negative."
    Params = FundingContrarianParams

    def generate_targets(self, data: MarketData) -> Targets:
        p: FundingContrarianParams = self.params
        z = zscore(smoothed(data.funding, p.smoothing_bars), p.z_window)
        # state +1 = long perp (funding very negative), -1 = short perp (funding very positive)
        state = hysteresis_state(
            enter_pos=z < -p.z_entry,
            exit_pos=z > -p.z_exit,
            enter_neg=z > p.z_entry,
            exit_neg=z < p.z_exit,
        ).astype(float)
        perp = state * p.leverage
        return Targets(perp=perp, hedge=-perp * p.hedge_ratio)

    def warmup_bars(self) -> int:
        return self.params.z_window + self.params.smoothing_bars
