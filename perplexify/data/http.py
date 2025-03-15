"""Thin HTTP client with retries, backoff and per-venue request spacing."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from perplexify.config import settings

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    pass


class HttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        min_interval: float | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout or settings.http_timeout_seconds,
            headers={"User-Agent": "perplexify/0.1"},
        )
        self._max_retries = settings.http_max_retries if max_retries is None else max_retries
        self._min_interval = settings.http_min_interval_seconds if min_interval is None else min_interval
        self._last_request = 0.0

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, json: Any = None) -> Any:
        return self._request("POST", path, json=json)

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                resp = self._client.request(method, path, **kwargs)
                self._last_request = time.monotonic()
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise HttpError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, HttpError, ValueError) as exc:
                last_exc = exc
                backoff = min(2.0**attempt * 0.5, 8.0)
                if attempt == self._max_retries:
                    break
                log.warning("request failed (%s), retry %d/%d in %.1fs", exc, attempt + 1, self._max_retries, backoff)
                time.sleep(backoff)
            except httpx.HTTPStatusError as exc:
                raise HttpError(f"{method} {path} -> HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
        raise HttpError(f"{method} {path} failed after {self._max_retries + 1} attempts: {last_exc}")

    def close(self) -> None:
        self._client.close()
