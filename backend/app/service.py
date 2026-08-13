from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .analytics import window_start
from .kalshi import CircuitExhausted, CircuitOpen, KalshiClient, RequestGovernor
from .models import SyncRun, Window
from .storage import StorageLimitExceeded, Store


class SyncService:
    def __init__(
        self,
        store: Store,
        mode: str,
        requests_per_second: float,
        base_url: str,
        pause_seconds: int,
        max_pauses: int,
    ):
        self.store = store
        self.mode = mode
        self.base_url = base_url
        self.governor = RequestGovernor(requests_per_second, pause_seconds, max_pauses)
        self.task: asyncio.Task[None] | None = None
        self.run_id: str | None = None

    def create_run(self, window: Window) -> SyncRun:
        existing = self.current_run()
        if existing and existing.status in {"queued", "running", "breaker_open"}:
            if self.task and not self.task.done():
                return existing
            self.store.update_run(existing.id, status="queued", stage="resuming", error=None)
            self._schedule(existing.id, window)
            return self.current_run()  # type: ignore[return-value]
        if existing and existing.status in {"failed_resumable", "cancelled"} and existing.window == window:
            self.store.update_run(existing.id, status="queued", stage="resuming", error=None)
            self._schedule(existing.id, window)
            return self.current_run()  # type: ignore[return-value]
        run_id = str(uuid.uuid4())
        self.store.start_run(run_id, window.value)
        self._schedule(run_id, window)
        return self.current_run()  # type: ignore[return-value]

    def cancel_current(self) -> SyncRun | None:
        """Stop the active task without discarding its durable cursor and staging data."""
        current = self.current_run()
        if not current or current.status not in {"queued", "running", "breaker_open"}:
            return current
        self.store.update_run(
            current.id,
            status="cancelled",
            stage="cancelled",
            error="Cancelled by user. Download progress is saved locally.",
            resumable=True,
        )
        if self.task and not self.task.done():
            self.task.cancel()
        return self.current_run()

    def _schedule(self, run_id: str, window: Window) -> None:
        self.run_id = run_id
        self.task = asyncio.create_task(self._run(run_id, window))

    def current_run(self) -> SyncRun | None:
        row = self.store.current_run()
        if not row:
            return None
        total = row["total_markets"] or 0
        seconds = self.governor.seconds_remaining_sync()
        return SyncRun(id=row["id"], status=row["status"], stage=row["stage"], window=row["window"],
                       processed_markets=row["processed_markets"], total_markets=total,
                       progress_percent=round(100 * row["processed_markets"] / total, 1) if total else 0,
                       breaker_open=seconds > 0 or row["status"] == "breaker_open",
                       breaker_seconds_remaining=seconds, error=row["error"], resumable=row["resumable"])

    async def _run(self, run_id: str, window: Window) -> None:
        try:
            self.store.update_run(run_id, status="running", stage="starting", error=None)
            while True:
                try:
                    if self.mode == "demo":
                        await self._demo(run_id, window)
                    else:
                        await self._real(run_id, window)
                    self.store.update_run(
                        run_id, status="completed", stage="complete", finished_at=datetime.now(UTC)
                    )
                    return
                except CircuitOpen as exc:
                    previous_stage = self.current_run().stage if self.current_run() else "paused"
                    self.store.update_run(
                        run_id,
                        status="breaker_open",
                        stage=previous_stage,
                        error=str(exc),
                        resumable=True,
                    )
                    await asyncio.sleep(exc.seconds_remaining)
                    self.store.update_run(run_id, status="running", stage="retrying", error=None)
        except asyncio.CancelledError:
            self.store.update_run(
                run_id,
                status="cancelled",
                stage="cancelled",
                error="Cancelled by user. Download progress is saved locally.",
                resumable=True,
            )
        except CircuitExhausted as exc:
            self.store.update_run(
                run_id, status="failed_resumable", stage="paused", error=str(exc), resumable=True
            )
        except StorageLimitExceeded as exc:
            self.store.update_run(
                run_id, status="failed_resumable", stage="storage limit", error=str(exc), resumable=True
            )
        except Exception as exc:
            self.store.update_run(
                run_id, status="failed_resumable", stage="failed", error=str(exc), resumable=True
            )

    async def _demo(self, run_id: str, window: Window) -> None:
        markets = demo_markets()
        start = window_start(window)
        if start:
            markets = [m for m in markets if datetime.fromisoformat(m["settlement_ts"].replace("Z", "+00:00")) >= start]
        self.store.update_run(run_id, stage="catalog", total_markets=len(markets))
        unchanged_tickers = self.store.completed_unchanged_tickers(markets)
        self.store.upsert_markets(markets)
        for index, market in enumerate(markets, 1):
            if market["ticker"] in unchanged_tickers:
                self.store.update_run(
                    run_id,
                    stage="trades",
                    processed_markets=index,
                    checkpoint={"ticker": market["ticker"], "skipped": True},
                )
                continue
            self.store.replace_market_trades(market, demo_trades(market))
            self.store.update_run(run_id, stage="trades", processed_markets=index,
                                  checkpoint={"ticker": market["ticker"]})
            await asyncio.sleep(0)

    async def _real(self, run_id: str, window: Window) -> None:
        client = KalshiClient(self.governor, base_url=self.base_url)
        try:
            self.store.update_run(run_id, stage="cutoff")
            cutoff = await client.historical_cutoff()
            start = window_start(window)
            start_ts = int(start.timestamp()) if start else 0
            now_ts = int(datetime.now(UTC).timestamp())
            checkpoint = self.store.checkpoint(run_id)
            catalog_is_complete = checkpoint.get("phase") == "trades" or "ticker" in checkpoint
            markets = self.store.staged_catalog(run_id)
            if not catalog_is_complete:
                current = self.current_run()
                discovered_markets = current.processed_markets if current else 0
                catalog_endpoint = checkpoint.get("endpoint", "/historical/markets")
                catalog_cursor = checkpoint.get("cursor")
                if catalog_endpoint == "/historical/markets":
                    self.store.update_run(run_id, stage="historical catalog")
                    async for values, next_cursor in client.pages(
                        "/historical/markets", {}, "markets", start_cursor=catalog_cursor
                    ):
                        selected = [
                            normalize_market(m) for m in values
                            if eligible_market(m)
                            and market_in_window(m, start, cutoff.market_settled_ts)
                        ]
                        self.store.append_staged_catalog(run_id, selected)
                        discovered_markets += len(selected)
                        self.store.update_run(
                            run_id,
                            stage="historical catalog",
                            processed_markets=discovered_markets,
                            checkpoint={
                                "phase": "catalog",
                                "endpoint": "/historical/markets",
                                "cursor": next_cursor,
                            },
                        )
                    catalog_endpoint = "/markets"
                    catalog_cursor = None
                    self.store.update_run(
                        run_id,
                        stage="recent catalog",
                        checkpoint={"phase": "catalog", "endpoint": "/markets", "cursor": None},
                    )
                if catalog_endpoint == "/markets":
                    live_params = {
                        "status": "settled",
                        "min_settled_ts": max(start_ts, cutoff.market_settled_ts),
                        "max_settled_ts": now_ts,
                    }
                    self.store.update_run(run_id, stage="recent catalog")
                    async for values, next_cursor in client.pages(
                        "/markets", live_params, "markets", start_cursor=catalog_cursor
                    ):
                        self.store.append_staged_catalog(
                            run_id,
                            [normalize_market(m) for m in values if eligible_market(m)],
                        )
                        discovered_markets += sum(1 for m in values if eligible_market(m))
                        self.store.update_run(
                            run_id,
                            stage="recent catalog",
                            processed_markets=discovered_markets,
                            checkpoint={
                                "phase": "catalog",
                                "endpoint": "/markets",
                                "cursor": next_cursor,
                            },
                        )
                markets = self.store.staged_catalog(run_id)
                self.store.update_run(
                    run_id,
                    stage="catalog complete",
                    processed_markets=0,
                    total_markets=len(markets),
                    checkpoint={"phase": "trades"},
                )
            self.store.update_run(run_id, stage="catalog", total_markets=len(markets))
            unchanged_tickers = self.store.completed_unchanged_tickers(markets)
            resume_checkpoint = self.store.checkpoint(run_id)
            self.store.upsert_markets(markets)
            for index, market in enumerate(markets, 1):
                if market["ticker"] in unchanged_tickers:
                    self.store.update_run(
                        run_id,
                        stage="trades",
                        processed_markets=index,
                        checkpoint={"ticker": market["ticker"], "skipped": True},
                    )
                    continue
                # Keep the pre-loop resume location: progress updates for already-complete
                # markets must not erase a later market's saved page cursor.
                checkpoint = resume_checkpoint
                resume_ticker = checkpoint.get("ticker")
                resume_endpoint = checkpoint.get("endpoint") if resume_ticker == market["ticker"] else None
                resume_cursor = checkpoint.get("cursor") if resume_ticker == market["ticker"] else None
                if resume_ticker and resume_ticker != market["ticker"]:
                    # Earlier completed markets are protected by the unchanged snapshot. A newer
                    # checkpoint only affects its matching ticker.
                    resume_endpoint = None
                    resume_cursor = None
                trade_requests = (
                    ("/historical/trades", {"max_ts": cutoff.trades_created_ts}),
                    ("/markets/trades", {"min_ts": cutoff.trades_created_ts}),
                )
                endpoint_names = [endpoint for endpoint, _bounds in trade_requests]
                if resume_endpoint == "complete":
                    # A crash can happen after both streams have staged successfully but before
                    # the atomic final-partition replace. Publish that durable staging artifact.
                    trades = self.store.staged_trades(run_id, market["ticker"])
                    self.store.replace_market_trades(market, trades)
                    self.store.clear_staged_trades(run_id, market["ticker"])
                    self.store.update_run(
                        run_id,
                        stage="trades",
                        processed_markets=index,
                        checkpoint={"ticker": market["ticker"], "endpoint": "complete", "cursor": None},
                    )
                    continue
                for endpoint_index, (endpoint, bounds) in enumerate(trade_requests):
                    if resume_endpoint in endpoint_names and endpoint_index < endpoint_names.index(resume_endpoint):
                        continue
                    params = {"ticker": market["ticker"], "is_block_trade": "false", **bounds}
                    start_cursor = resume_cursor if endpoint == resume_endpoint else None
                    async for values, next_cursor in client.pages(
                        endpoint, params, "trades", start_cursor=start_cursor
                    ):
                        self.store.append_staged_trades(run_id, market["ticker"], values)
                        self.store.update_run(
                            run_id,
                            stage="trades",
                            processed_markets=index - 1,
                            checkpoint={
                                "ticker": market["ticker"],
                                "endpoint": endpoint,
                                "cursor": next_cursor,
                            },
                        )
                    next_endpoint = (
                        endpoint_names[endpoint_index + 1]
                        if endpoint_index + 1 < len(endpoint_names)
                        else "complete"
                    )
                    self.store.update_run(
                        run_id,
                        stage="trades",
                        processed_markets=index - 1,
                        checkpoint={
                            "ticker": market["ticker"], "endpoint": next_endpoint, "cursor": None
                        },
                    )
                    resume_cursor = None
                trades = self.store.staged_trades(run_id, market["ticker"])
                self.store.replace_market_trades(market, trades)
                self.store.clear_staged_trades(run_id, market["ticker"])
                self.store.update_run(
                    run_id,
                    stage="trades",
                    processed_markets=index,
                    checkpoint={"ticker": market["ticker"], "endpoint": "complete", "cursor": None},
                )
            self.store.clear_staged_catalog(run_id)
        finally:
            await client.close()


