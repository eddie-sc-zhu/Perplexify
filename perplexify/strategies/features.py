"""Vectorised feature helpers shared by the strategies."""

from __future__ import annotations

import numpy as np
import pandas as pd

from perplexify.utils import HOURS_PER_YEAR


def annualized_funding(funding: pd.DataFrame, bar_hours: float) -> pd.DataFrame:
    """Funding per bar -> simple annualised rate (e.g. 0.0001/8h -> 10.95%)."""
    return funding * (HOURS_PER_YEAR / bar_hours)


def smoothed(df: pd.DataFrame, window: int) -> pd.DataFrame:
    if window <= 1:
        return df
    return df.rolling(window, min_periods=window).mean()


def zscore(df: pd.DataFrame, window: int, min_periods: int | None = None) -> pd.DataFrame:
    roll = df.rolling(window, min_periods=min_periods or window)
    sd = roll.std(ddof=0)
    return (df - roll.mean()) / sd.where(sd > 0)


def hysteresis_state(
    enter_pos: pd.DataFrame,
    exit_pos: pd.DataFrame,
    enter_neg: pd.DataFrame,
    exit_neg: pd.DataFrame,
) -> pd.DataFrame:
    """Per-column state machine in {-1, 0, +1}.

    Flat -> +1 when ``enter_pos``; +1 -> flat when ``exit_pos`` (or straight
    to -1 when ``enter_neg``); symmetric for the negative side. NaN
    conditions count as False, so nothing fires during warm-up.
    """
    ep = enter_pos.fillna(False).to_numpy(dtype=bool)
    xp = exit_pos.fillna(False).to_numpy(dtype=bool)
    en = enter_neg.fillna(False).to_numpy(dtype=bool)
    xn = exit_neg.fillna(False).to_numpy(dtype=bool)
    T, N = ep.shape
    out = np.zeros((T, N), dtype=np.int8)
    state = np.zeros(N, dtype=np.int8)
    for t in range(T):
        for j in range(N):
            s = state[j]
            if s == 0:
                if ep[t, j]:
                    s = 1
                elif en[t, j]:
                    s = -1
            elif s == 1:
                if en[t, j]:
                    s = -1
                elif xp[t, j]:
                    s = 0
            else:
                if ep[t, j]:
                    s = 1
                elif xn[t, j]:
                    s = 0
            state[j] = s
        out[t] = state
    return pd.DataFrame(out, index=enter_pos.index, columns=enter_pos.columns)
