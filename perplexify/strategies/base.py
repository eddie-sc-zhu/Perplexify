"""Strategy interface and registry.

A strategy turns a ``MarketData`` panel into *target weights* per bar and
symbol: ``perp`` (signed fraction of equity in the perpetual) and ``hedge``
(signed fraction in the spot/index leg). Targets at row t may only use data
up to and including row t; the engine holds them over (t, t+1].
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import pandas as pd
from pydantic import BaseModel, ConfigDict

from perplexify.data.dataset import MarketData


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Targets:
    perp: pd.DataFrame
    hedge: pd.DataFrame

    @classmethod
    def flat(cls, data: MarketData) -> "Targets":
        z = pd.DataFrame(0.0, index=data.index, columns=data.symbols)
        return cls(z, z.copy())


class Strategy(ABC):
    name: ClassVar[str] = "base"
    description: ClassVar[str] = ""
    Params: ClassVar[type[StrategyParams]] = StrategyParams

    def __init__(self, params: StrategyParams | dict[str, Any] | None = None, **overrides: Any) -> None:
        raw = params.model_dump() if isinstance(params, StrategyParams) else dict(params or {})
        raw.update(overrides)
        self.params = self.Params(**raw)

    @abstractmethod
    def generate_targets(self, data: MarketData) -> Targets:
        ...

    def warmup_bars(self) -> int:
        """Bars of history needed before the first non-trivial target."""
        return 0

    def __repr__(self) -> str:
        return f"{self.name}({self.params.model_dump()})"


REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    REGISTRY[cls.name] = cls
    return cls


def make_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    try:
        cls = REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown strategy {name!r}; available: {sorted(REGISTRY)}") from exc
    return cls(params)


def strategy_catalog() -> list[dict[str, Any]]:
    out = []
    for name, cls in sorted(REGISTRY.items()):
        schema = cls.Params.model_json_schema()
        out.append(
            {
                "name": name,
                "description": cls.description,
                "params": {
                    k: {"type": v.get("type", "any"), "default": v.get("default"), "description": v.get("description", "")}
                    for k, v in schema.get("properties", {}).items()
                },
            }
        )
    return out
