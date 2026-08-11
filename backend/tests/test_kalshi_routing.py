from datetime import UTC, datetime

import httpx
import pytest

from backend.app.kalshi import HistoricalCutoff, KalshiClient, RequestGovernor
from backend.app.models import Window
from backend.app.service import SyncService, eligible_market, normalize_market
from backend.app.storage import Store


@pytest.mark.asyncio
async def test_cutoff_parses_documented_iso_fields():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/historical/cutoff"
        return httpx.Response(
            200,
            json={
                "market_settled_ts": "2026-01-02T03:04:05Z",
                "trades_created_ts": "2026-01-03T04:05:06Z",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test")
    kalshi = KalshiClient(RequestGovernor(100), client=client)
    cutoff = await kalshi.historical_cutoff()
    assert cutoff.market_settled_ts == int(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp())
    assert cutoff.trades_created_ts == int(datetime(2026, 1, 3, 4, 5, 6, tzinfo=UTC).timestamp())
    await client.aclose()


@pytest.mark.asyncio
async def test_half_open_probe_is_released_on_5xx_and_closed_on_permanent_4xx():
    now = [0.0]
    governor = RequestGovernor(100, clock=lambda: now[0])
    await governor.record_429()
    now[0] = 61
    responses = iter([503, 200])

    async def retry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(responses), json={})

    retry_client = httpx.AsyncClient(
        transport=httpx.MockTransport(retry_handler), base_url="https://example.test"
    )
    kalshi = KalshiClient(governor, client=retry_client)
    assert await kalshi.get("/retry") == {}
    assert await governor.wait_until_allowed() is False
    await retry_client.aclose()

    await governor.record_429()
    now[0] = 122

    async def bad_request_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad request"})

    bad_client = httpx.AsyncClient(
        transport=httpx.MockTransport(bad_request_handler), base_url="https://example.test"
    )
    with pytest.raises(httpx.HTTPStatusError):
        await KalshiClient(governor, client=bad_client).get("/bad")
    assert await governor.wait_until_allowed() is False
    await bad_client.aclose()


@pytest.mark.asyncio
async def test_real_sync_routes_historical_and_live_partitions(monkeypatch, tmp_path):
    cutoff = HistoricalCutoff(1_700_000_000, 1_700_000_100)
    calls: list[tuple[str, dict]] = []
    version = [1]

    class RecordingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def historical_cutoff(self):
            return cutoff

        async def pages(self, path, params, item_name, start_cursor=None):
            calls.append((path, params))
            if path == "/historical/markets":
                yield ([{
                    "ticker": "OLD", "title": "Old market", "settlement_value_dollars": "1",
                    "settlement_ts": "2023-10-01T00:00:00Z", "updated_time": "2023-10-02T00:00:00Z",
                }], None)
            elif path == "/markets":
                yield ([{
                    "ticker": "NEW", "title": "New market", "settlement_value_dollars": "0",
                    "settlement_ts": "2024-01-01T00:00:00Z",
                    "updated_time": f"2024-01-0{version[0]}T00:00:00Z",
                }], None)
            else:
                yield ([], None)

        async def close(self):
            return None

    monkeypatch.setattr("backend.app.service.KalshiClient", RecordingClient)
    store = Store(tmp_path / "data")
    service = SyncService(store, "real", 10, "https://example.test", 9, 2)
    store.start_run("run", Window.ALL.value)
    await service._real("run", Window.ALL)
    assert calls[0] == ("/historical/markets", {})
    live_params = dict(calls[1][1])
    assert live_params["status"] == "settled"
    assert live_params["min_settled_ts"] == cutoff.market_settled_ts
    trade_calls = [call for call in calls if call[0] in {"/historical/trades", "/markets/trades"}]
    assert {path for path, _ in trade_calls} == {"/historical/trades", "/markets/trades"}
    assert all(
        params.get("max_ts") == cutoff.trades_created_ts or params.get("min_ts") == cutoff.trades_created_ts
        for _, params in trade_calls
    )
    first_trade_call_count = len(trade_calls)
    calls.clear()
    store.start_run("run-2", Window.ALL.value)
    await service._real("run-2", Window.ALL)
    assert not [call for call in calls if call[0] in {"/historical/trades", "/markets/trades"}]
    assert first_trade_call_count == 4
    calls.clear()
    version[0] = 2
    store.start_run("run-3", Window.ALL.value)
    await service._real("run-3", Window.ALL)
    changed_trade_calls = [
        call for call in calls if call[0] in {"/historical/trades", "/markets/trades"}
    ]
    assert len(changed_trade_calls) == 2
    assert all(params["ticker"] == "NEW" for _, params in changed_trade_calls)
    store.close()


def test_market_result_is_preferred_and_scalar_is_excluded():
    yes = {"ticker": "YES", "result": "yes", "settlement_value_dollars": "0"}
    no = {"ticker": "NO", "result": "no", "settlement_value_dollars": "1"}
    scalar = {"ticker": "SCALAR", "result": "scalar", "settlement_value_dollars": "1"}
    assert eligible_market(yes) and eligible_market(no)
    assert not eligible_market(scalar)
    assert normalize_market(yes)["settlement_value_dollars"] == "1"
    assert normalize_market(no)["settlement_value_dollars"] == "0"


@pytest.mark.asyncio
async def test_trade_cursor_resume_uses_staging_without_replaying_prior_page(monkeypatch, tmp_path):
    cutoff = HistoricalCutoff(2_000_000_000, 2_000_000_100)
    trade_cursors: list[str | None] = []
    interrupted = [False]
    market = {
        "ticker": "RESUME", "title": "Resumable market", "settlement_value_dollars": "1",
        "settlement_ts": "2024-01-01T00:00:00Z", "updated_time": "2024-01-02T00:00:00Z",
    }
    first_trade = {"trade_id": "same", "yes_price_dollars": ".2", "no_price_dollars": ".8", "created_time": "2023-12-30T00:00:00Z"}
    second_trade = {"trade_id": "new", "yes_price_dollars": ".1", "no_price_dollars": ".9", "created_time": "2023-12-31T00:00:00Z"}

    class MultiPageClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def historical_cutoff(self):
            return cutoff

        async def pages(self, path, params, item_name, start_cursor=None):
            if path == "/historical/markets":
                yield ([market], None)
                return
            if path == "/markets":
                yield ([], None)
                return
            if path == "/historical/trades":
                trade_cursors.append(start_cursor)
                if start_cursor is None:
                    yield ([first_trade], "after-first")
                    if not interrupted[0]:
                        interrupted[0] = True
                        raise RuntimeError("simulated interruption")
                    return
                assert start_cursor == "after-first"
                yield ([first_trade, second_trade], None)
                return
            if path == "/markets/trades":
                yield ([], None)
                return
            raise AssertionError(path)

        async def close(self):
            return None

    monkeypatch.setattr("backend.app.service.KalshiClient", MultiPageClient)
    store = Store(tmp_path / "data")
    service = SyncService(store, "real", 10, "https://example.test", 60, 3)
    store.start_run("resume-run", Window.ALL.value)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await service._real("resume-run", Window.ALL)
    assert store.checkpoint("resume-run") == {
        "ticker": "RESUME", "endpoint": "/historical/trades", "cursor": "after-first"
    }
    assert len(store.staged_trades("resume-run", "RESUME")) == 1
    await service._real("resume-run", Window.ALL)
    assert trade_cursors == [None, "after-first"]
    assert store.staged_trades("resume-run", "RESUME") == []
    assert store.db.execute("SELECT trade_count FROM aggregates WHERE ticker = 'RESUME'").fetchone() == (2,)
    store.close()


@pytest.mark.asyncio
async def test_complete_checkpoint_publishes_staged_data_after_crash(monkeypatch, tmp_path):
    cutoff = HistoricalCutoff(2_000_000_000, 2_000_000_100)
    market = {
        "ticker": "COMPLETE", "title": "Complete checkpoint", "settlement_value_dollars": "0",
        "settlement_ts": "2024-01-01T00:00:00Z", "updated_time": "2024-01-02T00:00:00Z",
    }
    trade = {
        "trade_id": "staged", "yes_price_dollars": ".9", "no_price_dollars": ".1",
        "created_time": "2023-12-31T00:00:00Z",
    }

    class CompleteClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def historical_cutoff(self):
            return cutoff

        async def pages(self, path, params, item_name, start_cursor=None):
            if path == "/historical/markets":
                yield ([market], None)
                return
            if path == "/markets":
                yield ([], None)
                return
            raise AssertionError(f"trade endpoint should not be replayed: {path}")

        async def close(self):
            return None

    monkeypatch.setattr("backend.app.service.KalshiClient", CompleteClient)
    store = Store(tmp_path / "data")
    store.upsert_markets([market])
    store.start_run("complete-run", Window.ALL.value)
    store.append_staged_catalog("complete-run", [market])
    store.append_staged_trades("complete-run", "COMPLETE", [trade])
    store.update_run(
        "complete-run", checkpoint={"ticker": "COMPLETE", "endpoint": "complete", "cursor": None}
    )
    service = SyncService(store, "real", 10, "https://example.test", 60, 3)
    await service._real("complete-run", Window.ALL)
    assert store.staged_trades("complete-run", "COMPLETE") == []
    assert store.db.execute("SELECT trade_count FROM aggregates WHERE ticker = 'COMPLETE'").fetchone() == (1,)
    store.close()


def test_staging_path_rejects_untrusted_components(tmp_path):
    store = Store(tmp_path / "data")
    with pytest.raises(ValueError):
        store.append_staged_trades("../run", "SAFE", [])
    with pytest.raises(ValueError):
        store.staged_trades("safe-run", "../ticker")
    store.close()


@pytest.mark.asyncio
async def test_resume_checkpoint_survives_skipping_earlier_unchanged_market(monkeypatch, tmp_path):
    cutoff = HistoricalCutoff(2_000_000_000, 2_000_000_100)
    old = {"ticker": "OLD", "title": "Old", "settlement_value_dollars": "1",
           "settlement_ts": "2023-01-01T00:00:00Z", "updated_time": "2023-01-02T00:00:00Z"}
    later = {"ticker": "LATER", "title": "Later", "settlement_value_dollars": "0",
             "settlement_ts": "2024-01-01T00:00:00Z", "updated_time": "2024-01-02T00:00:00Z"}
    cursors = []

    class ResumeAfterSkipClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def historical_cutoff(self):
            return cutoff

        async def pages(self, path, params, item_name, start_cursor=None):
            if path == "/historical/markets":
                yield ([old, later], None)
                return
            if path == "/markets":
                yield ([], None)
                return
            if params["ticker"] == "OLD":
                raise AssertionError("unchanged market replayed")
            cursors.append(start_cursor)
            yield ([], None)

        async def close(self):
            return None

    monkeypatch.setattr("backend.app.service.KalshiClient", ResumeAfterSkipClient)
    store = Store(tmp_path / "data")
    store.upsert_markets([old, later])
    store.replace_market_trades(old, [])
    store.start_run("resume-after-skip", Window.ALL.value)
    store.append_staged_catalog("resume-after-skip", [old, later])
    store.update_run("resume-after-skip", checkpoint={"ticker": "LATER", "endpoint": "/historical/trades", "cursor": "saved"})
    await SyncService(store, "real", 10, "https://example.test", 60, 3)._real("resume-after-skip", Window.ALL)
    assert cursors == ["saved", None]
    store.close()


@pytest.mark.asyncio
async def test_catalog_cursor_resume_does_not_restart_at_first_page(monkeypatch, tmp_path):
    cutoff = HistoricalCutoff(2_000_000_000, 2_000_000_100)
    cursors = []
    interrupted = [False]
    first = {"ticker": "FIRST", "title": "First", "settlement_value_dollars": "1",
             "settlement_ts": "2024-01-01T00:00:00Z"}
    second = {"ticker": "SECOND", "title": "Second", "settlement_value_dollars": "0",
              "settlement_ts": "2024-02-01T00:00:00Z"}

    class CatalogResumeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def historical_cutoff(self):
            return cutoff

        async def pages(self, path, params, item_name, start_cursor=None):
            if path == "/historical/markets":
                cursors.append(start_cursor)
                if start_cursor is None:
                    yield ([first], "second-page")
                    if not interrupted[0]:
                        interrupted[0] = True
                        raise RuntimeError("catalog interrupted")
                    return
                assert start_cursor == "second-page"
                yield ([second], None)
                return
            if path == "/markets":
                yield ([], None)
                return
            yield ([], None)

        async def close(self):
            return None

    monkeypatch.setattr("backend.app.service.KalshiClient", CatalogResumeClient)
    store = Store(tmp_path / "data")
    store.start_run("catalog-resume", Window.ALL.value)
    service = SyncService(store, "real", 10, "https://example.test", 60, 3)
    with pytest.raises(RuntimeError, match="catalog interrupted"):
        await service._real("catalog-resume", Window.ALL)
    assert store.checkpoint("catalog-resume") == {
        "phase": "catalog", "endpoint": "/historical/markets", "cursor": "second-page"
    }
    assert store.current_run()["processed_markets"] == 1
    await service._real("catalog-resume", Window.ALL)
    assert cursors == [None, "second-page"]
    assert store.db.execute("SELECT count(*) FROM markets").fetchone() == (2,)
    assert store.staged_catalog("catalog-resume") == []
    store.close()
