from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.parquet_dir = data_dir / "trades"
        self.staging_dir = data_dir / "staging"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.parquet_dir.mkdir(exist_ok=True)
        self.staging_dir.mkdir(exist_ok=True)
        self.db = duckdb.connect(str(data_dir / "kalshi.duckdb"))
        self._schema()

    def _schema(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS markets (
              ticker VARCHAR PRIMARY KEY, event_ticker VARCHAR, title VARCHAR NOT NULL,
              settlement_value DOUBLE, settled_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ,
              market_type VARCHAR NOT NULL DEFAULT 'binary', source_fingerprint VARCHAR,
              synced_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aggregates (
              ticker VARCHAR PRIMARY KEY, yes_peak INTEGER, no_peak INTEGER,
              yes_peak_at TIMESTAMPTZ, no_peak_at TIMESTAMPTZ,
              yes_first_crossed JSON, no_first_crossed JSON, trade_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sync_runs (
              id VARCHAR PRIMARY KEY, status VARCHAR NOT NULL, stage VARCHAR NOT NULL, "window" VARCHAR NOT NULL,
              processed_markets INTEGER NOT NULL DEFAULT 0, total_markets INTEGER NOT NULL DEFAULT 0,
              error VARCHAR, resumable BOOLEAN NOT NULL DEFAULT FALSE, started_at TIMESTAMPTZ NOT NULL,
              finished_at TIMESTAMPTZ, checkpoint JSON
            );
        """)
        # Safe migration for data directories created by earlier local versions.
        self.db.execute("ALTER TABLE markets ADD COLUMN IF NOT EXISTS source_fingerprint VARCHAR")

    def close(self) -> None:
        self.db.close()

    def upsert_markets(self, markets: list[dict[str, Any]]) -> None:
        if not markets:
            return
        rows = []
        now = datetime.now(UTC)
        for market in markets:
            value = market.get("settlement_value_dollars", market.get("settlement_value"))
            value = float(value) if value is not None else None
            rows.append((
                market["ticker"], market.get("event_ticker"), market.get("title") or market["ticker"], value,
                parse_time(market.get("settlement_ts") or market.get("settled_at")),
                parse_time(market.get("updated_time")), market.get("market_type", "binary"),
                market_fingerprint(market), now,
            ))
        self.db.executemany("""
          INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(ticker) DO UPDATE SET event_ticker=excluded.event_ticker, title=excluded.title,
          settlement_value=excluded.settlement_value, settled_at=excluded.settled_at, updated_at=excluded.updated_at,
          market_type=excluded.market_type, source_fingerprint=excluded.source_fingerprint,
          synced_at=excluded.synced_at
        """, rows)

    def is_market_complete_and_unchanged(self, market: dict[str, Any]) -> bool:
        """True only once normalized trades/aggregate are durably present for this source version."""
        row = self.db.execute("""
          SELECT EXISTS(SELECT 1 FROM aggregates a WHERE a.ticker = m.ticker)
          FROM markets m WHERE m.ticker = ? AND m.source_fingerprint = ?
        """, [market["ticker"], market_fingerprint(market)]).fetchone()
        return bool(row and row[0])

    def completed_unchanged_tickers(self, markets: list[dict[str, Any]]) -> set[str]:
        """Snapshot completed source versions before catalog upserts mutate stored fingerprints."""
        unchanged: set[str] = set()
        for market in markets:
            if self.is_market_complete_and_unchanged(market):
                unchanged.add(market["ticker"])
        return unchanged

    def replace_market_trades(self, market: dict[str, Any], trades: list[dict[str, Any]]) -> None:
        ticker = market["ticker"]
        eligible = [t for t in trades if not t.get("is_block_trade", False)]
        deduped = {str(t.get("trade_id", f"{ticker}:{i}")): t for i, t in enumerate(eligible)}
        eligible = list(deduped.values())
        settlement = parse_time(market.get("settlement_ts") or market.get("settled_at"))
        partition = self.parquet_dir / f"settled_year={settlement.year}" / f"settled_month={settlement.month:02d}"
        parquet_name = f"{hashlib.sha256(ticker.encode()).hexdigest()}.parquet"
        stage = self.staging_dir / parquet_name
        stage.parent.mkdir(exist_ok=True)
        normalized = [{
            "trade_id": str(t.get("trade_id", "")), "ticker": ticker,
            "yes_price": price_percent(t.get("yes_price_dollars", t.get("yes_price"))),
            "no_price": price_percent(t.get("no_price_dollars", t.get("no_price"))),
            "created_at": parse_time(t.get("created_time") or t.get("created_at")),
        } for t in eligible]
        if normalized:
            pl.DataFrame(normalized).write_parquet(stage, compression="zstd")
            partition.mkdir(parents=True, exist_ok=True)
            os.replace(stage, partition / parquet_name)
        else:
            (partition / parquet_name).unlink(missing_ok=True)
        yes = extrema(normalized, "yes_price")
        no = extrema(normalized, "no_price")
        self.db.execute("""
            INSERT INTO aggregates VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET yes_peak=excluded.yes_peak, no_peak=excluded.no_peak,
              yes_peak_at=excluded.yes_peak_at, no_peak_at=excluded.no_peak_at,
              yes_first_crossed=excluded.yes_first_crossed, no_first_crossed=excluded.no_first_crossed,
              trade_count=excluded.trade_count
        """, [ticker, yes[0], no[0], yes[1], no[1], json.dumps(first_crossings(normalized, "yes_price")),
               json.dumps(first_crossings(normalized, "no_price")), len(normalized)])

    def start_run(self, run_id: str, window: str) -> None:
        self.db.execute("INSERT INTO sync_runs VALUES (?, 'queued', 'queued', ?, 0, 0, NULL, FALSE, ?, NULL, NULL)",
                        [run_id, window, datetime.now(UTC)])

    def update_run(self, run_id: str, **values: Any) -> None:
        if not values:
            return
        allowed = {"status", "stage", "processed_markets", "total_markets", "error", "resumable", "checkpoint", "finished_at"}
        values = {k: v for k, v in values.items() if k in allowed}
        if "checkpoint" in values:
            values["checkpoint"] = json.dumps(values["checkpoint"])
        set_clause = ", ".join(f"{key} = ?" for key in values)
        self.db.execute(f"UPDATE sync_runs SET {set_clause} WHERE id = ?", [*values.values(), run_id])

    def checkpoint(self, run_id: str) -> dict[str, Any]:
        value = self.db.execute("SELECT checkpoint FROM sync_runs WHERE id = ?", [run_id]).fetchone()
        if not value or value[0] is None:
            return {}
        return json.loads(value[0]) if isinstance(value[0], str) else value[0]

    def append_staged_trades(self, run_id: str, ticker: str, trades: list[dict[str, Any]]) -> None:
        artifact = self._staging_artifact(run_id, ticker)
        if not trades:
            return
        artifact.parent.mkdir(parents=True, exist_ok=True)
        with artifact.open("a", encoding="utf-8") as handle:
            for trade in trades:
                handle.write(json.dumps(trade, default=str, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def staged_trades(self, run_id: str, ticker: str) -> list[dict[str, Any]]:
        artifact = self._staging_artifact(run_id, ticker)
        if not artifact.exists():
            return []
        with artifact.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def clear_staged_trades(self, run_id: str, ticker: str) -> None:
        artifact = self._staging_artifact(run_id, ticker)
        artifact.unlink(missing_ok=True)
        try:
            artifact.parent.rmdir()
        except OSError:
            pass

    def append_staged_catalog(self, run_id: str, markets: list[dict[str, Any]]) -> None:
        if not markets:
            return
        artifact = self._catalog_artifact(run_id)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        with artifact.open("a", encoding="utf-8") as handle:
            for market in markets:
                handle.write(json.dumps(market, default=str, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def staged_catalog(self, run_id: str) -> list[dict[str, Any]]:
        artifact = self._catalog_artifact(run_id)
        if not artifact.exists():
            return []
        with artifact.open(encoding="utf-8") as handle:
            deduped = {
                market["ticker"]: market
                for line in handle
                if line.strip()
                for market in [json.loads(line)]
            }
        return list(deduped.values())

    def clear_staged_catalog(self, run_id: str) -> None:
        artifact = self._catalog_artifact(run_id)
        artifact.unlink(missing_ok=True)
        try:
            artifact.parent.rmdir()
        except OSError:
            pass

    def _staging_artifact(self, run_id: str, ticker: str) -> Path:
        """Keep untrusted API tickers and stored run IDs from escaping the staging root."""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("Invalid sync run identifier for staging path")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", ticker):
            raise ValueError("Invalid Kalshi ticker for staging path")
        artifact = (self.staging_dir / run_id / f"{ticker}.ndjson").resolve()
        try:
            artifact.relative_to(self.staging_dir.resolve())
        except ValueError as exc:
            raise ValueError("Staging path escapes configured data directory") from exc
        return artifact

    def _catalog_artifact(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("Invalid sync run identifier for staging path")
        return self.staging_dir / run_id / "catalog.ndjson"

    def current_run(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        keys = [d[0] for d in self.db.description]
        return dict(zip(keys, row, strict=True))

    def status(self) -> dict[str, Any]:
        row = self.db.execute("""
          SELECT count(*), coalesce(sum(a.trade_count), 0), min(m.settled_at), max(m.settled_at)
          FROM markets m LEFT JOIN aggregates a USING(ticker)
        """).fetchone()
        last_success = self.db.execute(
            "SELECT max(finished_at) FROM sync_runs WHERE status = 'completed'"
        ).fetchone()[0]
        return {"has_data": bool(row[0]), "total_markets": row[0], "total_trades": row[1],
                "coverage_start": row[2], "coverage_end": row[3], "last_successful_sync": last_success}


def parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def price_percent(value: Any) -> int:
    numeric = float(value or 0)
    return round(numeric * 100) if numeric <= 1 else round(numeric)


def extrema(trades: list[dict[str, Any]], field: str) -> tuple[int | None, datetime | None]:
    if not trades:
        return None, None
    top = max(trades, key=lambda t: (t[field], -t["created_at"].timestamp()))
    return top[field], top["created_at"]


def first_crossings(trades: list[dict[str, Any]], field: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for threshold in range(50, 100):
        matching = [t["created_at"] for t in trades if t[field] >= threshold]
        if matching:
            result[str(threshold)] = min(matching).isoformat()
    return result


def market_fingerprint(market: dict[str, Any]) -> str:
    """Use Kalshi's update timestamp when available, with settlement fields as a stable fallback."""
    source = {
        "ticker": market.get("ticker"),
        "updated_time": market.get("updated_time"),
        "settlement_ts": market.get("settlement_ts") or market.get("settled_at"),
        "settlement_value": market.get("settlement_value_dollars", market.get("settlement_value")),
        "market_type": market.get("market_type", "binary"),
    }
    encoded = json.dumps(source, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
