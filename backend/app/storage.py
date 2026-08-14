from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl


class StorageLimitExceeded(RuntimeError):
    pass


class LegacyDatasetError(RuntimeError):
    """Raised instead of silently mixing an older on-disk cache with a new format."""

    pass


DATASET_VERSION = "2"


class Store:
    def __init__(self, data_dir: Path, max_storage_bytes: int | None = None):
        self.data_dir = data_dir
        self.max_storage_bytes = max_storage_bytes
        self.parquet_dir = data_dir / "trades"
        self.staging_dir = data_dir / "staging"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.parquet_dir.mkdir(exist_ok=True)
        self.staging_dir.mkdir(exist_ok=True)
        self.db = duckdb.connect(str(data_dir / "kalshi.duckdb"))
        self._schema()
        # Re-walking a data directory with millions of raw rows for every API
        # page is itself a laptop-scale bottleneck. Reserve conservative bytes
        # in process and refresh the exact value only for status/publish.
        self._reserved_storage_bytes = self._scan_storage_bytes()

    def _schema(self) -> None:
        # Do this check before adding our marker. Earlier versions used the same
        # tables but did not have a dataset metadata table, so continuing would
        # make the result impossible to reason about.
        existing_tables = {
            row[0]
            for row in self.db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        is_legacy = "markets" in existing_tables and "data_metadata" not in existing_tables
        if is_legacy:
            legacy_rows = self.db.execute("SELECT count(*) FROM markets").fetchone()[0]
            if legacy_rows:
                raise LegacyDatasetError(
                    "This local cache was created by an older Kalshi Data Stats format. "
                    "Clear the configured data directory, then reload; existing data was not changed."
                )
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS data_metadata (
              key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL
            );
            CREATE TABLE IF NOT EXISTS markets (
              ticker VARCHAR PRIMARY KEY, event_ticker VARCHAR, title VARCHAR NOT NULL,
              settlement_value DOUBLE, settled_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ,
              market_type VARCHAR NOT NULL DEFAULT 'binary', source_fingerprint VARCHAR,
              synced_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aggregates (
              ticker VARCHAR PRIMARY KEY, yes_peak INTEGER, no_peak INTEGER,
              yes_peak_at TIMESTAMPTZ, no_peak_at TIMESTAMPTZ,
              yes_first_crossed JSON, no_first_crossed JSON, trade_count INTEGER NOT NULL DEFAULT 0,
              raw_retained BOOLEAN NOT NULL DEFAULT FALSE, raw_trade_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sync_runs (
              id VARCHAR PRIMARY KEY, status VARCHAR NOT NULL, stage VARCHAR NOT NULL, "window" VARCHAR NOT NULL,
              processed_markets INTEGER NOT NULL DEFAULT 0, total_markets INTEGER NOT NULL DEFAULT 0,
              error VARCHAR, resumable BOOLEAN NOT NULL DEFAULT FALSE, started_at TIMESTAMPTZ NOT NULL,
              finished_at TIMESTAMPTZ, checkpoint JSON
            );
            CREATE TABLE IF NOT EXISTS run_markets (
              run_id VARCHAR NOT NULL, ticker VARCHAR NOT NULL, event_ticker VARCHAR,
              title VARCHAR NOT NULL, settlement_value DOUBLE, settled_at TIMESTAMPTZ NOT NULL,
              updated_at TIMESTAMPTZ, market_type VARCHAR NOT NULL DEFAULT 'binary',
              source_fingerprint VARCHAR NOT NULL, reported_volume DOUBLE,
              PRIMARY KEY(run_id, ticker)
            );
            CREATE TABLE IF NOT EXISTS run_aggregates (
              run_id VARCHAR NOT NULL, ticker VARCHAR NOT NULL, source_fingerprint VARCHAR NOT NULL,
              yes_peak INTEGER, no_peak INTEGER, yes_peak_at TIMESTAMPTZ, no_peak_at TIMESTAMPTZ,
              yes_first_crossed JSON, no_first_crossed JSON, trade_count INTEGER NOT NULL DEFAULT 0,
              raw_retained BOOLEAN NOT NULL DEFAULT FALSE, raw_trade_count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(run_id, ticker)
            );
        """)
        # Safe migration for data directories created by earlier local versions.
        self.db.execute("ALTER TABLE markets ADD COLUMN IF NOT EXISTS source_fingerprint VARCHAR")
        # DuckDB does not support adding a constrained column. Existing caches
        # are rejected above, but these remain harmless for empty transitional DBs.
        self.db.execute("ALTER TABLE aggregates ADD COLUMN IF NOT EXISTS raw_retained BOOLEAN DEFAULT FALSE")
        self.db.execute("ALTER TABLE aggregates ADD COLUMN IF NOT EXISTS raw_trade_count INTEGER DEFAULT 0")
        version = self.db.execute(
            "SELECT value FROM data_metadata WHERE key = 'dataset_version'"
        ).fetchone()
        if version and version[0] != DATASET_VERSION:
            raise LegacyDatasetError(
                "This local cache uses an unsupported Kalshi Data Stats format. "
                "Clear the configured data directory, then reload; existing data was not changed."
            )
        if not version:
            self.db.execute(
                "INSERT INTO data_metadata VALUES ('dataset_version', ?)", [DATASET_VERSION]
            )

    @staticmethod
    def _market_row(market: dict[str, Any]) -> tuple[Any, ...]:
        value = market.get("settlement_value_dollars", market.get("settlement_value"))
        value = float(value) if value is not None else None
        volume = market.get("volume", market.get("volume_fp"))
        try:
            volume = float(volume) if volume is not None else None
        except (TypeError, ValueError):
            volume = None
        return (
            market["ticker"], market.get("event_ticker"), market.get("title") or market["ticker"], value,
            parse_time(market.get("settlement_ts") or market.get("settled_at")),
            parse_time(market.get("updated_time")), market.get("market_type", "binary"),
            market_fingerprint(market), volume,
        )

    def close(self) -> None:
        self.db.close()

    def upsert_markets(self, markets: list[dict[str, Any]]) -> None:
        if not markets:
            return
        rows = []
        now = datetime.now(UTC)
        for market in markets:
            rows.append((*self._market_row(market)[:-1], now))
        self.db.executemany("""
          INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(ticker) DO UPDATE SET event_ticker=excluded.event_ticker, title=excluded.title,
          settlement_value=excluded.settlement_value, settled_at=excluded.settled_at, updated_at=excluded.updated_at,
          market_type=excluded.market_type, source_fingerprint=excluded.source_fingerprint,
          synced_at=excluded.synced_at
        """, rows)

    def stage_catalog_page(self, run_id: str, markets: list[dict[str, Any]]) -> None:
        """Durably stage one API page in DuckDB; never materialize the whole catalog."""
        if not markets:
            return
        # DuckDB owns the staging table, but reserve a conservative representation
        # before inserting so a capped laptop never gets an unbounded catalog write.
        self._ensure_can_write(sum(len(json.dumps(market, default=str)) for market in markets))
        rows = [(run_id, *self._market_row(market)) for market in markets]
        self.db.executemany("""
          INSERT INTO run_markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(run_id, ticker) DO UPDATE SET event_ticker=excluded.event_ticker,
            title=excluded.title, settlement_value=excluded.settlement_value,
            settled_at=excluded.settled_at, updated_at=excluded.updated_at,
            market_type=excluded.market_type, source_fingerprint=excluded.source_fingerprint,
            reported_volume=excluded.reported_volume
        """, rows)

    def run_market_count(self, run_id: str) -> int:
        return int(self.db.execute(
            "SELECT count(*) FROM run_markets WHERE run_id = ?", [run_id]
        ).fetchone()[0])

    def run_raw_progress(self, run_id: str) -> tuple[int, int]:
        row = self.db.execute("""
          SELECT coalesce(sum(CASE WHEN raw_retained THEN 1 ELSE 0 END), 0),
                 coalesce(sum(raw_trade_count), 0)
          FROM run_aggregates WHERE run_id = ?
        """, [run_id]).fetchone()
        return int(row[0]), int(row[1])

    def iter_run_markets(self, run_id: str, batch_size: int = 250):
        """Yield bounded batches in a stable order suitable for restart-safe processing."""
        last_ticker = ""
        while True:
            rows = self.db.execute("""
              SELECT ticker, event_ticker, title, settlement_value, settled_at, updated_at,
                     market_type, source_fingerprint, reported_volume
              FROM run_markets
              WHERE run_id = ? AND ticker > ?
              ORDER BY ticker LIMIT ?
            """, [run_id, last_ticker, batch_size]).fetchall()
            if not rows:
                return
            for row in rows:
                last_ticker = row[0]
                yield {
                    "ticker": row[0], "event_ticker": row[1], "title": row[2],
                    "settlement_value": row[3], "settled_at": row[4], "updated_time": row[5],
                    "market_type": row[6], "source_fingerprint": row[7],
                    "reported_volume": row[8],
                }

    def run_market_is_complete(self, run_id: str, market: dict[str, Any]) -> bool:
        row = self.db.execute("""
          SELECT 1 FROM run_aggregates WHERE run_id = ? AND ticker = ? AND source_fingerprint = ?
        """, [run_id, market["ticker"], market.get("source_fingerprint", market_fingerprint(market))]).fetchone()
        return row is not None

    def append_market_trade_page(
        self, run_id: str, ticker: str, trades: list[dict[str, Any]]
    ) -> None:
        """Append one page for one active market. This is intentionally the only raw
        staging kept in memory/on disk until we know whether the market is a miss."""
        normalized = []
        for index, trade in enumerate(trades):
            if trade.get("is_block_trade", False):
                continue
            normalized.append({
                "trade_id": str(trade.get("trade_id", f"{ticker}:{index}")),
                "ticker": ticker,
                "yes_price": price_percent(trade.get("yes_price_dollars", trade.get("yes_price"))),
                "no_price": price_percent(trade.get("no_price_dollars", trade.get("no_price"))),
                "created_at": parse_time(trade.get("created_time") or trade.get("created_at")).isoformat(),
            })
        self.append_staged_trades(run_id, ticker, normalized)

    def finalize_run_market(self, run_id: str, market: dict[str, Any]) -> None:
        """Compute exact extrema from one market's staged pages and retain raw parquet
        only if its eventual losing side reached 50%.  DuckDB streams the input in
        batches, avoiding a Python list of trades."""
        ticker = market["ticker"]
        artifact = self._staging_artifact(run_id, ticker)
        yes_peak: int | None = None
        no_peak: int | None = None
        yes_peak_at: datetime | None = None
        no_peak_at: datetime | None = None
        yes_cross: dict[str, str] = {}
        no_cross: dict[str, str] = {}
        trade_count = 0
        if artifact.exists() and artifact.stat().st_size:
            # Deduplicate at the SQL boundary. The cursor is consumed in small
            # batches so the largest allocation is one result batch, not a market.
            query = """
              SELECT trade_id, yes_price, no_price, CAST(created_at AS TIMESTAMPTZ) AS created_at
              FROM (
                SELECT *, row_number() OVER (PARTITION BY trade_id ORDER BY created_at) AS row_number
                FROM read_json_auto(?)
              ) WHERE row_number = 1
              ORDER BY created_at ASC
            """
            cursor = self.db.execute(query, [str(artifact)])
            while rows := cursor.fetchmany(500):
                for _trade_id, yes, no, created_at in rows:
                    trade_count += 1
                    yes = int(yes)
                    no = int(no)
                    if yes_peak is None or yes > yes_peak:
                        yes_peak, yes_peak_at = yes, created_at
                    if no_peak is None or no > no_peak:
                        no_peak, no_peak_at = no, created_at
                    for threshold in range(50, 100):
                        key = str(threshold)
                        if yes >= threshold and key not in yes_cross:
                            yes_cross[key] = created_at.isoformat()
                        if no >= threshold and key not in no_cross:
                            no_cross[key] = created_at.isoformat()
        settlement_value = market.get("settlement_value_dollars", market.get("settlement_value"))
        losing_yes = float(settlement_value) == 0
        losing_peak = yes_peak if losing_yes else no_peak
        raw_retained = bool(losing_peak is not None and losing_peak >= 50)
        raw_trade_count = trade_count if raw_retained else 0
        if raw_retained and artifact.exists():
            raw_target = self._run_raw_artifact(run_id, market)
            raw_target.parent.mkdir(parents=True, exist_ok=True)
            # The NDJSON exists while COPY writes its compressed replacement, so
            # reserve its full size before doing work rather than briefly exceeding
            # a configured local cap.
            self._ensure_can_write(artifact.stat().st_size)
            temp = raw_target.with_suffix(".tmp.parquet")
            escaped = str(temp).replace("'", "''")
            self.db.execute(f"""
              COPY (
                SELECT trade_id, ticker, yes_price, no_price, CAST(created_at AS TIMESTAMPTZ) AS created_at
                FROM (
                  SELECT *, row_number() OVER (PARTITION BY trade_id ORDER BY created_at) AS row_number
                  FROM read_json_auto(?)
                ) WHERE row_number = 1
              ) TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """, [str(artifact)])
            os.replace(temp, raw_target)
        self.clear_staged_trades(run_id, ticker)
        self.db.execute("""
          INSERT INTO run_aggregates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(run_id, ticker) DO UPDATE SET source_fingerprint=excluded.source_fingerprint,
            yes_peak=excluded.yes_peak, no_peak=excluded.no_peak,
            yes_peak_at=excluded.yes_peak_at, no_peak_at=excluded.no_peak_at,
            yes_first_crossed=excluded.yes_first_crossed, no_first_crossed=excluded.no_first_crossed,
            trade_count=excluded.trade_count, raw_retained=excluded.raw_retained,
            raw_trade_count=excluded.raw_trade_count
        """, [
            run_id, ticker, market.get("source_fingerprint", market_fingerprint(market)), yes_peak, no_peak,
            yes_peak_at, no_peak_at, json.dumps(yes_cross), json.dumps(no_cross), trade_count,
            raw_retained, raw_trade_count,
        ])

    def publish_run(self, run_id: str, window: str) -> None:
        """Make a fully staged run visible in one database transaction.

        Until this method is called, the dashboard continues to query the prior
        completed dataset.  A failed/cancelled run therefore cannot expose a
        partial catalog.
        """
        now = datetime.now(UTC)
        self.db.execute("BEGIN TRANSACTION")
        try:
            if window != "all":
                # A selected window is a deliberately bounded local cache.
                self.db.execute("DELETE FROM aggregates")
                self.db.execute("DELETE FROM markets")
            self.db.execute("""
              INSERT INTO markets (
                ticker, event_ticker, title, settlement_value, settled_at, updated_at,
                market_type, source_fingerprint, synced_at
              )
              SELECT ticker, event_ticker, title, settlement_value, settled_at, updated_at,
                     market_type, source_fingerprint, ?
              FROM run_markets WHERE run_id = ?
              ON CONFLICT(ticker) DO UPDATE SET event_ticker=excluded.event_ticker,
                title=excluded.title, settlement_value=excluded.settlement_value,
                settled_at=excluded.settled_at, updated_at=excluded.updated_at,
                market_type=excluded.market_type, source_fingerprint=excluded.source_fingerprint,
                synced_at=excluded.synced_at
            """, [now, run_id])
            self.db.execute("""
              INSERT INTO aggregates (
                ticker, yes_peak, no_peak, yes_peak_at, no_peak_at,
                yes_first_crossed, no_first_crossed, trade_count, raw_retained, raw_trade_count
              )
              SELECT ticker, yes_peak, no_peak, yes_peak_at, no_peak_at,
                     yes_first_crossed, no_first_crossed, trade_count, raw_retained, raw_trade_count
              FROM run_aggregates WHERE run_id = ?
              ON CONFLICT(ticker) DO UPDATE SET yes_peak=excluded.yes_peak,
                no_peak=excluded.no_peak, yes_peak_at=excluded.yes_peak_at,
                no_peak_at=excluded.no_peak_at, yes_first_crossed=excluded.yes_first_crossed,
                no_first_crossed=excluded.no_first_crossed, trade_count=excluded.trade_count,
                raw_retained=excluded.raw_retained, raw_trade_count=excluded.raw_trade_count
            """, [run_id])
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

        run_root = self.staging_dir / run_id
        raw_source = run_root / "raw"
        datasets = self.data_dir / "datasets"
        datasets.mkdir(exist_ok=True)
        raw_target = datasets / run_id
        if raw_source.exists():
            os.replace(raw_source, raw_target)
        else:
            raw_target.mkdir(exist_ok=True)
        if window != "all":
            for candidate in datasets.iterdir():
                if candidate != raw_target and candidate.is_dir() and not candidate.is_symlink():
                    shutil.rmtree(candidate)
            # Transitional/demo raw files used the old flat trades directory.
            # Prune them only after the new compact dataset has published.
            if self.parquet_dir.exists() and not self.parquet_dir.is_symlink():
                shutil.rmtree(self.parquet_dir)
                self.parquet_dir.mkdir(exist_ok=True)
        self.db.execute("""
          INSERT INTO data_metadata VALUES ('active_raw_dataset', ?)
          ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, [run_id])
        # These tables can be very large; they are only needed for a resumable run.
        self.db.execute("DELETE FROM run_aggregates WHERE run_id = ?", [run_id])
        self.db.execute("DELETE FROM run_markets WHERE run_id = ?", [run_id])
        if run_root.exists():
            shutil.rmtree(run_root)

    def _run_raw_artifact(self, run_id: str, market: dict[str, Any]) -> Path:
        return self._raw_artifact_in(self.staging_dir / run_id / "raw", market)

    @staticmethod
    def _raw_artifact_in(root: Path, market: dict[str, Any]) -> Path:
        settlement = parse_time(market.get("settlement_ts") or market.get("settled_at"))
        safe_ticker = hashlib.sha256(str(market["ticker"]).encode()).hexdigest()
        return (
            root /
            f"settled_year={settlement.year}" / f"settled_month={settlement.month:02d}" /
            f"{safe_ticker}.parquet"
        )

    def is_market_complete_and_unchanged(self, market: dict[str, Any]) -> bool:
        """True only once normalized trades/aggregate are durably present for this source version."""
        row = self.db.execute("""
          SELECT EXISTS(SELECT 1 FROM aggregates a WHERE a.ticker = m.ticker)
          FROM markets m WHERE m.ticker = ? AND m.source_fingerprint = ?
        """, [market["ticker"], market.get("source_fingerprint", market_fingerprint(market))]).fetchone()
        return bool(row and row[0])

    def copy_existing_market_to_run(self, run_id: str, market: dict[str, Any]) -> bool:
        """Reuse an unchanged compact aggregate without re-downloading trades.

        A selected-window publication replaces the active raw dataset directory,
        so a retained miss must be copied into the new run before it is safe to
        skip its trade endpoints. Return False when that raw asset is missing so
        the caller can rebuild it from Kalshi instead of silently losing it.
        """
        fingerprint = market.get("source_fingerprint", market_fingerprint(market))
        retained = self.db.execute(
            "SELECT coalesce(raw_retained, FALSE) FROM aggregates WHERE ticker = ?", [market["ticker"]]
        ).fetchone()
        if not retained:
            return False
        if retained[0] and not self._copy_existing_raw_to_run(run_id, market):
            return False
        self.db.execute("""
          INSERT INTO run_aggregates
          SELECT ?, a.ticker, ?, a.yes_peak, a.no_peak, a.yes_peak_at, a.no_peak_at,
                 a.yes_first_crossed, a.no_first_crossed, a.trade_count,
                 coalesce(a.raw_retained, FALSE), coalesce(a.raw_trade_count, 0)
          FROM aggregates a WHERE a.ticker = ?
          ON CONFLICT(run_id, ticker) DO NOTHING
        """, [run_id, fingerprint, market["ticker"]])
        return True

    def _copy_existing_raw_to_run(self, run_id: str, market: dict[str, Any]) -> bool:
        active = self.db.execute(
            "SELECT value FROM data_metadata WHERE key = 'active_raw_dataset'"
        ).fetchone()
        if not active or not re.fullmatch(r"[A-Za-z0-9_-]+", active[0]):
            return False
        source = self._raw_artifact_in(self.data_dir / "datasets" / active[0], market)
        if not source.exists():
            return False
        target = self._run_raw_artifact(run_id, market)
        if target.exists():
            return True
        self._ensure_can_write(source.stat().st_size)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return True

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
        normalized = [{
            "trade_id": str(t.get("trade_id", "")), "ticker": ticker,
            "yes_price": price_percent(t.get("yes_price_dollars", t.get("yes_price"))),
            "no_price": price_percent(t.get("no_price_dollars", t.get("no_price"))),
            "created_at": parse_time(t.get("created_time") or t.get("created_at")),
        } for t in eligible]
        if normalized:
            partition.mkdir(parents=True, exist_ok=True)
            target = partition / parquet_name
            descriptor, temporary_name = tempfile.mkstemp(prefix="kalshi-trades-", suffix=".parquet")
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                pl.DataFrame(normalized).write_parquet(temporary, compression="zstd")
                existing_size = target.stat().st_size if target.exists() else 0
                self._ensure_can_write(max(0, temporary.stat().st_size - existing_size))
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            (partition / parquet_name).unlink(missing_ok=True)
        yes = extrema(normalized, "yes_price")
        no = extrema(normalized, "no_price")
        self.db.execute("""
            INSERT INTO aggregates (
              ticker, yes_peak, no_peak, yes_peak_at, no_peak_at,
              yes_first_crossed, no_first_crossed, trade_count, raw_retained, raw_trade_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET yes_peak=excluded.yes_peak, no_peak=excluded.no_peak,
              yes_peak_at=excluded.yes_peak_at, no_peak_at=excluded.no_peak_at,
              yes_first_crossed=excluded.yes_first_crossed, no_first_crossed=excluded.no_first_crossed,
              trade_count=excluded.trade_count, raw_retained=excluded.raw_retained,
              raw_trade_count=excluded.raw_trade_count
        """, [ticker, yes[0], no[0], yes[1], no[1], json.dumps(first_crossings(normalized, "yes_price")),
               json.dumps(first_crossings(normalized, "no_price")), len(normalized), bool(normalized), len(normalized)])

    def start_run(self, run_id: str, window: str) -> None:
        self.db.execute("INSERT INTO sync_runs VALUES (?, 'queued', 'queued', ?, 0, 0, NULL, FALSE, ?, NULL, NULL)",
                        [run_id, window, datetime.now(UTC)])

    def mark_interrupted_runs_resumable(self) -> None:
        """A process restart stops asyncio tasks; retain their durable checkpoints for reload."""
        self.db.execute("""
          UPDATE sync_runs
          SET status = 'failed_resumable', stage = 'interrupted',
              error = 'The app restarted. Download progress is saved locally.', resumable = TRUE
          WHERE status IN ('queued', 'running', 'breaker_open')
        """)

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
        # This public helper is also used by older checkpoint tests. Normalize
        # legacy API-shaped rows at the boundary so the finalizer has one schema.
        normalized = []
        for index, trade in enumerate(trades):
            if "yes_price" in trade and "created_at" in trade:
                normalized.append(trade)
            elif not trade.get("is_block_trade", False):
                normalized.append({
                    "trade_id": str(trade.get("trade_id", f"{ticker}:{index}")), "ticker": ticker,
                    "yes_price": price_percent(trade.get("yes_price_dollars", trade.get("yes_price"))),
                    "no_price": price_percent(trade.get("no_price_dollars", trade.get("no_price"))),
                    "created_at": parse_time(trade.get("created_time") or trade.get("created_at")).isoformat(),
                })
        encoded = "".join(json.dumps(trade, default=str, separators=(",", ":")) + "\n" for trade in normalized)
        self._ensure_can_write(len(encoded.encode()))
        artifact.parent.mkdir(parents=True, exist_ok=True)
        with artifact.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
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
        size = artifact.stat().st_size if artifact.exists() else 0
        artifact.unlink(missing_ok=True)
        self._reserved_storage_bytes = max(0, self._reserved_storage_bytes - size)
        try:
            artifact.parent.rmdir()
        except OSError:
            pass

    def append_staged_catalog(self, run_id: str, markets: list[dict[str, Any]]) -> None:
        # Compatibility for old interrupted runs/tests. New ingestion uses only
        # the DuckDB staging table above and never calls this NDJSON path.
        self.stage_catalog_page(run_id, markets)
        if not markets:
            return
        artifact = self._catalog_artifact(run_id)
        encoded = "".join(json.dumps(market, default=str, separators=(",", ":")) + "\n" for market in markets)
        self._ensure_can_write(len(encoded.encode()))
        artifact.parent.mkdir(parents=True, exist_ok=True)
        with artifact.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
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
          SELECT count(*), coalesce(sum(a.trade_count), 0), min(m.settled_at), max(m.settled_at),
                 count(a.ticker), coalesce(sum(CASE WHEN a.raw_retained THEN 1 ELSE 0 END), 0),
                 coalesce(sum(a.raw_trade_count), 0)
          FROM markets m LEFT JOIN aggregates a USING(ticker)
        """).fetchone()
        last_success = self.db.execute(
            "SELECT max(finished_at) FROM sync_runs WHERE status = 'completed'"
        ).fetchone()[0]
        return {"has_data": bool(row[0]), "total_markets": row[0], "total_trades": row[1],
                "coverage_start": row[2], "coverage_end": row[3], "last_successful_sync": last_success,
                "aggregate_markets": row[4], "raw_markets": row[5], "raw_trades": row[6],
                "dataset_version": DATASET_VERSION, "scope": self.dataset_scope(),
                "storage_bytes": self.storage_bytes(), "storage_limit_bytes": self.max_storage_bytes}

    def dataset_scope(self) -> str:
        row = self.db.execute("""
          SELECT "window" FROM sync_runs WHERE status = 'completed'
          ORDER BY finished_at DESC LIMIT 1
        """).fetchone()
        return row[0] if row else "empty"

    def storage_bytes(self) -> int:
        self._reserved_storage_bytes = self._scan_storage_bytes()
        return self._reserved_storage_bytes

    def _scan_storage_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.data_dir.rglob("*") if path.is_file())

    def _ensure_can_write(self, additional_bytes: int) -> None:
        if self.max_storage_bytes is None:
            return
        used_bytes = self._reserved_storage_bytes
        if used_bytes + additional_bytes > self.max_storage_bytes:
            raise StorageLimitExceeded(
                "Local storage limit reached "
                f"({format_bytes(used_bytes)} used of {format_bytes(self.max_storage_bytes)}). "
                "Increase KALSHI_MAX_STORAGE_GB or clear local data before resuming."
            )
        self._reserved_storage_bytes += additional_bytes


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


def format_bytes(value: int) -> str:
    if value < 1024**2:
        return f"{value / 1024:.1f} KB"
    return f"{value / 1024**3:.2f} GB"


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
