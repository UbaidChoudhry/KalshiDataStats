import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: () => ({ setOption: vi.fn(), on: vi.fn(), resize: vi.fn(), dispose: vi.fn() }),
}))

const markets = [
  { ticker: 'HIGH', event_ticker: 'EVENT', title: 'High miss', peak_confidence: 96, losing_side: 'yes', first_crossed_at: '2026-01-01T00:00:00Z', settled_at: '2026-01-02T00:00:00Z' },
]

describe('historical dashboard', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      const payload = url.includes('/summary') ? { window: '1y', threshold: 80, settled_markets: 100, crossed_markets: 20, wrong_markets: 4, miss_rate: .2 }
        : url.includes('/bands') ? { items: [{ min_percent: 80, max_percent: 84, label: '80–84%', count: 4 }] }
        : url.includes('/misses') ? { items: markets, page: 1, page_size: 50, total: 1, pages: 1 }
        : url.includes('/data/status') ? { has_data: true, coverage_start: '2025-01-01T00:00:00Z', coverage_end: '2026-01-01T00:00:00Z', last_successful_sync: '2026-01-01T00:00:00Z', total_markets: 100, total_trades: 1000 }
        : { id: 'run', status: 'completed', stage: 'Complete', window: '1y', processed_markets: 100, total_markets: 100, progress_percent: 100, breaker_open: false, breaker_seconds_remaining: 0, error: null, resumable: false }
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
  })

  it('renders API analytics and market identities', async () => {
    render(<App />)
    expect(await screen.findByText('High miss')).toBeInTheDocument()
    expect(screen.getByText('100')).toBeInTheDocument()
    expect(screen.getByText('4')).toBeInTheDocument()
  })

  it('applies a custom threshold and reloads analytics', async () => {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('High miss')
    const custom = screen.getByLabelText('Custom confidence threshold')
    await user.type(custom, '87')
    await user.tab()
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('threshold=87'), expect.anything()))
  })

  it('starts a reload from the selected time frame', async () => {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('High miss')
    await user.click(screen.getByRole('button', { name: 'Reload data' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/v1/sync-runs', expect.objectContaining({ method: 'POST' })))
  })

  it('opens the local data screen with cache details', async () => {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('High miss')
    await user.click(screen.getByRole('button', { name: 'Local data' }))
    expect(screen.getByRole('heading', { name: 'Local data' })).toBeInTheDocument()
    expect(screen.getByText('Cached markets')).toBeInTheDocument()
    expect(screen.getByText('Cached trades')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'View historical misses' })).toBeInTheDocument()
  })

  it('offers cancellation while a load is active', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      const payload = url.includes('/summary') ? { window: '1y', threshold: 80, settled_markets: 0, crossed_markets: 0, wrong_markets: 0, miss_rate: null }
        : url.includes('/bands') ? { items: [] }
        : url.includes('/misses') ? { items: [], page: 1, page_size: 50, total: 0, pages: 0 }
        : url.includes('/data/status') ? { has_data: false, coverage_start: null, coverage_end: null, last_successful_sync: null, total_markets: 0, total_trades: 0 }
        : { id: 'run', status: 'running', stage: 'historical catalog', window: '1y', processed_markets: 40, total_markets: 100, progress_percent: 40, breaker_open: false, breaker_seconds_remaining: 0, error: null, resumable: true }
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const user = userEvent.setup()
    render(<App />)
    await screen.findByRole('button', { name: 'Cancel load' })
    await user.click(screen.getByRole('button', { name: 'Cancel load' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/v1/sync-runs/current/cancel', expect.objectContaining({ method: 'POST' })))
  })
})
