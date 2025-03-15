"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from perplexify import __version__
from perplexify.api.routes import backtest, bot, health, markets
from perplexify.api.service import DataService, default_service
from perplexify.data.http import HttpError

log = logging.getLogger(__name__)


def create_app(data_service: DataService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.data = data_service or default_service()
        app.state.bot = None
        yield
        running = getattr(app.state, "bot", None)
        if running is not None and running.running:
            await running.stop()

    app = FastAPI(
        title="Perplexify",
        version=__version__,
        description="Crypto perpetual-futures funding-rate trading system: live funding scanner, backtests, strategy comparison and a paper-trading bot.",
        lifespan=lifespan,
    )
    @app.exception_handler(HttpError)
    async def _upstream_error(request: Request, exc: HttpError):
        log.warning("upstream venue unreachable: %s", exc)
        return JSONResponse(status_code=502, content={"detail": f"exchange unreachable: {exc}"})

    app.include_router(health.router)
    app.include_router(markets.router)
    app.include_router(backtest.router)
    app.include_router(bot.router)
    return app


app = create_app()
