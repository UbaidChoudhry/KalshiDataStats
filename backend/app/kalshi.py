from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx


class CircuitOpen(RuntimeError):
    def __init__(self, seconds_remaining: int):
        self.seconds_remaining = seconds_remaining
        super().__init__(f"Kalshi circuit breaker is open for {seconds_remaining} more seconds")


class CircuitExhausted(RuntimeError):
    pass


@dataclass(frozen=True)
class HistoricalCutoff:
    market_settled_ts: int
    trades_created_ts: int


class RequestGovernor:
    """One process-wide token bucket and 429 circuit breaker."""

    def __init__(
        self,
        requests_per_second: float,
        pause_seconds: int = 60,
        max_pauses: int = 3,
        clock=time.monotonic,
    ):
        self.interval = 1 / requests_per_second
        self.pause_seconds = pause_seconds
        self.max_pauses = max_pauses
        self.clock = clock
        self._next_request_at = 0.0
        self._open_until = 0.0
        self._open_count = 0
        self._probe_in_flight = False
        self._lock = asyncio.Lock()

    async def wait_until_allowed(self) -> bool:
        async with self._lock:
            now = self.clock()
            if now < self._open_until:
                raise CircuitOpen(max(1, int(self._open_until - now + 0.999)))
            is_probe = self._open_until > 0
            if is_probe:
                if self._probe_in_flight:
                    raise CircuitOpen(1)
                self._probe_in_flight = True
            delay = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + self.interval
        if delay:
            await asyncio.sleep(delay)
            # A concurrent request may have received a 429 while this request
            # was rate-paced. Recheck immediately before it reaches Kalshi.
            async with self._lock:
                now = self.clock()
                if now < self._open_until:
                    raise CircuitOpen(max(1, int(self._open_until - now + 0.999)))
                if self._open_until > 0 and not is_probe:
                    if self._probe_in_flight:
                        raise CircuitOpen(1)
                    self._probe_in_flight = True
                    return True
        return is_probe

    async def record_429(self) -> None:
        async with self._lock:
            self._probe_in_flight = False
            # A normal 429 opens pause one. The first two failed half-open probes
            # open pauses two and three; only the probe after pause three fails.
            if self._open_count >= self.max_pauses:
                raise CircuitExhausted(
                    f"Kalshi returned 429 after {self.max_pauses} configured circuit-breaker pauses"
                )
            self._open_count += 1
            self._open_until = self.clock() + self.pause_seconds

    async def record_success(self, was_probe: bool) -> None:
        async with self._lock:
            if was_probe:
                self._open_until = 0.0
                self._open_count = 0
                self._probe_in_flight = False

    async def release_probe(self) -> None:
        async with self._lock:
            self._probe_in_flight = False

    async def seconds_remaining(self) -> int:
        async with self._lock:
            return max(0, int(self._open_until - self.clock() + 0.999))

    def seconds_remaining_sync(self) -> int:
        return max(0, int(self._open_until - self.clock() + 0.999))


class KalshiClient:
    BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

    def __init__(
        self,
        governor: RequestGovernor,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
    ):
        self.governor = governor
        self.client = client or httpx.AsyncClient(base_url=base_url, timeout=30)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(4):
            probe = await self.governor.wait_until_allowed()
            try:
                response = await self.client.get(path, params=params)
            except (httpx.NetworkError, httpx.TimeoutException):
                if probe:
                    await self.governor.release_probe()
                if attempt == 3:
                    raise
                await asyncio.sleep(min(4, 0.25 * 2**attempt))
                continue
            if response.status_code == 429:
                await self.governor.record_429()
                raise CircuitOpen(await self.governor.seconds_remaining())
            if response.status_code >= 500:
                if probe:
                    await self.governor.release_probe()
                if attempt == 3:
                    response.raise_for_status()
                await asyncio.sleep(min(4, 0.25 * 2**attempt))
                continue
            if response.status_code >= 400:
                if probe:
                    # A reachable non-rate-limited API is sufficient to close the breaker;
                    # propagate the permanent validation/auth failure without retries.
                    await self.governor.record_success(True)
                response.raise_for_status()
            response.raise_for_status()
            await self.governor.record_success(probe)
            return response.json()
        raise AssertionError("unreachable")

    async def pages(
        self,
        path: str,
        params: dict[str, Any],
        item_name: str,
        start_cursor: str | None = None,
    ) -> AsyncIterator[tuple[list[dict[str, Any]], str | None]]:
        """Yield each page with the cursor needed for the next request."""
        cursor = start_cursor
        while True:
            page_params = {**params, "limit": 1000}
            if cursor:
                page_params["cursor"] = cursor
            payload = await self.get(path, page_params)
            next_cursor = payload.get("cursor") or None
            yield payload.get(item_name, []), next_cursor
            cursor = next_cursor
            if cursor is None:
                return

    async def historical_cutoff(self) -> HistoricalCutoff:
        payload = await self.get("/historical/cutoff")
        now = int(datetime.now(UTC).timestamp())
        return HistoricalCutoff(
            market_settled_ts=timestamp(payload.get("market_settled_ts"), now),
            trades_created_ts=timestamp(payload.get("trades_created_ts"), now),
        )


def timestamp(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
