"""Paper-trading bot control."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from perplexify.api.schemas import BotStartRequest
from perplexify.bot.paper_trader import BotConfig, PaperTrader

router = APIRouter(prefix="/bot", tags=["bot"])


def _bot(request: Request) -> PaperTrader | None:
    return getattr(request.app.state, "bot", None)


@router.get("/status")
def status(request: Request):
    bot = _bot(request)
    if bot is None:
        return {"running": False}
    return bot.status()


@router.post("/start")
async def start(req: BotStartRequest, request: Request):
    bot = _bot(request)
    if bot is not None and bot.running:
        raise HTTPException(409, "bot already running")
    cfg = BotConfig(**req.model_dump())
    bot = PaperTrader(cfg, request.app.state.data)
    request.app.state.bot = bot
    await bot.start()
    return bot.status()


@router.post("/stop")
async def stop(request: Request):
    bot = _bot(request)
    if bot is None or not bot.running:
        raise HTTPException(409, "bot not running")
    await bot.stop()
    return bot.status()


@router.post("/tick")
def tick(request: Request):
    """Force one evaluation cycle now (handy for demos and tests)."""
    bot = _bot(request)
    if bot is None:
        raise HTTPException(409, "bot not started")
    bot.tick()
    return bot.status()


@router.get("/positions")
def positions(request: Request):
    bot = _bot(request)
    if bot is None:
        raise HTTPException(409, "bot not started")
    return bot.state.positions


@router.get("/ledger")
def ledger(request: Request, limit: int = 100):
    bot = _bot(request)
    if bot is None:
        raise HTTPException(409, "bot not started")
    return bot.state.ledger[-limit:]
