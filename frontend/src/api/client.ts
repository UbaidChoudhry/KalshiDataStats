import { ApiError, type ConfidenceBand, type DataStatus, type MissesResponse, type OpenMarketLink, type OpenMarketsHorizon, type OpenMarketsResponse, type Summary, type SyncRun, type WindowKey } from './types'

const API_ROOT = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    let breakerSecondsRemaining: number | null = null
    try {
      const payload = (await response.json()) as { detail?: string | { message?: string; retry_seconds?: number }; error?: { message?: string }; breaker_seconds_remaining?: number }
      if (typeof payload.detail === 'string') detail = payload.detail
      if (typeof payload.detail === 'object' && payload.detail) {
        detail = payload.detail.message ?? detail
        breakerSecondsRemaining = payload.detail.retry_seconds ?? null
      }
      if (payload.error?.message) detail = payload.error.message
      breakerSecondsRemaining ??= payload.breaker_seconds_remaining ?? null
    } catch {
      // Preserve the status-based fallback for non-JSON failures.
    }
    throw new ApiError(detail, response.status, breakerSecondsRemaining)
  }
  return response.json() as Promise<T>
}

export const api = {
  dataStatus: () => request<DataStatus>('/data/status'),
  summary: (window: WindowKey, threshold: number) =>
    request<Summary>(`/history/summary?window=${window}&threshold=${threshold}`),
  bands: (window: WindowKey, threshold: number) =>
    request<{ items: ConfidenceBand[] }>(`/history/bands?window=${window}&threshold=${threshold}`),
  misses: (params: {
    window: WindowKey
    threshold: number
    page: number
    pageSize: number
    sort: string
    direction: 'asc' | 'desc'
    bandMin?: number
    bandMax?: number
  }) => {
    const query = new URLSearchParams({
      window: params.window,
      threshold: String(params.threshold),
      page: String(params.page),
      page_size: String(params.pageSize),
      sort: params.sort,
      direction: params.direction,
    })
    if (params.bandMin !== undefined) query.set('min_percent', String(params.bandMin))
    if (params.bandMax !== undefined) query.set('max_percent', String(params.bandMax))
    return request<MissesResponse>(`/history/misses?${query}`)
  },
  openMarkets: (params: {
    threshold: number
    horizon: OpenMarketsHorizon
    page: number
    pageSize: number
    refresh: boolean
    category: string
    signal?: AbortSignal
  }) => {
    const query = new URLSearchParams({
      threshold: String(params.threshold),
      horizon: params.horizon,
      page: String(params.page),
      page_size: String(params.pageSize),
      category: params.category,
      refresh: String(params.refresh),
    })
    return request<OpenMarketsResponse>(`/open-markets?${query}`, { signal: params.signal })
  },
  openMarketLink: (eventTicker: string) =>
    request<OpenMarketLink>(`/open-markets/link?${new URLSearchParams({ event_ticker: eventTicker })}`),
  startSync: (window: WindowKey) =>
    request<SyncRun>('/sync-runs', { method: 'POST', body: JSON.stringify({ window }) }),
  currentSync: () => request<SyncRun | null>('/sync-runs/current'),
  cancelSync: () => request<SyncRun | null>('/sync-runs/current/cancel', { method: 'POST' }),
}
