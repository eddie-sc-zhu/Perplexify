"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from perplexify.backtest.compare import StrategySpec
from perplexify.backtest.engine import EngineConfig


class MarketRow(BaseModel):
    symbol: str
    mark_price: float | None = None
    index_price: float | None = None
    funding_rate: float
    funding_interval_hours: float
    funding_apr: float
    next_funding_time: datetime | None = None
    open_interest: float | None = None
    volume_24h: float | None = None


class FundingPoint(BaseModel):
    timestamp: datetime
    funding_rate: float
    premium: float | None = None
    index_price: float | None = None


class FundingHistoryResponse(BaseModel):
    exchange: str
    symbol: str
    interval_hours: float
    points: list[FundingPoint]
    mean_apr: float


class Opportunity(BaseModel):
    symbol: str
    exchange: str
    funding_rate: float
    funding_interval_hours: float
    funding_apr: float
    side: str = Field(description="'short_perp' collects positive funding, 'long_perp' collects negative funding")
    mark_price: float | None = None
    volume_24h: float | None = None


class CrossVenueOpportunity(BaseModel):
    symbol: str
    long_venue: str = Field(description="Venue where you go long the perp (lowest funding)")
    short_venue: str = Field(description="Venue where you go short the perp (highest funding)")
    long_apr: float
    short_apr: float
    spread_apr: float = Field(description="Annualised funding captured by the long/short pair")
    venues: dict[str, float]


class DataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exchange: str = Field("hyperliquid", description="hyperliquid | dydx | okx | synthetic")
    symbols: list[str] = Field(default_factory=lambda: ["BTC"])
    interval: str = Field("4h", description="Bar size: 1h, 4h, 8h, 1d")
    start: datetime | None = None
    end: datetime | None = None


class BacktestRequest(DataRequest):
    strategy: StrategySpec = Field(default_factory=lambda: StrategySpec(name="funding_carry"))
    engine: EngineConfig = Field(default_factory=EngineConfig)
    include_curve: bool = True
    max_points: int = Field(500, ge=10, le=20000)


class CompareRequest(DataRequest):
    strategies: list[StrategySpec]
    engine: EngineConfig = Field(default_factory=EngineConfig)
    sort_by: str = "sharpe"
    walk_forward_split: float | None = Field(None, gt=0, lt=1, description="Optional in/out-of-sample split fraction")


class CurvePoint(BaseModel):
    timestamp: datetime
    equity: float
    drawdown: float
    gross_leverage: float


class TradeSummary(BaseModel):
    count: int
    win_rate: float | None
    avg_return: float | None
    avg_bars_held: float | None
    by_direction: dict[str, int]


class DataSummary(BaseModel):
    exchange: str
    symbols: list[str]
    bar_hours: float
    bars: int
    start: datetime
    end: datetime
    has_index: bool


class BacktestResponse(BaseModel):
    strategy: str
    params: dict[str, Any]
    engine: EngineConfig
    data: DataSummary
    metrics: dict[str, Any]
    trades: TradeSummary
    events: list[dict[str, Any]]
    curve: list[CurvePoint] = Field(default_factory=list)


class CompareResponse(BaseModel):
    data: DataSummary
    table: list[dict[str, Any]]
    walk_forward: dict[str, dict[str, dict[str, Any]]] | None = None


class BotStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exchange: str = "hyperliquid"
    symbols: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    interval: str = "1h"
    strategy: StrategySpec = Field(default_factory=lambda: StrategySpec(name="funding_carry"))
    engine: EngineConfig = Field(default_factory=EngineConfig)
    poll_seconds: int = Field(300, ge=10)
    history_days: int = Field(30, ge=2)
