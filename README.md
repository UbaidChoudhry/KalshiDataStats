# Forecast Lens

Forecast Lens is a local analytics dashboard for finding settled Kalshi markets where the eventual losing side traded at a high implied probability. Phase 1 covers historical mispredictions; open-market analysis is intentionally reserved for Phase 2.

## What it measures

A market is counted as a miss when any non-block trade prices the eventual losing side at or above the selected threshold. The 3-month, 6-month, and 1-year filters use settlement time, while every eligible trade across each included market's full lifetime is analyzed. Finalized binary and multivariate-event markets are supported; scalar markets are excluded.

The dashboard includes summary statistics, five-point confidence bands, clickable bar-to-market drill-down, custom integer thresholds from 50% to 99%, sorting, and 50-row server-side pagination.

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

Copy `.env.example` to `.env` only if you want to override defaults. The application does not load secrets and does not require a Kalshi API key for Phase 1.

Launch the production-style single process:

```bash
uv run python -m backend.app
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The first **Load data** operation may take a long time for a large window. Progress and rate-limit pauses are shown in the dashboard, and interrupted work is resumable.

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

## Storage choice

The adopted design combines DuckDB metadata and aggregate queries with Zstandard-compressed Parquet trade partitions by settlement year and month. This keeps analytical scans fast while retaining raw normalized trades for future statistics.

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

An active load can be cancelled from the dashboard. The saved catalog cursor and downloaded
pages remain local, so **Reload data** resumes that same time frame rather than starting over.
While the catalog is still being scanned, progress reports matching markets discovered; once
the total is known, it reports downloaded markets out of the total.

## Limitations

- Phase 1 does not analyze open markets, stream live prices, trade, place orders, create accounts, or deploy to the cloud.
- Kalshi can move records between live and historical API partitions. Each sync reads the current cutoff and routes requests accordingly.
- Public exchange data can be corrected upstream; reloading refreshes affected finalized markets without deleting unrelated cached coverage.
