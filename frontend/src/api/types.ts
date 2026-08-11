import type { components } from './generated'

export type WindowKey = components['schemas']['Window']
export type Summary = components['schemas']['HistorySummary']
export type ConfidenceBand = components['schemas']['Band']
export type MissedMarket = components['schemas']['Miss']
export type MissesResponse = components['schemas']['MissesResponse']
export type DataStatus = components['schemas']['DataStatus']

export type SyncStatus = 'idle' | 'queued' | 'running' | 'breaker_open' | 'completed' | 'failed_resumable'

export type SyncRun = Omit<components['schemas']['SyncRun'], 'status'> & {
  status: SyncStatus
}
