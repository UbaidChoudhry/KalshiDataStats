# Forecast Lens

Forecast Lens is a local Kalshi analytics dashboard for finding historical mispredictions and nearby open markets with a high-confidence option.

## What it measures

A market is counted as a miss when any non-block trade prices the eventual losing side at or above the selected threshold. The 3-month, 6-month, and 1-year filters use settlement time, while every eligible trade across each included market's full lifetime is analyzed. Only ordinary finalized YES/NO markets are included; scalar and multivariate-event (combo) markets are excluded.

The historical dashboard includes summary statistics, five-point confidence bands, clickable bar-to-market drill-down, custom integer thresholds from 50% to 99%, sorting, and 50-row server-side pagination.

The Open markets page uses current best bids to find active ordinary YES/NO markets closing within 24 hours, 3 days, 7 days, or 14 days. It is always ordered from the soonest scheduled close to the latest, refreshes once per minute while visible, and never scans all future markets.

## Local setup

Requirements:

- macOS or Linux
- [uv](https://docs.astral.sh/uv/) (provisions Python 3.12)
- Node.js 22 or newer

Install and build:

```bash
uv sync --dev
npm --prefix frontend install
npm --prefix frontend run build
```

Copy `.env.example` to `.env` only if you want to override defaults. The application does not load secrets and does not require a Kalshi API key for these public market-data features.

Launch the production-style single process:

```bash
uv run python -m backend.app
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The first **Load data** operation scans the ordinary-market catalog and may take a while. It then shows downloaded markets out of the selected total. Progress and rate-limit pauses are shown in the dashboard, and interrupted work is resumable.

For deterministic offline development:

```bash
KALSHI_SYNC_MODE=demo KALSHI_DATA_DIR=/tmp/kalshi-data-stats-demo uv run python -m backend.app
```

For separate frontend/backend development, run `uv run fastapi dev backend/app/main.py --host 127.0.0.1` and `npm --prefix frontend run dev`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `KALSHI_BASE_URL` | `https://external-api.kalshi.com/trade-api/v2` | Public Kalshi API base URL |
| `KALSHI_REQUESTS_PER_SECOND` | `5` | Process-wide request pace; burst size is one |
| `KALSHI_MAX_STORAGE_GB` | *(blank)* | Optional local data cap in GiB; blank means no limit |
| `KALSHI_429_PAUSE_SECONDS` | `60` | Circuit-open interval after HTTP 429 |
| `KALSHI_429_MAX_PAUSES` | `3` | Maximum pause/probe cycles before a resumable failure |
| `KALSHI_DATA_DIR` | `~/Library/Application Support/KalshiDataStats` | DuckDB, Parquet, staging, and checkpoint location |
| `KALSHI_SYNC_MODE` | `real` | Set to `demo` for deterministic offline data |
| `KALSHI_HOST` / `KALSHI_PORT` | `127.0.0.1` / `8000` | Local server bind address |

The backend refuses to start when `KALSHI_DATA_DIR` resolves inside the repository. `.env`, key files, databases, Parquet files, staging data, logs, environments, dependencies, and build output are ignored by Git.

### Updating an older local cache

The compact-storage format is versioned. If the app reports that the existing local cache is from an older format, it will not mix the two datasets. Stop the app, move or remove the folder named by `KALSHI_DATA_DIR` (the default is `~/Library/Application Support/KalshiDataStats`), then start the app and load the selected window again. This affects local downloaded market data only; it never touches the repository or credentials.

## Storage choice

The adopted design combines DuckDB metadata and aggregate queries with Zstandard-compressed Parquet trade partitions by settlement year and month. It is designed for a laptop: every eligible market has a compact DuckDB aggregate (peak price, timestamps, crossings, and trade count), while raw normalized Parquet is retained only where the eventual losing side traded at 50% or above. This keeps all supported 50%–99% historical-miss analysis exact without retaining ordinary non-miss trade history.

Catalog pages are staged and queried in DuckDB instead of accumulated in application memory. A selected-window reload publishes only after it succeeds; then data outside that selected settlement window is pruned. An all-history reload retains all completed coverage. Existing data stays queryable if a load is cancelled, rate limited, or hits the optional storage cap.

Open-market snapshots are separate: they remain in process memory for only 60 seconds and are never written to DuckDB or Parquet. Historical loads and open-market refreshes share the same process-wide request pace and circuit breaker.

- DuckDB alone is simpler, but makes raw partition replacement and recovery less convenient.
- SQLite is lightweight, but is less suitable for large column-oriented analytical scans.
- PostgreSQL scales well, but adds an unnecessary local database server and administration burden.

All downloaded data remains local and outside the repository.

## API and development

Start or stop the local app without keeping a terminal window open:

```bash
./scripts/start-app.sh
./scripts/stop-app.sh
```

The scripts record only their own background process in your temporary directory and write its
launch log to `/tmp/kalshi-data-stats.log` (or the system `TMPDIR` equivalent).

The local API is versioned under `/api/v1`; interactive documentation is available at `/docs`. Refresh frontend contracts from the running server with:

```bash
npm --prefix frontend run generate:api
```

Run all checks:

```bash
uv run ruff check backend
uv run pytest -q
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run test:e2e
```

The committed `frontend/src/api/generated.ts` contract is generated from FastAPI's OpenAPI
schema. With the app running on port 8000, refresh and drift-check it with
`npm --prefix frontend run check:api`.

`GET /api/v1/open-markets` accepts `threshold`, `horizon`, `page`, `page_size`, and `refresh`. Horizons are limited to `24h`, `3d`, `7d`, and `14d`; there is deliberately no unbounded future-market option.

An active load can be cancelled from the dashboard. The saved catalog cursor and downloaded
market work remain local, so **Reload data** resumes that same time frame rather than starting over.
While the catalog is still being scanned, progress reports matching ordinary markets discovered;
once the total is known, it reports downloaded markets out of the total. The Local data screen
shows the aggregate-market count, retained raw trade count, dataset version, coverage, and disk use.

## Limitations

- Open-market prices are one-minute REST snapshots, not streaming quotes, and the app does not trade or place orders.
- Multivariate-event (combo) markets are intentionally excluded to keep the local historical dataset tractable.
- Kalshi can move records between live and historical API partitions. Each sync reads the current cutoff and routes requests accordingly.
- Public exchange data can be corrected upstream; reloading refreshes affected finalized markets without deleting unrelated cached coverage.