def eligible_market(market: dict[str, Any]) -> bool:
    result = str(market.get("result", "")).lower()
    if result:
        return result in {"yes", "no"} and market.get("market_type") != "scalar"
    value = market.get("settlement_value_dollars", market.get("settlement_value"))
    try:
        return float(value) in (0, 1) and market.get("market_type") != "scalar"
    except (TypeError, ValueError):
        return False


def normalize_market(market: dict[str, Any]) -> dict[str, Any]:
    result = str(market.get("result", "")).lower()
    normalized = {**market, "market_type": market.get("market_type", "binary")}
    if result in {"yes", "no"}:
        normalized["settlement_value_dollars"] = "1" if result == "yes" else "0"
    return normalized


def market_in_window(market: dict[str, Any], start: datetime | None, cutoff_ts: int) -> bool:
    settled = datetime.fromisoformat(str(market["settlement_ts"]).replace("Z", "+00:00"))
    return settled.timestamp() < cutoff_ts and (start is None or settled >= start)


def demo_markets() -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    return [
        {"ticker": "KXDEMO-RAIN", "event_ticker": "KXDEMO", "title": "Will rainfall exceed 2 inches?", "settlement_value_dollars": "0", "settlement_ts": (now - timedelta(days=12)).isoformat().replace("+00:00", "Z")},
        {"ticker": "KXDEMO-RATE", "event_ticker": "KXDEMO", "title": "Will the rate hold steady?", "settlement_value_dollars": "1", "settlement_ts": (now - timedelta(days=34)).isoformat().replace("+00:00", "Z")},
        {"ticker": "KXDEMO-GDP", "event_ticker": "KXDEMO", "title": "Will GDP exceed forecast?", "settlement_value_dollars": "0", "settlement_ts": (now - timedelta(days=80)).isoformat().replace("+00:00", "Z")},
        {"ticker": "KXDEMO-ELECTION", "event_ticker": "KXDEMO", "title": "Will the incumbent win?", "settlement_value_dollars": "1", "settlement_ts": (now - timedelta(days=140)).isoformat().replace("+00:00", "Z")},
    ]


def demo_trades(market: dict[str, Any]) -> list[dict[str, Any]]:
    at = datetime.fromisoformat(market["settlement_ts"].replace("Z", "+00:00")) - timedelta(days=2)
    prices = {"KXDEMO-RAIN": (91, 9), "KXDEMO-RATE": (15, 85), "KXDEMO-GDP": (82, 18), "KXDEMO-ELECTION": (62, 38)}[market["ticker"]]
    return [{"trade_id": market["ticker"] + "-1", "yes_price_dollars": prices[0] / 100, "no_price_dollars": prices[1] / 100, "created_time": at.isoformat()},
            {"trade_id": market["ticker"] + "-block", "yes_price_dollars": .99, "no_price_dollars": .01, "created_time": at.isoformat(), "is_block_trade": True}]
