from datetime import UTC, datetime, timedelta

from backend.app import analytics
from backend.app.models import Window
from backend.app.storage import Store


def seeded_store(tmp_path):
    store = Store(tmp_path / "data")
    markets = [
        {"ticker": "YES-WRONG", "title": "Yes loses", "settlement_value": 0, "settled_at": datetime.now(UTC)},
        {"ticker": "NO-WRONG", "title": "No loses", "settlement_value": 1, "settled_at": datetime.now(UTC)},
        {"ticker": "NO-TRADES", "title": "No eligible trades", "settlement_value": 1, "settled_at": datetime.now(UTC)},
        {"ticker": "SCALAR", "title": "Excluded", "settlement_value": 0, "settled_at": datetime.now(UTC), "market_type": "scalar"},
    ]
    store.upsert_markets(markets)
    store.replace_market_trades(markets[0], [{"trade_id": "1", "yes_price": .8, "no_price": .2, "created_at": datetime.now(UTC)}, {"trade_id": "block", "yes_price": .99, "is_block_trade": True}])
    store.replace_market_trades(markets[1], [{"trade_id": "2", "yes_price": .25, "no_price": .9, "created_at": datetime.now(UTC)}, {"trade_id": "2", "yes_price": .25, "no_price": .9, "created_at": datetime.now(UTC)}])
    return store


def test_counts_both_losing_sides_and_no_trade_markets(tmp_path):
    store = seeded_store(tmp_path)
    result = analytics.summary(store, Window.ALL, 80)
    assert result == {"window": Window.ALL, "threshold": 80, "settled_markets": 3, "crossed_markets": 2, "wrong_markets": 2, "miss_rate": 1.0}
    assert [band["count"] for band in analytics.bands(store, Window.ALL, 80)] == [1, 0, 1, 0]
    rows = analytics.misses(store, Window.ALL, 80, 90, 94, 1, 50, "peak_confidence", "desc")
    assert rows["total"] == 1 and rows["items"][0]["ticker"] == "NO-WRONG"
    by_side = analytics.misses(store, Window.ALL, 80, None, None, 1, 50, "losing_side", "asc")
    assert [item["losing_side"] for item in by_side["items"]] == ["NO", "YES"]
    by_crossed = analytics.misses(store, Window.ALL, 80, None, None, 1, 50, "first_crossed_at", "asc")
    assert by_crossed["items"][0]["first_crossed_at"] is not None
    store.close()


def test_custom_band_is_clipped_and_threshold_is_inclusive(tmp_path):
    store = seeded_store(tmp_path)
    result = analytics.summary(store, Window.ALL, 80)
    assert result["wrong_markets"] == 2
    assert analytics.bands(store, Window.ALL, 87)[0] == {"min_percent": 87, "max_percent": 89, "label": "87–89%", "count": 0}
    store.close()


def test_settlement_windows_and_mve_inclusion(tmp_path):
    store = Store(tmp_path / "data")
    now = datetime.now(UTC)
    markets = [
        {"ticker": "RECENT-MVE", "title": "Recent combo", "settlement_value": 0,
         "settled_at": now - timedelta(days=20), "market_type": "mve"},
        {"ticker": "OLDER", "title": "Older binary", "settlement_value": 1,
         "settled_at": now - timedelta(days=120)},
        {"ticker": "OLD", "title": "Old binary", "settlement_value": 0,
         "settled_at": now - timedelta(days=400)},
    ]
    store.upsert_markets(markets)
    for index, market in enumerate(markets):
        store.replace_market_trades(market, [{"trade_id": str(index), "yes_price": .9,
                                              "no_price": .9, "created_at": now}])
    assert analytics.summary(store, Window.THREE_MONTHS, 80)["settled_markets"] == 1
    assert analytics.summary(store, Window.SIX_MONTHS, 80)["settled_markets"] == 2
    assert analytics.summary(store, Window.ONE_YEAR, 80)["settled_markets"] == 2
    assert analytics.summary(store, Window.ALL, 80)["settled_markets"] == 3
    assert analytics.summary(store, Window.THREE_MONTHS, 80)["wrong_markets"] == 1
    store.close()


def test_window_boundaries_use_rolling_calendar_months():
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    assert analytics.window_start(Window.THREE_MONTHS, now) == datetime(2026, 5, 31, 12, tzinfo=UTC)
    assert analytics.window_start(Window.SIX_MONTHS, now) == datetime(2026, 2, 28, 12, tzinfo=UTC)
    assert analytics.window_start(Window.ONE_YEAR, now) == datetime(2025, 8, 31, 12, tzinfo=UTC)
