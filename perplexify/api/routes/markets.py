"""Live market data: funding snapshots, history and opportunity scanning."""

from __future__ import annotations

import math

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request

from perplexify.api.schemas import CrossVenueOpportunity, FundingHistoryResponse, FundingPoint, MarketRow, Opportunity
from perplexify.utils import HOURS_PER_YEAR

router = APIRouter(tags=["markets"])


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    return v


def _rows(df: pd.DataFrame) -> list[dict]:
    return [{k: _clean(v) for k, v in rec.items()} for rec in df.to_dict("records")]


@router.get("/markets/{exchange}", response_model=list[MarketRow])
def markets(exchange: str, request: Request, top: int = Query(50, ge=1, le=500)):
    svc = request.app.state.data
    try:
        df = svc.markets(exchange)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    cols = [c for c in MarketRow.model_fields if c in df]
    return _rows(df[cols].head(top))


@router.get("/funding/{exchange}/{symbol}/history", response_model=FundingHistoryResponse)
def funding_history(exchange: str, symbol: str, request: Request, days: float = Query(30, gt=0, le=1000)):
    svc = request.app.state.data
    try:
        df, hours = svc.funding_history(exchange, symbol, days)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    points = [FundingPoint(**{k: _clean(v) for k, v in r.items()}) for r in df.to_dict("records")]
    mean_apr = float(df["funding_rate"].mean() * HOURS_PER_YEAR / hours) if len(df) else 0.0
    return FundingHistoryResponse(exchange=exchange, symbol=symbol.upper(), interval_hours=hours, points=points, mean_apr=mean_apr)


@router.get("/opportunities/{exchange}", response_model=list[Opportunity])
def opportunities(
    exchange: str,
    request: Request,
    min_apr: float = Query(0.10, description="Minimum |annualised funding| to report"),
    min_volume: float = Query(1_000_000, description="Minimum 24h notional volume"),
    top: int = Query(20, ge=1, le=200),
):
    """Single-venue scan: coins whose current funding is rich enough to harvest."""
    svc = request.app.state.data
    try:
        df = svc.markets(exchange)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    df = df[(df["funding_apr"].abs() >= min_apr) & (df["volume_24h"].fillna(0) >= min_volume)].copy()
    df["side"] = df["funding_apr"].map(lambda a: "short_perp" if a > 0 else "long_perp")
    df["exchange"] = exchange
    df = df.reindex(df["funding_apr"].abs().sort_values(ascending=False).index).head(top)
    cols = [c for c in Opportunity.model_fields if c in df]
    return _rows(df[cols])


@router.get("/opportunities/cross-venue/scan", response_model=list[CrossVenueOpportunity])
def cross_venue(request: Request, min_spread_apr: float = Query(0.05), top: int = Query(20, ge=1, le=200)):
    """Funding-arbitrage scan: long the perp where funding is lowest, short where highest."""
    svc = request.app.state.data
    pf = svc.predicted_fundings()
    out = []
    for sym, grp in pf.groupby("symbol"):
        if len(grp) < 2:
            continue
        aprs = dict(zip(grp["venue"], grp["funding_apr"].astype(float)))
        lo = min(aprs, key=aprs.get)
        hi = max(aprs, key=aprs.get)
        spread = aprs[hi] - aprs[lo]
        if spread >= min_spread_apr:
            out.append(CrossVenueOpportunity(symbol=sym, long_venue=lo, short_venue=hi, long_apr=aprs[lo], short_apr=aprs[hi], spread_apr=spread, venues=aprs))
    out.sort(key=lambda o: o.spread_apr, reverse=True)
    return out[:top]
