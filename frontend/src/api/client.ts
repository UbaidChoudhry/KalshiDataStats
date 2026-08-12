import type { ConfidenceBand, DataStatus, MissesResponse, Summary, SyncRun, WindowKey } from './types'

const API_ROOT = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try {
      const payload = (await response.json()) as { detail?: string }
      if (payload.detail) detail = payload.detail
    } catch {
      // Preserve the status-based fallback for non-JSON failures.
    }
    throw new Error(detail)
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
  startSync: (window: WindowKey) =>
    request<SyncRun>('/sync-runs', { method: 'POST', body: JSON.stringify({ window }) }),
  currentSync: () => request<SyncRun | null>('/sync-runs/current'),
  cancelSync: () => request<SyncRun | null>('/sync-runs/current/cancel', { method: 'POST' }),
}
