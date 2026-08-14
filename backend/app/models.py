from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Window(StrEnum):
    THREE_MONTHS = "3m"
    SIX_MONTHS = "6m"
    ONE_YEAR = "1y"
    ALL = "all"


class SyncRequest(BaseModel):
    window: Window = Window.SIX_MONTHS


class SyncRun(BaseModel):
    id: str
    status: str
    stage: str
    window: Window
    processed_markets: int = 0
    total_markets: int = 0
    progress_percent: float = 0
    raw_markets: int = 0
    raw_trades: int = 0
    breaker_open: bool = False
    breaker_seconds_remaining: int = 0
    error: str | None = None
    resumable: bool = False


class DataStatus(BaseModel):
    has_data: bool
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    last_successful_sync: datetime | None = None
    total_markets: int = 0
    total_trades: int = 0
    aggregate_markets: int = 0
    raw_markets: int = 0
    raw_trades: int = 0
    dataset_version: str = "2"
    scope: str = "empty"
    mve_excluded: bool = True
    legacy_cache_error: str | None = None
    storage_bytes: int = 0
    storage_limit_bytes: int | None = None


class HistorySummary(BaseModel):
    window: Window
    threshold: int = Field(ge=50, le=99)
    settled_markets: int
    crossed_markets: int
    wrong_markets: int
    miss_rate: float | None


class Band(BaseModel):
    min_percent: int
    max_percent: int
    label: str
    count: int


class BandsResponse(BaseModel):
    items: list[Band]


class Miss(BaseModel):
    ticker: str
    event_ticker: str | None = None
    title: str
    peak_confidence: int
    losing_side: str
    first_crossed_at: datetime | None = None
    settled_at: datetime


class MissesResponse(BaseModel):
    items: list[Miss]
    page: int
    page_size: int
    total: int
    pages: int
