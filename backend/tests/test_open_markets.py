from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.kalshi import CircuitOpen, KalshiClient, RequestGovernor
from backend.app.open_markets import (
    OpenMarketService,
    OpenMarketsUnavailable,
    normalize_category,
    slugify,
    to_market,
)


def close_after(hours: int) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def market(ticker: str, close_time: str, yes: str | None, no: str | None, **extra):
    values = {
        "ticker": ticker,
        "event_ticker": f"EVENT-{ticker}",
        "title": ticker,
        "category": "Sports",
        "status": "active",
        "market_type": "binary",
        "close_time": close_time,
    }
    if yes is not None:
        values["yes_bid_dollars"] = yes
    if no is not None:
        values["no_bid_dollars"] = no
    return {**values, **extra}


def test_open_markets_api_defaults_validation_and_demo_is_nonpersistent(monkeypatch, tmp_path):
    monkeypatch.setenv("KALSHI_DATA_DIR", str(tmp_path / "outside-data"))
    monkeypatch.setenv("KALSHI_SYNC_MODE", "demo")
    from backend.app.main import app

    with TestClient(app) as client:
        response = client.get("/api/v1/open-markets")
        assert response.status_code == 200
        body = response.json()
        assert body["matching_markets"] == 2  # 91 YES plus a single 80/80 tie row.
        assert {item["qualifying_side"] for item in body["items"]} == {"yes", "both"}
        assert body["items"][0]["ticker"] == "KXDEMO-OPEN-RAIN"
        assert client.get("/api/v1/data/status").json()["has_data"] is False
        assert app.state.sync.governor is app.state.open_markets.governor
        assert app.state.sync.governor is app.state.request_governor
        assert client.get("/api/v1/open-markets?threshold=49").status_code == 422
        assert client.get("/api/v1/open-markets?horizon=30d").status_code == 422
        assert client.get("/api/v1/open-markets?page=0").status_code == 422
        assert client.get("/api/v1/open-markets?page_size=51").status_code == 422


@pytest.mark.asyncio
async def test_live_query_filters_locally_sorts_and_keeps_only_the_best_side(monkeypatch):
    calls = []
    first = market("Z-LATER", close_after(48), "0.95", "0.82", volume_24h_fp="2", liquidity_dollars="7")
    second = market("A-EARLIER", close_after(24), "0.90", "0.90", yes_sub_title="Up", no_sub_title="Down")
    filtered = [
        market("CLOSED", close_after(24), "0.99", "0.01", status="closed"),
        market("SCALAR", close_after(24), "0.99", "0.01", market_type="scalar"),
        market("MVE", close_after(24), "0.99", "0.01", mve_collection_ticker="KXMVE"),
        market("MVE-LEGS", close_after(24), "0.99", "0.01", mve_selected_legs=[{"side": "yes"}]),
        market("OUTSIDE", close_after(96), "0.99", "0.01"),
        market("MALFORMED", "not-a-date", "0.99", "0.01"),
    ]

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def pages(self, path, params, item_name):
            calls.append((path, params, item_name))
            yield [first, *filtered], "next"
            yield [second], None

        async def close(self):
            pass

    monkeypatch.setattr("backend.app.open_markets.KalshiClient", Client)
    service = OpenMarketService("real", "https://example.test", RequestGovernor(100))
    response = await service.list("3d", 80, 1, 50)
    assert calls[0][0] == "/markets"
    assert calls[0][2] == "markets"
    assert set(calls[0][1]) == {"min_close_ts", "max_close_ts", "mve_filter"}
    assert calls[0][1]["max_close_ts"] - calls[0][1]["min_close_ts"] == 3 * 24 * 60 * 60
    assert calls[0][1]["mve_filter"] == "exclude"
    assert response.scanned_markets == 8
    assert response.matching_markets == 2
    assert [(item.ticker, item.qualifying_side, item.qualifying_bid_percent) for item in response.items] == [
        ("A-EARLIER", "both", 90), ("Z-LATER", "yes", 95),
    ]
    assert response.items[0].qualifying_label == "Up / Down"
    assert response.items[0].volume_24h is None
    assert response.items[-1].volume_24h == 2
    assert response.items[-1].liquidity_dollars == 7


@pytest.mark.asyncio
async def test_cache_is_keyed_by_horizon_and_reused_across_threshold_and_pages(monkeypatch):
    fetched = 0

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def pages(self, *_args):
            nonlocal fetched
            fetched += 1
            await asyncio.sleep(0.01)
            yield [market("ONE", close_after(24), "0.91", "0.09")], None

        async def close(self):
            pass

    monkeypatch.setattr("backend.app.open_markets.KalshiClient", Client)
    service = OpenMarketService("real", "https://example.test", RequestGovernor(100))
    first, second = await asyncio.gather(
        service.list("7d", 80, 1, 1, refresh=True), service.list("7d", 80, 1, 1, refresh=True)
    )
    assert first.refresh_state == "refreshed"
    assert second.refresh_state == "cached"
    assert fetched == 1
    assert (await service.list("7d", 90, 2, 1)).refresh_state == "cached"
    assert fetched == 1
    assert (await service.list("24h", 80, 1, 1)).refresh_state == "fresh"
    assert fetched == 2
    assert (await service.list("7d", 80, 1, 1, refresh=True)).refresh_state == "refreshed"
    assert fetched == 3


