import type { components } from './generated'

export type WindowKey = components['schemas']['Window']
export type Summary = components['schemas']['HistorySummary']
export type ConfidenceBand = components['schemas']['Band']
export type MissedMarket = components['schemas']['Miss']
export type MissesResponse = components['schemas']['MissesResponse']
export type DataStatus = components['schemas']['DataStatus']

export type SyncStatus = 'idle' | 'queued' | 'running' | 'breaker_open' | 'completed' | 'failed_resumable' | 'cancelled'

export type SyncRun = Omit<components['schemas']['SyncRun'], 'status'> & {
  status: SyncStatus
}

export type OpenMarketsHorizon = components['schemas']['OpenMarketHorizon']
export type OpenMarket = components['schemas']['OpenMarket']
export type OpenMarketsResponse = components['schemas']['OpenMarketsResponse']

export class ApiError extends Error {
  status: number
  breakerSecondsRemaining: number | null

  constructor(message: string, status: number, breakerSecondsRemaining: number | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.breakerSecondsRemaining = breakerSecondsRemaining
  }
}
