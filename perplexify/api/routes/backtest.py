"""Backtest endpoints: single run and multi-config comparison."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request

from perplexify.api.schemas import (
    BacktestRequest,
    BacktestResponse,
    CompareRequest,
    CompareResponse,
    CurvePoint,
    DataSummary,
    TradeSummary,
)
from perplexify.backtest import BacktestResult, compare_strategies, run_backtest, walk_forward
from perplexify.data.dataset import MarketData
from perplexify.strategies import make_strategy

router = APIRouter(prefix="/backtest", tags=["backtest"])


def _jsonable(v):
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def _metrics(m: dict) -> dict:
    return {k: _jsonable(v) for k, v in m.items()}


def _summary(md: MarketData) -> DataSummary:
    return DataSummary(
        exchange=md.exchange, symbols=md.symbols, bar_hours=md.bar_hours, bars=len(md),
        start=md.start.to_pydatetime(), end=md.end.to_pydatetime(), has_index=md.has_index,
    )


def _curve(res: BacktestResult, max_points: int) -> list[CurvePoint]:
    n = len(res.equity)
    step = max(1, math.ceil(n / max_points))
    dd = res.drawdown()
    gross = res.positions_perp.abs().sum(axis=1) + res.positions_hedge.abs().sum(axis=1)
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return [
        CurvePoint(timestamp=res.equity.index[i].to_pydatetime(), equity=float(res.equity.iloc[i]),
                   drawdown=float(dd.iloc[i]), gross_leverage=float(gross.iloc[i]))
        for i in idx
    ]


def _trades(res: BacktestResult) -> TradeSummary:
    t = res.trades
    if t.empty:
        return TradeSummary(count=0, win_rate=None, avg_return=None, avg_bars_held=None, by_direction={})
    return TradeSummary(
        count=int(len(t)), win_rate=float((t["pnl"] > 0).mean()), avg_return=float(t["return"].mean()),
        avg_bars_held=float(t["bars"].mean()), by_direction={k: int(v) for k, v in t["direction"].value_counts().items()},
    )


def _load(request: Request, req) -> MarketData:
    svc = request.app.state.data
    try:
        return svc.get_market_data(req.exchange, req.symbols, req.interval, req.start, req.end)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("", response_model=BacktestResponse)
def backtest(req: BacktestRequest, request: Request):
    md = _load(request, req)
    try:
        strategy = make_strategy(req.strategy.name, req.strategy.params)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    res = run_backtest(md, strategy, req.engine)
    return BacktestResponse(
        strategy=res.strategy, params=res.params, engine=res.config, data=_summary(md), metrics=_metrics(res.metrics),
        trades=_trades(res), events=[{k: _jsonable(v) for k, v in e.items()} for e in res.events],
        curve=_curve(res, req.max_points) if req.include_curve else [],
    )


@router.post("/compare", response_model=CompareResponse)
def compare(req: CompareRequest, request: Request):
    if not req.strategies:
        raise HTTPException(400, "strategies must not be empty")
    md = _load(request, req)
    table, _ = compare_strategies(md, req.strategies, req.engine, sort_by=req.sort_by)
    rows = [{k: _jsonable(v) for k, v in r.items()} for r in table.to_dict("records")]
    wf = None
    if req.walk_forward_split:
        wf = {}
        for spec in req.strategies:
            try:
                parts = walk_forward(md, spec, req.walk_forward_split, req.engine)
            except Exception as exc:  # report, do not fail the whole comparison
                parts = {"error": {"message": str(exc)}}
            wf[spec.resolved_label()] = {k: _metrics(v) for k, v in parts.items()}
    return CompareResponse(data=_summary(md), table=rows, walk_forward=wf)
