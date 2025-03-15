"""Cross-sectional carry: rotate the delta-neutral trade into the richest funding.

Every ``rebalance_bars`` the universe is ranked by smoothed annualised
funding; the ``top_n`` names above ``min_apr`` get an equal slice of the
leverage budget (short perp / long index). Diversifying across coins smooths
the funding income and reduces single-name basis risk.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from perplexify.data.dataset import MarketData
from perplexify.strategies.base import Strategy, StrategyParams, Targets, register
from perplexify.strategies.features import annualized_funding, smoothed


class CrossSectionalCarryParams(StrategyParams):
    lookback_bars: int = Field(24, ge=1, description="Bars of funding averaged for the ranking")
    top_n: int = Field(3, ge=1, description="Maximum number of names held")
    min_apr: float = Field(0.05, description="Minimum smoothed annualised funding to be eligible")
    rebalance_bars: int = Field(6, ge=1, description="Bars between rebalances")
    leverage: float = Field(1.0, gt=0, description="Total notional per leg across all names, fraction of equity")
    allow_negative_carry: bool = Field(False, description="Also rank by |funding| and mirror the trade on negative names")


@register
class CrossSectionalCarry(Strategy):
    name = "cross_sectional_carry"
    description = "Multi-asset delta-neutral carry: hold the top-N coins by funding, rebalanced periodically."
    Params = CrossSectionalCarryParams

    def generate_targets(self, data: MarketData) -> Targets:
        p: CrossSectionalCarryParams = self.params
        apr = smoothed(annualized_funding(data.funding, data.bar_hours), p.lookback_bars).to_numpy(dtype=float)
        T, N = apr.shape
        state = np.zeros((T, N))
        current = np.zeros(N)
        for t in range(T):
            if t % p.rebalance_bars == 0:
                row = apr[t]
                score = np.abs(row) if p.allow_negative_carry else row
                score = np.where(np.isnan(score), -np.inf, score)
                eligible = np.flatnonzero(score >= p.min_apr)
                current = np.zeros(N)
                if len(eligible):
                    chosen = eligible[np.argsort(-score[eligible], kind="stable")[: p.top_n]]
                    w = p.leverage / len(chosen)
                    current[chosen] = np.sign(row[chosen]) * w
            state[t] = current
        carry = pd.DataFrame(state, index=data.index, columns=data.symbols)
        return Targets(perp=-carry, hedge=carry)

    def warmup_bars(self) -> int:
        return self.params.lookback_bars
