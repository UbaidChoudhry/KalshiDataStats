import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  retries: 0,
  reporter: 'line',
  use: { baseURL: 'http://127.0.0.1:8010', trace: 'retain-on-failure' },
  webServer: {
    command: 'cd .. && KALSHI_SYNC_MODE=demo KALSHI_DATA_DIR=/tmp/kalshi-data-stats-e2e-v2 uv run python -m backend.app',
    url: 'http://127.0.0.1:8010/api/v1/health',
    env: { KALSHI_PORT: '8010', UV_CACHE_DIR: '/tmp/kalshi-uv-cache' },
    reuseExistingServer: false,
    timeout: 120000,
  },
})
