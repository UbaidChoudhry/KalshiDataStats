from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Window(StrEnum):
    THREE_MONTHS = "3m"
    SIX_MONTHS = "6m"
    ONE_YEAR = "1y"
    ALL = "all"


class OpenMarketHorizon(StrEnum):
    ONE_DAY = "24h"
    THREE_DAYS = "3d"
    SEVEN_DAYS = "7d"
    FOURTEEN_DAYS = "14d"


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


class OpenMarket(BaseModel):
    ticker: str
    event_ticker: str | None = None
    category: str = "Other"
    title: str
    subtitle: str | None = None
    qualifying_side: Literal["yes", "no", "both"]
    qualifying_label: str
    qualifying_bid_percent: float
    yes_bid_percent: float | None = None
    no_bid_percent: float | None = None
    volume_24h: float | None = None
    liquidity_dollars: float | None = None
    close_at: datetime
    can_close_early: bool = False


class OpenMarketLink(BaseModel):
    """The canonical Kalshi event page containing a selected market."""

    url: str


class OpenMarketsResponse(BaseModel):
    """A non-persistent, point-in-time view of qualifying open contracts."""

    items: list[OpenMarket]
    page: int
    page_size: int
    total: int
    pages: int
    scanned_markets: int
    matching_markets: int
    category_counts: dict[str, int] = Field(default_factory=dict)
    closing_soon_markets: int = 0
    as_of: datetime
    stale: bool = False
    refresh_state: str
    breaker_seconds_remaining: int = Field(ge=0)
    next_close_at: datetime | None = None
    highest_bid: float | None = None


class OpenMarketsError(BaseModel):
    code: Literal["open_markets_unavailable"] = "open_markets_unavailable"
    message: str
    resumable: bool = True


class OpenMarketsErrorResponse(BaseModel):
    error: OpenMarketsError
    breaker_seconds_remaining: int = Field(ge=0)
