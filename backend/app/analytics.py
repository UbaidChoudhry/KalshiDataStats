from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from dateutil.relativedelta import relativedelta

from .models import Window
from .storage import Store


def window_start(window: Window, now: datetime | None = None) -> datetime | None:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if window == Window.ALL:
        return None
    offsets = {
        Window.THREE_MONTHS: relativedelta(months=3),
        Window.SIX_MONTHS: relativedelta(months=6),
        Window.ONE_YEAR: relativedelta(years=1),
    }
    return now - offsets[window]


def qualified(store: Store, window: Window) -> list[dict[str, Any]]:
    start = window_start(window)
    sql = """
      SELECT m.ticker, m.event_ticker, m.title, m.settlement_value, m.settled_at,
             a.yes_peak, a.no_peak, a.yes_first_crossed, a.no_first_crossed
      FROM markets m LEFT JOIN aggregates a USING(ticker)
      WHERE m.settlement_value IN (0, 1) AND m.market_type != 'scalar'
    """
    params: list[Any] = []
    if start:
        sql += " AND m.settled_at >= ?"
        params.append(start)
    records = store.db.execute(sql, params).fetchall()
    fields = [d[0] for d in store.db.description]
    return [dict(zip(fields, row, strict=True)) for row in records]


def losing_values(record: dict[str, Any], threshold: int) -> tuple[int | None, str, datetime | None]:
    losing_side = "NO" if record["settlement_value"] == 1 else "YES"
    peak = record["no_peak"] if losing_side == "NO" else record["yes_peak"]
    crossings = record["no_first_crossed"] if losing_side == "NO" else record["yes_first_crossed"]
    if not crossings:
        return peak, losing_side, None
    raw = json.loads(crossings) if isinstance(crossings, str) else crossings
    value = raw.get(str(threshold))
    return peak, losing_side, datetime.fromisoformat(value) if value else None


def summary(store: Store, window: Window, threshold: int) -> dict[str, Any]:
    records = qualified(store, window)
    crossed = sum(1 for r in records if max(r["yes_peak"] or 0, r["no_peak"] or 0) >= threshold)
    wrong = sum(1 for r in records if (losing_values(r, threshold)[0] or 0) >= threshold)
    return {"window": window, "threshold": threshold, "settled_markets": len(records),
            "crossed_markets": crossed, "wrong_markets": wrong,
            "miss_rate": round(wrong / crossed, 6) if crossed else None}


def bands(store: Store, window: Window, threshold: int) -> list[dict[str, Any]]:
    records = qualified(store, window)
    result = []
    first_ceiling = min(99, ((threshold // 5) + 1) * 5 - 1)
    floors = [threshold, *range(first_ceiling + 1, 100, 5)]
    for floor in floors:
        ceiling = first_ceiling if floor == threshold else min(floor + 4, 99)
        count = sum(1 for r in records if floor <= (losing_values(r, threshold)[0] or -1) <= ceiling)
        result.append({"min_percent": floor, "max_percent": ceiling, "label": f"{floor}\u2013{ceiling}%", "count": count})
    return result


def misses(store: Store, window: Window, threshold: int, min_percent: int | None, max_percent: int | None,
           page: int, page_size: int, sort: str, direction: str) -> dict[str, Any]:
    entries = []
    for record in qualified(store, window):
        peak, side, first = losing_values(record, threshold)
        if peak is None or peak < threshold:
            continue
        if min_percent is not None and peak < min_percent:
            continue
        if max_percent is not None and peak > max_percent:
            continue
        entries.append({"ticker": record["ticker"], "event_ticker": record["event_ticker"], "title": record["title"],
                        "peak_confidence": peak, "losing_side": side, "first_crossed_at": first,
                        "settled_at": record["settled_at"]})
    allowed = {
        "peak_confidence",
        "settled_at",
        "first_crossed_at",
        "losing_side",
        "title",
        "ticker",
    }
    key = sort if sort in allowed else "peak_confidence"
    entries.sort(key=lambda item: (item[key] is None, item[key]), reverse=direction != "asc")
    total = len(entries)
    pages = math.ceil(total / page_size) if total else 0
    return {"items": entries[(page - 1) * page_size:page * page_size], "page": page,
            "page_size": page_size, "total": total, "pages": pages}
