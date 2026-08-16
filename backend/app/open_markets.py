from __future__ import annotations

import asyncio
import math
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .kalshi import CircuitExhausted, CircuitOpen, KalshiClient, RequestGovernor
from .models import OpenMarket, OpenMarketsResponse

HORIZONS = {"24h": 24 * 60 * 60, "3d": 3 * 24 * 60 * 60, "7d": 7 * 24 * 60 * 60, "14d": 14 * 24 * 60 * 60}
CACHE_SECONDS = 60
CLOSING_SOON_SECONDS = 15 * 60
DISPLAY_CATEGORIES = (
    "Elections", "Politics", "Culture", "Sports", "Crypto", "Commodities",
    "Climate", "Economics", "Mentions", "Finance", "Tech & Science",
)
CATEGORY_ALIASES = {
    "climate and weather": "Climate",
    "financials": "Finance",
    "companies": "Finance",
    "science and technology": "Tech & Science",
    "entertainment": "Culture",
    "social": "Mentions",
}


class OpenMarketsUnavailable(RuntimeError):
    """A retryable live-catalog failure when there is no snapshot to serve."""

    def __init__(self, message: str, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


@dataclass(frozen=True)
class _Snapshot:
    markets: tuple[OpenMarket, ...]
    scanned_markets: int
    as_of: datetime
    cached_at: float


class OpenMarketService:
    """Live active-market catalog with a short in-memory cache only.

    The cache is deliberately kept before threshold and paging so all views of a
    horizon reuse one API scan. It is never written to the analytics store.
    """

    def __init__(
        self,
        mode: str,
        base_url: str,
        governor: RequestGovernor,
        *,
        clock: Any = time.monotonic,
    ):
        self.mode = mode
        self.base_url = base_url
        self.governor = governor
        self.clock = clock
        self._cache: dict[str, _Snapshot] = {}
        self._locks = {horizon: asyncio.Lock() for horizon in HORIZONS}
        self._event_links: dict[str, str] = {}
        self._event_link_locks: dict[str, asyncio.Lock] = {}
        self._series_slugs: dict[str, str] = {}
        self._event_categories: dict[str, str] = {}

    async def list(
        self, horizon: str, threshold: int, page: int, page_size: int, refresh: bool = False, category: str = "all"
    ) -> OpenMarketsResponse:
        observed = self._cache.get(horizon)
        if observed is not None and self._age(observed) < CACHE_SECONDS and not refresh:
            return self._page(observed, threshold, page, page_size, category, stale=False, state="cached")
        # A refresh is still a single flight: callers that began while the first
        # refresh was in progress reuse the snapshot that it just produced.
        async with self._locks[horizon]:
            snapshot = self._cache.get(horizon)
            if snapshot is not None and self._age(snapshot) < CACHE_SECONDS and (
                not refresh or snapshot is not observed
            ):
                return self._page(snapshot, threshold, page, page_size, category, stale=False, state="cached")
            try:
                snapshot = await self._fetch(horizon)
            except (CircuitOpen, CircuitExhausted, httpx.HTTPError) as exc:
                previous = self._cache.get(horizon)
                if previous is not None:
                    return self._page(previous, threshold, page, page_size, category, stale=True, state="stale")
                seconds = await self.governor.seconds_remaining()
                # Transient non-circuit errors still receive a resumable, bounded retry signal.
                raise OpenMarketsUnavailable(str(exc), max(1, seconds)) from exc
            self._cache[horizon] = snapshot
            return self._page(
                snapshot,
                threshold,
                page,
                page_size,
                category,
                stale=False,
                state="refreshed" if refresh else "fresh",
            )

    async def _fetch(self, horizon: str) -> _Snapshot:
        started_at = datetime.now(UTC)
        now = int(started_at.timestamp())
        maximum = now + HORIZONS[horizon]
        max_close_at = datetime.fromtimestamp(maximum, UTC)
        if self.mode == "demo":
            values = demo_open_markets(started_at)
        else:
            # These are intentionally the only server-side market filters. Active
            # ordinary binary selection remains local, avoiding API filter drift.
            params = {
                "min_close_ts": now,
                "max_close_ts": maximum,
                "mve_filter": "exclude",
            }
            values = []
            client = KalshiClient(self.governor, base_url=self.base_url)
            try:
                async for markets, _cursor in client.pages("/markets", params, "markets"):
                    values.extend(markets)
                await self._populate_categories(values, client, started_at, max_close_at)
            finally:
                await client.close()
        selected: list[OpenMarket] = []
        for market in values:
            if active_ordinary_binary(market):
                item = to_market(market)
                if item is not None and started_at <= item.close_at <= max_close_at:
                    selected.append(item)
        selected.sort(key=lambda item: (item.close_at, -item.qualifying_bid_percent, item.ticker))
        return _Snapshot(tuple(selected), len(values), datetime.now(UTC), self.clock())

    async def _populate_categories(
        self, markets: list[dict[str, Any]], client: KalshiClient, started_at: datetime, max_close_at: datetime
    ) -> None:
        """Attach event categories in a few batched requests, never one request per market."""
        event_tickers = {
            str(market.get("event_ticker", "")).strip().upper()
            for market in markets
            if active_ordinary_binary(market)
            and market.get("category") is None
            and market.get("event_ticker")
            and (close_at := parse_time(market.get("close_time"))) is not None
            and started_at <= close_at <= max_close_at
        }
        missing = sorted(event_tickers - self._event_categories.keys())
        batches = [missing[start:start + 200] for start in range(0, len(missing), 200)]

        async def fetch_batch(tickers: list[str]) -> dict[str, str]:
            resolved: dict[str, str] = {}
            cursor: str | None = None
            while True:
                params: dict[str, Any] = {"tickers": ",".join(tickers), "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                payload = await client.get("/events", params)
                for event in payload.get("events", []):
                    ticker = str(event.get("event_ticker", "")).strip().upper()
                    if ticker:
                        resolved[ticker] = normalize_category(event.get("category"))
                cursor = payload.get("cursor") or None
                if cursor is None:
                    return resolved

        for resolved in await asyncio.gather(*(fetch_batch(batch) for batch in batches)):
            self._event_categories.update(resolved)
        for market in markets:
            close_at = parse_time(market.get("close_time"))
            if market.get("category") is None and close_at is not None and started_at <= close_at <= max_close_at:
                event_ticker = str(market.get("event_ticker", "")).strip().upper()
                market["category"] = self._event_categories.get(event_ticker, "Other")

    def _page(
        self,
        snapshot: _Snapshot,
        threshold: int,
        page: int,
        page_size: int,
        category: str,
        *,
        stale: bool,
        state: str,
    ) -> OpenMarketsResponse:
        matches = [market for market in snapshot.markets if market.qualifying_bid_percent >= threshold]
        category_counts = {name: sum(market.category == name for market in matches) for name in DISPLAY_CATEGORIES}
        category_counts["Other"] = sum(market.category == "Other" for market in matches)
        now = datetime.now(UTC)
        closing_soon = [market for market in matches if now <= market.close_at <= now + timedelta(seconds=CLOSING_SOON_SECONDS)]
        if category == "closing_soon":
            matches = closing_soon
        elif category != "all":
            matches = [market for market in matches if market.category == category]
        total = len(matches)
        pages = math.ceil(total / page_size) if total else 0
        start = (page - 1) * page_size
        return OpenMarketsResponse(
            items=matches[start:start + page_size],
            page=page,
            page_size=page_size,
            total=total,
            pages=pages,
            scanned_markets=snapshot.scanned_markets,
            matching_markets=total,
            category_counts=category_counts,
            closing_soon_markets=len(closing_soon),
            as_of=snapshot.as_of,
            stale=stale,
            refresh_state=state,
            breaker_seconds_remaining=self.governor.seconds_remaining_sync(),
            next_close_at=matches[0].close_at if matches else None,
            highest_bid=max((market.qualifying_bid_percent for market in matches), default=None),
        )

    def _age(self, snapshot: _Snapshot | None) -> float:
        return float("inf") if snapshot is None else max(0.0, self.clock() - snapshot.cached_at)

    async def market_link(self, event_ticker: str) -> str:
        """Resolve Kalshi's canonical event route only after a user clicks its CTA.

        Market catalogs do not contain Kalshi's website slug. Resolving it lazily
        keeps opening the dashboard to the one bounded catalog request, while
        caching the result in memory for subsequent clicks.
        """
        normalized = event_ticker.strip().upper()
        if not normalized:
            raise ValueError("event_ticker is required")
        cached = self._event_links.get(normalized)
        if cached is not None:
            return cached
        lock = self._event_link_locks.setdefault(normalized, asyncio.Lock())
        async with lock:
            cached = self._event_links.get(normalized)
            if cached is not None:
                return cached
            if self.mode == "demo":
                raise ValueError("Demo markets do not have a live Kalshi page")
            client = KalshiClient(self.governor, base_url=self.base_url)
            try:
                event_payload = await client.get(f"/events/{normalized}")
                event = event_payload.get("event") or {}
                series_ticker = str(event.get("series_ticker") or "").strip().upper()
                if not series_ticker:
                    raise ValueError("Kalshi did not return a series ticker for this market")
                series_slug = self._series_slugs.get(series_ticker)
                if series_slug is None:
                    series_payload = await client.get(f"/series/{series_ticker}")
                    series = series_payload.get("series") or {}
                    series_slug = slugify(str(series.get("title") or ""))
                    if not series_slug:
                        raise ValueError("Kalshi did not return a usable series title for this market")
                    self._series_slugs[series_ticker] = series_slug
            finally:
                await client.close()
            url = f"https://kalshi.com/markets/{series_ticker.lower()}/{series_slug}/{normalized.lower()}"
            self._event_links[normalized] = url
            return url


def active_ordinary_binary(market: dict[str, Any]) -> bool:
    return (
        bool(market.get("ticker"))
        and
        str(market.get("status", "")).lower() == "active"
        and str(market.get("market_type", "")).lower() == "binary"
        and not market.get("mve_collection_ticker")
        and not market.get("mve_selected_legs")
    )


def to_market(market: dict[str, Any]) -> OpenMarket | None:
    yes_bid = cents(market, "yes_bid_dollars", "yes_bid")
    no_bid = cents(market, "no_bid_dollars", "no_bid")
    close_at = parse_time(market.get("close_time"))
    if close_at is None:
        return None
    if yes_bid is None and no_bid is None:
        return None
    yes_label = str(market.get("yes_sub_title") or "Yes")
    no_label = str(market.get("no_sub_title") or "No")
    if yes_bid is None:
        qualifying_side, qualifying_label, qualifying_bid = "no", no_label, no_bid
    elif no_bid is None:
        qualifying_side, qualifying_label, qualifying_bid = "yes", yes_label, yes_bid
    elif yes_bid == no_bid:
        qualifying_side, qualifying_label, qualifying_bid = "both", f"{yes_label} / {no_label}", yes_bid
    elif yes_bid > no_bid:
        qualifying_side, qualifying_label, qualifying_bid = "yes", yes_label, yes_bid
    else:
        qualifying_side, qualifying_label, qualifying_bid = "no", no_label, no_bid
    return OpenMarket(
        ticker=str(market.get("ticker", "")),
        event_ticker=market.get("event_ticker"),
        category=normalize_category(market.get("category")),
        title=str(market.get("title") or market.get("subtitle") or market.get("ticker", "")),
        subtitle=market.get("subtitle"),
        qualifying_side=qualifying_side,
        qualifying_label=qualifying_label,
        qualifying_bid_percent=qualifying_bid,
        yes_bid_percent=yes_bid,
        no_bid_percent=no_bid,
        volume_24h=number(market.get("volume_24h_fp", market.get("volume_24h"))),
        liquidity_dollars=number(market.get("liquidity_dollars", market.get("liquidity"))),
        close_at=close_at,
        can_close_early=bool(market.get("can_close_early", False)),
    )


def cents(market: dict[str, Any], dollars_key: str, legacy_key: str) -> float | None:
    if market.get(dollars_key) is not None:
        try:
            # Do not round here: a $0.796 bid is 79.6%, not a qualifying 80%.
            return float(Decimal(str(market[dollars_key])) * 100)
        except (InvalidOperation, ValueError):
            return None
    value = market.get(legacy_key)
    try:
        return None if value is None else float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return None


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    characters = (char.lower() if char.isalnum() else "" if char in "'’" else " " for char in normalized)
    return "-".join(part for part in "".join(characters).split())


def normalize_category(value: Any) -> str:
    candidate = str(value or "").strip()
    alias = CATEGORY_ALIASES.get(candidate.casefold())
    if alias is not None:
        return alias
    if candidate.casefold() == "sport":
        candidate = "Sports"
    for category in DISPLAY_CATEGORIES:
        if candidate.casefold() == category.casefold():
            return category
    return "Other"


def category_filter(value: str) -> str | None:
    candidate = value.strip()
    if candidate.casefold() == "all":
        return "all"
    if candidate.casefold() in {"closing_soon", "next_15_minutes"}:
        return "closing_soon"
    normalized = normalize_category(candidate)
    if normalized == "Other" and candidate.casefold() != "other":
        return None
    return normalized


def demo_open_markets(now: datetime) -> list[dict[str, Any]]:
    """Fixed Phase 2 sample data: deterministic across restarts and test runs."""
    return [
        {
            "ticker": "KXDEMO-OPEN-RAIN", "event_ticker": "KXDEMO-WEATHER", "title": "Will rainfall exceed 2 inches?",
            "category": "Climate",
            "status": "active", "market_type": "binary", "yes_sub_title": "Over 2 inches", "no_sub_title": "2 inches or less",
            "yes_bid_dollars": "0.91", "no_bid_dollars": "0.09", "volume_24h_fp": "142.50", "liquidity_dollars": "740.00",
            "close_time": (now.replace(microsecond=0) + timedelta(hours=12)).isoformat(), "can_close_early": True,
        },
        {
            "ticker": "KXDEMO-OPEN-RATE", "event_ticker": "KXDEMO-RATES", "title": "Will the rate hold steady?",
            "category": "Economics",
            "status": "active", "market_type": "binary", "yes_sub_title": "Hold steady", "no_sub_title": "Change",
            "yes_bid_dollars": "0.80", "no_bid_dollars": "0.80", "volume_24h_fp": "90", "liquidity_dollars": "610",
            "close_time": (now.replace(microsecond=0) + timedelta(days=2)).isoformat(), "can_close_early": False,
        },
        {
            "ticker": "KXDEMO-OPEN-LOW", "event_ticker": "KXDEMO-LOW", "title": "Low confidence example",
            "category": "Culture",
            "status": "active", "market_type": "binary", "yes_bid_dollars": "0.60", "no_bid_dollars": "0.40",
            "close_time": (now.replace(microsecond=0) + timedelta(days=3)).isoformat(),
        },
        {"ticker": "KXDEMO-CLOSED", "status": "closed", "market_type": "binary", "close_time": now.isoformat()},
        {"ticker": "KXDEMO-SCALAR", "status": "active", "market_type": "scalar", "close_time": now.isoformat()},
    ]
