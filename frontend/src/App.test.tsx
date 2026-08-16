import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { OpenMarketsPage } from './components/OpenMarketsPage'

vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: () => ({ setOption: vi.fn(), on: vi.fn(), resize: vi.fn(), dispose: vi.fn() }),
}))

const markets = [
  { ticker: 'HIGH', event_ticker: 'EVENT', title: 'High miss', peak_confidence: 96, losing_side: 'yes', first_crossed_at: '2026-01-01T00:00:00Z', settled_at: '2026-01-02T00:00:00Z' },
]
const openMarkets = {
  items: [
    { ticker: 'SOON', event_ticker: 'EVENT-SOON', category: 'Politics', title: 'Will the launch happen this morning?', subtitle: 'Morning launch', qualifying_side: 'yes', qualifying_label: 'Yes', qualifying_bid_percent: 92, yes_bid_percent: 92, no_bid_percent: 8, volume_24h: 12345, liquidity_dollars: 4200, close_at: '2026-08-15T12:00:00Z', can_close_early: true },
    { ticker: 'LATER', event_ticker: 'EVENT-LATER', category: 'Sports', title: 'Will the launch happen tomorrow?', subtitle: 'Tomorrow launch', qualifying_side: 'no', qualifying_label: 'No', qualifying_bid_percent: 85, yes_bid_percent: 15, no_bid_percent: 85, volume_24h: 3400, liquidity_dollars: 1200, close_at: '2026-08-16T12:00:00Z', can_close_early: false },
  ],
  page: 1, page_size: 50, total: 2, pages: 1, scanned_markets: 4, matching_markets: 2,
  next_close_at: '2026-08-15T12:00:00Z', highest_bid: 92, as_of: '2026-08-14T12:00:00Z', stale: false, refresh_state: 'live', breaker_seconds_remaining: 0,
}

