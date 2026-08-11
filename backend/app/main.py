from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import analytics
from .config import Settings
from .models import (
    BandsResponse,
    DataStatus,
    HistorySummary,
    MissesResponse,
    SyncRequest,
    SyncRun,
    Window,
)
from .service import SyncService
from .storage import Store


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_environment()
    store = Store(settings.data_dir)
    app.state.settings = settings
    app.state.store = store
    app.state.sync = SyncService(
        store,
        settings.sync_mode,
        settings.requests_per_second,
        settings.base_url,
        settings.pause_seconds,
        settings.max_pauses,
    )
    try:
        yield
    finally:
        store.close()


app = FastAPI(title="Kalshi Data Stats", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])


def store() -> Store:
    return app.state.store


@app.get("/api/v1/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/data/status", response_model=DataStatus)
async def data_status() -> dict:
    return store().status()


@app.post("/api/v1/sync-runs", status_code=202, response_model=SyncRun)
async def start_sync(request: SyncRequest) -> SyncRun:
    return app.state.sync.create_run(request.window)


@app.get("/api/v1/sync-runs/current", response_model=SyncRun | None)
async def current_sync() -> SyncRun | None:
    return app.state.sync.current_run()


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
