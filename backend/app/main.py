from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import analytics
from .config import Settings
from .kalshi import RequestGovernor
from .models import (
    BandsResponse,
    DataStatus,
    HistorySummary,
    MissesResponse,
    OpenMarketHorizon,
    OpenMarketsErrorResponse,
    OpenMarketsResponse,
    SyncRequest,
    SyncRun,
    Window,
)
from .open_markets import OpenMarketService, OpenMarketsUnavailable
from .service import SyncService
from .storage import LegacyDatasetError, Store


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_environment()
    app.state.settings = settings
    app.state.store = None
    app.state.sync = None
    app.state.request_governor = RequestGovernor(
        settings.requests_per_second, settings.pause_seconds, settings.max_pauses
    )
    app.state.open_markets = OpenMarketService(
        settings.sync_mode, settings.base_url, app.state.request_governor
    )
    app.state.legacy_cache_error = None
    try:
        store = Store(settings.data_dir, max_storage_bytes=settings.max_storage_bytes)
        store.mark_interrupted_runs_resumable()
        app.state.store = store
        app.state.sync = SyncService(
            store,
            settings.sync_mode,
            settings.requests_per_second,
            settings.base_url,
            settings.pause_seconds,
            settings.max_pauses,
            governor=app.state.request_governor,
        )
    except LegacyDatasetError as exc:
        # Still serve the local UI so it can give a clear recovery instruction.
        app.state.legacy_cache_error = str(exc)
    try:
        yield
    finally:
        if app.state.store is not None:
            app.state.store.close()


app = FastAPI(title="Kalshi Data Stats", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])


def store() -> Store:
    if app.state.store is None:
        raise HTTPException(409, app.state.legacy_cache_error or "Local data store is unavailable")
    return app.state.store


@app.get("/api/v1/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/data/status", response_model=DataStatus)
async def data_status() -> dict:
    if app.state.store is None:
        return {
            "has_data": False, "dataset_version": "2", "scope": "legacy",
            "legacy_cache_error": app.state.legacy_cache_error,
        }
    return store().status()


@app.post("/api/v1/sync-runs", status_code=202, response_model=SyncRun)
async def start_sync(request: SyncRequest) -> SyncRun:
    if app.state.sync is None:
        raise HTTPException(409, app.state.legacy_cache_error or "Local data store is unavailable")
    return app.state.sync.create_run(request.window)


@app.get("/api/v1/sync-runs/current", response_model=SyncRun | None)
async def current_sync() -> SyncRun | None:
    if app.state.sync is None:
        return None
    return app.state.sync.current_run()


@app.post("/api/v1/sync-runs/current/cancel", response_model=SyncRun | None)
async def cancel_current_sync() -> SyncRun | None:
    if app.state.sync is None:
        return None
    return app.state.sync.cancel_current()


@app.get("/api/v1/history/summary", response_model=HistorySummary)
async def history_summary(window: Window = Window.SIX_MONTHS, threshold: int = Query(80, ge=50, le=99)) -> dict:
    return analytics.summary(store(), window, threshold)


@app.get("/api/v1/history/bands", response_model=BandsResponse)
async def history_bands(window: Window = Window.SIX_MONTHS, threshold: int = Query(80, ge=50, le=99)) -> dict:
    return {"items": analytics.bands(store(), window, threshold)}


@app.get("/api/v1/history/misses", response_model=MissesResponse)
async def history_misses(
    window: Window = Window.SIX_MONTHS,
    threshold: int = Query(80, ge=50, le=99),
    min_percent: int | None = Query(None, ge=50, le=99),
    max_percent: int | None = Query(None, ge=50, le=99),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=50),
    sort: str = "peak_confidence",
    direction: Literal["asc", "desc"] = "desc",
) -> dict:
    if min_percent is not None and max_percent is not None and min_percent > max_percent:
        raise HTTPException(422, "min_percent cannot exceed max_percent")
    return analytics.misses(store(), window, threshold, min_percent, max_percent, page, page_size, sort, direction)


@app.get(
    "/api/v1/open-markets",
    response_model=OpenMarketsResponse,
    responses={503: {"model": OpenMarketsErrorResponse, "description": "Live snapshot temporarily unavailable"}},
)
async def open_markets(
    threshold: int = Query(80, ge=50, le=99),
    horizon: OpenMarketHorizon = OpenMarketHorizon.SEVEN_DAYS,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=50),
    refresh: bool = False,
) -> OpenMarketsResponse | JSONResponse:
    try:
        return await app.state.open_markets.list(horizon.value, threshold, page, page_size, refresh)
    except OpenMarketsUnavailable as exc:
        # Keep errors out of FastAPI's opaque `detail` wrapper so the client can
        # resume a refresh using the same countdown field as a stale response.
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(exc.retry_after_seconds)},
            content={
                "error": {
                    "code": "open_markets_unavailable",
                    "message": str(exc),
                    "resumable": True,
                },
                "breaker_seconds_remaining": exc.retry_after_seconds,
            },
        )


# The production build is optional during backend-only development. When present, one local
# process serves it, while the API remains under /api/v1.
frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        index = frontend_dist / "index.html"
        if index.exists():
            return FileResponse(index)
        raise HTTPException(404)
