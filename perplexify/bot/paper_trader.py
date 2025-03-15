"""Paper-trading bot: runs a strategy live against real funding and prices.

Each ``tick``:

1. refresh the recent panel for the configured symbols (cached, incremental),
2. accrue every funding settlement that landed since the previous tick,
3. mark positions to the latest price and update equity,
4. ask the strategy for fresh targets, pass them through the risk manager,
5. trade the difference at the mark price, paying fees and slippage,
6. append a ledger row and persist the state as JSON.

Only simulated money moves; the bot never signs or sends an order.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from perplexify.backtest.compare import StrategySpec
from perplexify.backtest.engine import EngineConfig
from perplexify.backtest.risk import RiskManager
from perplexify.config import settings
from perplexify.strategies import make_strategy
from perplexify.utils import interval_to_hours, utc_now

log = logging.getLogger(__name__)


class BotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exchange: str = "hyperliquid"
    symbols: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    interval: str = "1h"
    strategy: StrategySpec = Field(default_factory=lambda: StrategySpec(name="funding_carry"))
    engine: EngineConfig = Field(default_factory=EngineConfig)
    poll_seconds: int = 300
    history_days: int = 30
    state_dir: Path = Field(default_factory=lambda: settings.bot_state_dir)


class BotState(BaseModel):
    equity: float
    initial_equity: float
    # symbol -> {"perp_weight", "hedge_weight", "perp_qty", "hedge_qty", "price"}
    positions: dict[str, dict] = Field(default_factory=dict)
    ledger: list[dict] = Field(default_factory=list)
    last_funding_time: datetime | None = None
    last_tick: datetime | None = None
    ticks: int = 0
    funding_pnl: float = 0.0
    price_pnl: float = 0.0
    fees_paid: float = 0.0
    peak_equity: float = 0.0


class PaperTrader:
    def __init__(self, cfg: BotConfig, data_service) -> None:
        self.cfg = cfg
        self.data = data_service
        self.strategy = make_strategy(cfg.strategy.name, cfg.strategy.params)
        self.symbols = [s.upper() for s in cfg.symbols]
        self.risk = RiskManager(cfg.engine.risk, len(self.symbols))
        self.state = BotState(equity=cfg.engine.initial_equity, initial_equity=cfg.engine.initial_equity,
                              peak_equity=cfg.engine.initial_equity)
        for s in self.symbols:
            self.state.positions[s] = {"perp_weight": 0.0, "hedge_weight": 0.0, "perp_qty": 0.0, "hedge_qty": 0.0, "price": None}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.running = False
        self.last_error: str | None = None
        self._entry_equity = np.full(len(self.symbols), cfg.engine.initial_equity)
        self._trade_pnl = np.zeros(len(self.symbols))
        self._entry_tick = np.zeros(len(self.symbols), dtype=int)

    # -- persistence -------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return Path(self.cfg.state_dir) / f"paper_{self.cfg.exchange}_{self.cfg.strategy.name}.json"

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(self.state.model_dump_json(indent=2))

    # -- one evaluation cycle ---------------------------------------------
    def tick(self, now: pd.Timestamp | None = None) -> dict:
        now = now or utc_now()
        st = self.state
        bar_hours = interval_to_hours(self.cfg.interval)
        md = self.data.get_market_data(
            self.cfg.exchange, self.symbols, self.cfg.interval, now - pd.Timedelta(days=self.cfg.history_days), now
        )
        syms = [s for s in self.symbols if s in md.symbols]
        last_px = md.perp_close.iloc[-1]
        last_idx = md.index_close.iloc[-1]

        # 1. funding accrued since the previous tick (bars strictly after the last processed one)
        funding_pnl = 0.0
        new_bars = md.funding[md.index > st.last_funding_time] if st.last_funding_time else md.funding.iloc[-1:]
        for s in syms:
            qty = st.positions[s]["perp_qty"]
            if qty != 0.0:
                paid = -qty * last_px[s] * float(new_bars[s].sum())  # shorts (qty<0) receive positive funding
                funding_pnl += paid
        st.last_funding_time = md.index[-1].to_pydatetime()

        # 2. mark to market
        price_pnl = 0.0
        for s in syms:
            pos = st.positions[s]
            if pos["price"] is not None:
                price_pnl += pos["perp_qty"] * (last_px[s] - pos["price"]) + pos["hedge_qty"] * (last_idx[s] - pos["index_price"])
            pos["price"], pos["index_price"] = float(last_px[s]), float(last_idx[s])
        st.equity += funding_pnl + price_pnl
        st.funding_pnl += funding_pnl
        st.price_pnl += price_pnl
        st.peak_equity = max(st.peak_equity, st.equity)
        for i, s in enumerate(syms):
            if st.positions[s]["perp_qty"] != 0 or st.positions[s]["hedge_qty"] != 0:
                self._trade_pnl[i] += 0.0  # per-symbol attribution kept simple: handled via weights below

        # 3. fresh targets through the risk manager
        targets = self.strategy.generate_targets(md)
        tgt_p = targets.perp.iloc[-1].reindex(syms).fillna(0.0).to_numpy()
        tgt_h = targets.hedge.iloc[-1].reindex(syms).fillna(0.0).to_numpy()
        cur_p = np.array([st.positions[s]["perp_qty"] * last_px[s] / st.equity for s in syms])
        cur_h = np.array([st.positions[s]["hedge_qty"] * last_idx[s] / st.equity for s in syms])
        pnl_frac = np.where((cur_p != 0) | (cur_h != 0), (st.equity - self._entry_equity[: len(syms)]) / self._entry_equity[: len(syms)], 0.0)
        bars_held = np.where((cur_p != 0) | (cur_h != 0), st.ticks - self._entry_tick[: len(syms)], 0)
        tgt_p, tgt_h, events = self.risk.apply(st.ticks, tgt_p, tgt_h, cur_p, cur_h, st.equity, pnl_frac, bars_held)

        # 4. trade the difference
        fee_p = self.cfg.engine.perp_fee + self.cfg.engine.slippage_bps / 1e4
        fee_h = self.cfg.engine.hedge_fee + self.cfg.engine.slippage_bps / 1e4
        fills = []
        fees = 0.0
        for i, s in enumerate(syms):
            d_p = (tgt_p[i] - cur_p[i]) * st.equity
            d_h = (tgt_h[i] - cur_h[i]) * st.equity
            if abs(d_p) < 1e-9 and abs(d_h) < 1e-9:
                continue
            was_flat = cur_p[i] == 0 and cur_h[i] == 0
            fee = abs(d_p) * fee_p + abs(d_h) * fee_h
            fees += fee
            pos = st.positions[s]
            pos["perp_qty"] += d_p / last_px[s]
            pos["hedge_qty"] += d_h / last_idx[s]
            pos["perp_weight"], pos["hedge_weight"] = float(tgt_p[i]), float(tgt_h[i])
            if was_flat:
                self._entry_equity[i] = st.equity
                self._entry_tick[i] = st.ticks
            fills.append({"symbol": s, "perp_notional": round(d_p, 2), "hedge_notional": round(d_h, 2), "price": float(last_px[s]), "fee": round(fee, 4)})
        st.equity -= fees
        st.fees_paid += fees

        # 5. ledger + persist
        row = {
            "time": now.isoformat(), "equity": round(st.equity, 2), "funding_pnl": round(funding_pnl, 4),
            "price_pnl": round(price_pnl, 4), "fees": round(fees, 4), "fills": fills,
            "events": [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in e.items()} for e in events],
            "last_bar": md.index[-1].isoformat(), "bar_hours": bar_hours,
        }
        st.ledger.append(row)
        st.ledger = st.ledger[-5000:]
        st.last_tick = now.to_pydatetime()
        st.ticks += 1
        self.save()
        return row

    # -- loop --------------------------------------------------------------
    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                row = await asyncio.to_thread(self.tick)
                self.last_error = None
                log.info("tick %d equity=%.2f fills=%d", self.state.ticks, row["equity"], len(row["fills"]))
            except Exception as exc:  # keep the loop alive
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("bot tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        self._stop.clear()
        self.running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
        self.running = False

    def status(self) -> dict:
        st = self.state
        return {
            "running": self.running,
            "exchange": self.cfg.exchange,
            "symbols": self.symbols,
            "strategy": self.cfg.strategy.model_dump(),
            "equity": round(st.equity, 2),
            "return": round(st.equity / st.initial_equity - 1, 6),
            "drawdown": round(st.equity / st.peak_equity - 1, 6) if st.peak_equity else 0.0,
            "funding_pnl": round(st.funding_pnl, 2),
            "price_pnl": round(st.price_pnl, 2),
            "fees_paid": round(st.fees_paid, 2),
            "ticks": st.ticks,
            "last_tick": st.last_tick.isoformat() if st.last_tick else None,
            "last_error": self.last_error,
            "positions": st.positions,
            "state_file": str(self.state_path),
        }

    @classmethod
    def load(cls, cfg: BotConfig, data_service) -> "PaperTrader":
        bot = cls(cfg, data_service)
        if bot.state_path.exists():
            bot.state = BotState.model_validate_json(bot.state_path.read_text())
        return bot