describe('historical dashboard', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      const payload = url.includes('/open-markets/link') ? { url: 'https://kalshi.com/markets/kxpgatour/pga-tour/kxpgatour-fesjc26' }
        : url.includes('/open-markets') ? openMarkets
        : url.includes('/summary') ? { window: '1y', threshold: 80, settled_markets: 100, crossed_markets: 20, wrong_markets: 4, miss_rate: .2 }
        : url.includes('/bands') ? { items: [{ min_percent: 80, max_percent: 84, label: '80–84%', count: 4 }] }
        : url.includes('/misses') ? { items: markets, page: 1, page_size: 50, total: 1, pages: 1 }
        : url.includes('/data/status') ? { has_data: true, coverage_start: '2025-01-01T00:00:00Z', coverage_end: '2026-01-01T00:00:00Z', last_successful_sync: '2026-01-01T00:00:00Z', total_markets: 100, total_trades: 1000, aggregate_markets: 100, raw_markets: 4, raw_trades: 52, dataset_version: '2', scope: '1y', mve_excluded: true, storage_bytes: 1024 ** 3, storage_limit_bytes: 5 * 1024 ** 3 }
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
    expect(screen.getByText('Market aggregates')).toBeInTheDocument()
    expect(screen.getByText('Retained miss trades')).toBeInTheDocument()
    expect(screen.getByText('Raw market files')).toBeInTheDocument()
    expect(screen.getByText('Storage used')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'View historical misses' })).toBeInTheDocument()
  })

  it('opens soonest-closing markets and sends the selected filter contract', async () => {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('High miss')
    await user.click(screen.getByRole('button', { name: 'Open markets' }))
    expect(await screen.findByRole('heading', { name: 'Open markets' })).toBeInTheDocument()
    expect(screen.getByText('Will the launch happen this morning?')).toBeInTheDocument()
    expect(screen.getByText('May close early')).toBeInTheDocument()
    expect(screen.getByText('Will the launch happen tomorrow?')).toBeInTheDocument()
    expect(screen.getByText('Politics')).toBeInTheDocument()
    expect(screen.getByText('Sports')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open Will the launch happen this morning? on Kalshi' })).toBeInTheDocument()
    expect(screen.getByText('YES %')).toBeInTheDocument()
    expect(screen.getByText('NO %')).toBeInTheDocument()
    expect(screen.getByText('92%')).toBeInTheDocument()
    expect(screen.getByText('8%')).toBeInTheDocument()
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/v1/open-markets?threshold=80&horizon=7d&page=1&page_size=50&refresh=false'), expect.anything()))

    await user.click(screen.getByRole('button', { name: '90%+' }))
    await user.selectOptions(screen.getByLabelText('Closing horizon'), '3d')
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('threshold=90&horizon=3d&page=1&page_size=50&refresh=false'), expect.anything()))
  })

  it('resolves the canonical Kalshi event page only when its CTA is clicked', async () => {
    const replace = vi.fn()
    const popup = { opener: window, location: { replace }, close: vi.fn() }
    const open = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
    const user = userEvent.setup()
    render(<OpenMarketsPage />)
    await screen.findByText('Will the launch happen this morning?')
    await user.click(screen.getByRole('button', { name: 'Open Will the launch happen this morning? on Kalshi' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/v1/open-markets/link?event_ticker=EVENT-SOON', expect.anything()))
    expect(replace).toHaveBeenCalledWith('https://kalshi.com/markets/kxpgatour/pga-tour/kxpgatour-fesjc26')
    expect(popup.opener).toBeNull()
    open.mockRestore()
  })

  it('places markets closing within fifteen minutes above their category groups', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      const soon = new Date(Date.now() + 10 * 60 * 1000).toISOString()
      const later = new Date(Date.now() + 60 * 60 * 1000).toISOString()
      const payload = url.includes('/open-markets') ? {
        ...openMarkets,
        items: [
          { ...openMarkets.items[0], category: 'Crypto', close_at: soon },
          { ...openMarkets.items[1], category: 'Finance', close_at: later },
        ],
      } : openMarkets
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    render(<OpenMarketsPage />)
    expect(await screen.findByText('Closing in the next 15 minutes')).toBeInTheDocument()
    expect(screen.getByText('Finance')).toBeInTheDocument()
    expect(screen.queryByText('Crypto')).not.toBeInTheDocument()
  })

  it('keeps cached results visible when open-market data is stale', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      const payload = url.includes('/open-markets') ? { ...openMarkets, stale: true }
        : url.includes('/summary') ? { window: '1y', threshold: 80, settled_markets: 0, crossed_markets: 0, wrong_markets: 0, miss_rate: null }
        : url.includes('/bands') ? { items: [] }
        : url.includes('/misses') ? { items: [], page: 1, page_size: 50, total: 0, pages: 0 }
        : url.includes('/data/status') ? { has_data: false, coverage_start: null, coverage_end: null, last_successful_sync: null, total_markets: 0, total_trades: 0 }
        : { id: 'run', status: 'completed', stage: 'Complete', window: '1y', processed_markets: 0, total_markets: 0, progress_percent: 100, breaker_open: false, breaker_seconds_remaining: 0, error: null, resumable: false }
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', { name: 'Open markets' }))
    expect(await screen.findByText('Showing cached open-market data')).toBeInTheDocument()
    expect(screen.getByText('Will the launch happen this morning?')).toBeInTheDocument()
  })

  it('renders a tied qualifying market as one row with its combined label', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      const payload = {
        ...openMarkets,
        items: [{ ...openMarkets.items[1], qualifying_side: 'both', qualifying_label: 'Hold steady / Change', qualifying_bid_percent: 80.5 }],
        total: 1,
        matching_markets: 1,
        pages: 1,
      }
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    render(<OpenMarketsPage />)
    expect(await screen.findByText('Hold steady / Change')).toBeInTheDocument()
    expect(screen.getByText('80.5¢')).toBeInTheDocument()
    expect(document.querySelectorAll('.open-table tbody tr:not(.market-group-row)')).toHaveLength(1)
  })

  it('keeps a newer open-market response when an older request resolves late', async () => {
    const pending: Array<{ resolve: (response: Response) => void; signal: AbortSignal | undefined }> = []
    vi.stubGlobal('fetch', vi.fn((_input: string | URL | Request, init?: RequestInit) => new Promise<Response>((resolve) => {
      pending.push({ resolve, signal: init?.signal ?? undefined })
    })))
    const user = userEvent.setup()
    render(<OpenMarketsPage />)
    await waitFor(() => expect(pending).toHaveLength(1))
    await user.click(screen.getByRole('button', { name: '90%+' }))
    await waitFor(() => expect(pending).toHaveLength(2))
    expect(pending[0].signal?.aborted).toBe(true)

    await act(async () => {
      pending[0].resolve(new Response(JSON.stringify({ ...openMarkets, items: [{ ...openMarkets.items[0], title: 'Older result' }] }), { status: 200 }))
      pending[1].resolve(new Response(JSON.stringify({ ...openMarkets, items: [{ ...openMarkets.items[1], title: 'Newer result' }] }), { status: 200 }))
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(await screen.findByText('Newer result')).toBeInTheDocument()
    expect(screen.queryByText('Older result')).not.toBeInTheDocument()
  })

  it('aborts an in-flight open-market request when the page unmounts', async () => {
    const pending: Array<{ resolve: (response: Response) => void; signal: AbortSignal | undefined }> = []
    vi.stubGlobal('fetch', vi.fn((_input: string | URL | Request, init?: RequestInit) => new Promise<Response>((resolve) => {
      pending.push({ resolve, signal: init?.signal ?? undefined })
    })))
    const view = render(<OpenMarketsPage />)
    await waitFor(() => expect(pending).toHaveLength(1))
    view.unmount()
    expect(pending[0].signal?.aborted).toBe(true)
    await act(async () => {
      pending[0].resolve(new Response(JSON.stringify(openMarkets), { status: 200 }))
      await Promise.resolve()
    })
  })

  it('counts down a live-pricing circuit pause while retaining stale results', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      const payload = url.includes('/open-markets') ? { ...openMarkets, stale: true, breaker_seconds_remaining: 3, refresh_state: 'circuit_open' }
        : { id: 'run', status: 'completed', stage: 'Complete', window: '1y', processed_markets: 0, total_markets: 0, progress_percent: 100, breaker_open: false, breaker_seconds_remaining: 0, error: null, resumable: false }
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    vi.useFakeTimers()
    render(<OpenMarketsPage />)
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    expect(screen.getByText(/retry in 3s/)).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(screen.getByText(/retry in 2s/)).toBeInTheDocument()
    vi.useRealTimers()
  })

  it('refreshes open markets every minute only while visible and stops on unmount', async () => {
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    vi.useFakeTimers()
    const view = render(<OpenMarketsPage />)
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    expect(fetch).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(fetch).toHaveBeenCalledTimes(2)
    visibility.mockReturnValue('hidden')
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(fetch).toHaveBeenCalledTimes(2)
    view.unmount()
    visibility.mockRestore()
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(fetch).toHaveBeenCalledTimes(2)
    vi.useRealTimers()
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
