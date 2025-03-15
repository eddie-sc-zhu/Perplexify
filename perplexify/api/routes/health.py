"""Health and discovery endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from perplexify import __version__
from perplexify.strategies import strategy_catalog

router = APIRouter(tags=["meta"])


@router.get("/health")
def health(request: Request):
    return {"status": "ok", "version": __version__, "exchanges": request.app.state.data.exchanges()}


@router.get("/strategies")
def strategies():
    return strategy_catalog()
