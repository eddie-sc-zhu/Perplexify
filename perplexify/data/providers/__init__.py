"""Provider registry."""

from __future__ import annotations

from perplexify.data.providers.base import FundingProvider, UnsupportedInterval  # noqa: F401
from perplexify.data.providers.dydx import DydxProvider
from perplexify.data.providers.hyperliquid import HyperliquidProvider
from perplexify.data.providers.okx import OkxProvider

PROVIDERS: dict[str, type[FundingProvider]] = {
    HyperliquidProvider.name: HyperliquidProvider,
    DydxProvider.name: DydxProvider,
    OkxProvider.name: OkxProvider,
}

_instances: dict[str, FundingProvider] = {}


def list_providers() -> list[str]:
    return sorted(PROVIDERS)


def get_provider(name: str) -> FundingProvider:
    key = name.lower()
    if key not in PROVIDERS:
        raise KeyError(f"Unknown exchange {name!r}; available: {list_providers()}")
    if key not in _instances:
        _instances[key] = PROVIDERS[key]()
    return _instances[key]
