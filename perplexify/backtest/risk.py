"""Predefined risk constraints applied to every target before execution."""

from __future__ import annotations

import math

import numpy as np
from pydantic import BaseModel, ConfigDict, Field


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_gross_leverage: float = Field(2.0, ge=0, description="Cap on sum(|perp| + |hedge|) as a multiple of equity")
    max_position_weight: float = Field(1.0, ge=0, description="Cap on |weight| of any single leg in one symbol")
    max_concurrent_positions: int | None = Field(None, ge=1, description="Keep only the k largest positions")
    max_drawdown: float | None = Field(0.15, ge=0, le=1, description="Kill switch: flatten everything past this drawdown")
    drawdown_cooldown_bars: int | None = Field(None, ge=1, description="Bars to stay flat after the kill switch; None = rest of the run")
    stop_loss: float | None = Field(None, ge=0, description="Close a symbol whose open trade loses this fraction of entry equity")
    stop_loss_cooldown_bars: int = Field(0, ge=0, description="Bars a stopped symbol stays blocked")
    min_holding_bars: int = Field(0, ge=0, description="Do not close or flip a position before this many bars")


class RiskManager:
    """Stateful filter: ``apply`` turns raw strategy targets into executable ones."""

    def __init__(self, cfg: RiskConfig, n_symbols: int) -> None:
        self.cfg = cfg
        self.n = n_symbols
        self.peak = -math.inf
        self.halted_until: float | None = None
        self.blocked_until = np.zeros(n_symbols)

    def apply(
        self,
        t: int,
        target_perp: np.ndarray,
        target_hedge: np.ndarray,
        current_perp: np.ndarray,
        current_hedge: np.ndarray,
        equity: float,
        trade_pnl_frac: np.ndarray,
        bars_held: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, list[dict]]:
        cfg = self.cfg
        events: list[dict] = []
        p = target_perp.astype(float).copy()
        h = target_hedge.astype(float).copy()
        zeros = np.zeros(self.n)

        # 1. portfolio drawdown kill switch
        if self.halted_until is not None:
            if t < self.halted_until:
                return zeros, zeros.copy(), events
            self.halted_until = None
            self.peak = equity
            events.append({"bar": t, "type": "resume", "equity": equity})
        self.peak = max(self.peak, equity)
        if cfg.max_drawdown is not None and self.peak > 0 and equity < self.peak * (1 - cfg.max_drawdown):
            self.halted_until = (t + cfg.drawdown_cooldown_bars) if cfg.drawdown_cooldown_bars else math.inf
            events.append({"bar": t, "type": "kill_switch", "equity": equity, "peak": self.peak,
                           "drawdown": equity / self.peak - 1})
            return zeros, zeros.copy(), events

        open_mask = (np.abs(current_perp) > 1e-12) | (np.abs(current_hedge) > 1e-12)

        # 2. per-position stop loss
        if cfg.stop_loss is not None:
            stopped = open_mask & (trade_pnl_frac < -cfg.stop_loss)
            for i in np.flatnonzero(stopped):
                self.blocked_until[i] = t + cfg.stop_loss_cooldown_bars + 1
                events.append({"bar": t, "type": "stop_loss", "symbol_index": int(i), "pnl_frac": float(trade_pnl_frac[i])})
            p[stopped] = 0.0
            h[stopped] = 0.0

        # 3. symbols blocked after a stop
        blocked = self.blocked_until > t
        p[blocked] = 0.0
        h[blocked] = 0.0

        # 4. minimum holding period: refuse to close/flip young positions
        if cfg.min_holding_bars > 0:
            young = open_mask & (bars_held < cfg.min_holding_bars) & ~blocked
            closing = young & ((np.abs(p) < 1e-12) & (np.abs(h) < 1e-12))
            flipping = young & ((np.sign(p) * np.sign(current_perp) < 0) | (np.sign(h) * np.sign(current_hedge) < 0))
            keep = closing | flipping
            p[keep] = current_perp[keep]
            h[keep] = current_hedge[keep]

        # 5. per-symbol cap
        cap = cfg.max_position_weight
        p = np.clip(p, -cap, cap)
        h = np.clip(h, -cap, cap)

        # 6. concentration cap
        if cfg.max_concurrent_positions is not None:
            size = np.abs(p) + np.abs(h)
            active = np.flatnonzero(size > 1e-12)
            if len(active) > cfg.max_concurrent_positions:
                keep_idx = active[np.argsort(-size[active], kind="stable")[: cfg.max_concurrent_positions]]
                mask = np.zeros(self.n, dtype=bool)
                mask[keep_idx] = True
                p[~mask] = 0.0
                h[~mask] = 0.0

        # 7. gross leverage cap
        gross = float(np.abs(p).sum() + np.abs(h).sum())
        if cfg.max_gross_leverage > 0 and gross > cfg.max_gross_leverage:
            scale = cfg.max_gross_leverage / gross
            p *= scale
            h *= scale
        return p, h, events