@pytest.mark.asyncio
async def test_missing_bids_and_circuit_failure_use_stale_snapshot(monkeypatch):
    now = [0.0]
    values = [market("TIE", close_after(24), "0.80", "0.80"), market("MISSING", close_after(48), None, "0.81")]
    failure = [False]

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def pages(self, *_args):
            if failure[0]:
                raise CircuitOpen(12)
            yield values, None

        async def close(self):
            pass

    monkeypatch.setattr("backend.app.open_markets.KalshiClient", Client)
    governor = RequestGovernor(100, clock=lambda: now[0])
    service = OpenMarketService("real", "https://example.test", governor, clock=lambda: now[0])
    first = await service.list("7d", 80, 1, 50)
    assert [(item.ticker, item.qualifying_side) for item in first.items] == [
        ("TIE", "both"), ("MISSING", "no"),
    ]
    failure[0] = True
    stale = await service.list("7d", 80, 1, 50, refresh=True)
    assert stale.stale is True
    assert stale.refresh_state == "stale"

    cold = OpenMarketService("real", "https://example.test", governor)
    with pytest.raises(OpenMarketsUnavailable, match="Kalshi circuit breaker"):
        await cold.list("7d", 80, 1, 50)


@pytest.mark.asyncio
async def test_fractional_cent_bid_does_not_round_up_to_the_threshold(monkeypatch):
    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def pages(self, *_args):
            yield [market("FRACTIONAL", close_after(24), "0.796", "0.204")], None

        async def close(self):
            pass

    monkeypatch.setattr("backend.app.open_markets.KalshiClient", Client)
    service = OpenMarketService("real", "https://example.test", RequestGovernor(100))
    response = await service.list("7d", 80, 1, 50)
    assert response.matching_markets == 0
    below = await service.list("7d", 79, 1, 50)
    assert below.items[0].qualifying_bid_percent == 79.6


@pytest.mark.asyncio
async def test_rate_paced_request_rechecks_a_concurrent_circuit_open():
    governor = RequestGovernor(10, pause_seconds=5)
    await governor.wait_until_allowed()
    waiting = asyncio.create_task(governor.wait_until_allowed())
    await asyncio.sleep(0.01)
    await governor.record_429()
    with pytest.raises(CircuitOpen):
        await waiting


@pytest.mark.asyncio
async def test_market_pagination_preserves_close_bounds_on_every_page():
    queries = []

    async def handler(request: httpx.Request) -> httpx.Response:
        queries.append(dict(request.url.params))
        return httpx.Response(200, json={"markets": [], "cursor": "next" if len(queries) == 1 else None})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test")
    kalshi = KalshiClient(RequestGovernor(100), client=client)
    async for _markets, _cursor in kalshi.pages(
        "/markets", {"min_close_ts": 100, "max_close_ts": 200, "mve_filter": "exclude"}, "markets"
    ):
        pass
    await client.aclose()
    assert queries == [
        {"min_close_ts": "100", "max_close_ts": "200", "mve_filter": "exclude", "limit": "1000"},
        {"min_close_ts": "100", "max_close_ts": "200", "mve_filter": "exclude", "limit": "1000", "cursor": "next"},
    ]


def test_naive_close_times_are_normalized_to_utc():
    item = to_market(market("NAIVE", "2026-08-16T12:00:00", "0.81", "0.19"))
    assert item is not None
    assert item.close_at.tzinfo == UTC


@pytest.mark.asyncio
async def test_categories_are_enriched_in_event_batches_and_cached(monkeypatch):
    calls = []
    raw = market("ELECTION", close_after(24), "0.90", "0.10")
    raw.pop("category")
    raw["event_ticker"] = "EVENT-ELECTION"

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def pages(self, *_args):
            yield [raw], None

        async def get(self, path, params):
            calls.append((path, params))
            return {"events": [{"event_ticker": "EVENT-ELECTION", "category": "Elections"}], "cursor": None}

        async def close(self):
            pass

    monkeypatch.setattr("backend.app.open_markets.KalshiClient", Client)
    service = OpenMarketService("real", "https://example.test", RequestGovernor(100))
    first = await service.list("7d", 80, 1, 50)
    assert first.items[0].category == "Elections"
    assert calls == [("/events", {"tickers": "EVENT-ELECTION", "limit": 200})]
    await service.list("7d", 80, 1, 50, refresh=True)
    assert len(calls) == 1
    assert normalize_category("Sport") == "Sports"
    assert normalize_category("Unknown") == "Other"


@pytest.mark.asyncio
async def test_market_link_resolves_and_caches_the_canonical_kalshi_event_route(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get(self, path):
            calls.append(path)
            if path == "/events/KXPGATOUR-FESJC26":
                return {"event": {"series_ticker": "KXPGATOUR"}}
            assert path == "/series/KXPGATOUR"
            return {"series": {"title": "PGA Tour"}}

        async def close(self):
            pass

    monkeypatch.setattr("backend.app.open_markets.KalshiClient", Client)
    service = OpenMarketService("real", "https://example.test", RequestGovernor(100))
    expected = "https://kalshi.com/markets/kxpgatour/pga-tour/kxpgatour-fesjc26"
    assert await service.market_link("kxpgatour-fesjc26") == expected
    assert await service.market_link("KXPGATOUR-FESJC26") == expected
    assert calls == ["/events/KXPGATOUR-FESJC26", "/series/KXPGATOUR"]
    assert slugify("Women's Pro Basketball Game") == "womens-pro-basketball-game"
