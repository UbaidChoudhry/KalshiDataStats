import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertCircle, BarChart3, Database, Divide, History, LoaderCircle, RadioTower, RefreshCw, ScanLine, Settings2, TrendingUp, XCircle } from 'lucide-react'
import { api } from './api/client'
import type { ConfidenceBand, DataStatus, MissesResponse, Summary, SyncRun, WindowKey } from './api/types'
import { ConfidenceChart } from './components/ConfidenceChart'
import { MissesTable } from './components/MissesTable'
import { OpenMarketsPage } from './components/OpenMarketsPage'

const WINDOW_LABELS: Record<WindowKey, string> = { '3m': 'Last 3 months', '6m': 'Last 6 months', '1y': 'Last year', all: 'All available' }
const emptySummary: Summary = { window: '1y', threshold: 80, settled_markets: 0, crossed_markets: 0, wrong_markets: 0, miss_rate: null }
const emptyMisses: MissesResponse = { items: [], page: 1, page_size: 50, total: 0, pages: 0 }
const idleSync: SyncRun = { id: 'idle', status: 'idle', stage: 'Idle', window: '1y', processed_markets: 0, total_markets: 0, progress_percent: 0, raw_markets: 0, raw_trades: 0, breaker_open: false, breaker_seconds_remaining: 0, error: null, resumable: false }

function App() {
  const [activePage, setActivePage] = useState<'history' | 'open' | 'data'>('history')
  const [windowKey, setWindowKey] = useState<WindowKey>('1y')
  const [threshold, setThreshold] = useState(80)
  const [customThreshold, setCustomThreshold] = useState('')
  const [summary, setSummary] = useState(emptySummary)
  const [bands, setBands] = useState<ConfidenceBand[]>([])
  const [selectedBand, setSelectedBand] = useState<ConfidenceBand | null>(null)
  const [misses, setMisses] = useState(emptyMisses)
  const [dataStatus, setDataStatus] = useState<DataStatus | null>(null)
  const [sync, setSync] = useState(idleSync)
  const [page, setPage] = useState(1)
  const [sort, setSort] = useState('peak_confidence')
  const [direction, setDirection] = useState<'asc' | 'desc'>('desc')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)

  const isSyncing = starting || ['queued', 'running', 'breaker_open'].includes(sync.status)
  const legacyCacheError = dataStatus?.legacy_cache_error ?? null

  const loadDashboard = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [nextSummary, nextBands, nextMisses, nextStatus] = await Promise.all([
        api.summary(windowKey, threshold), api.bands(windowKey, threshold), api.misses({
          window: windowKey, threshold, page, pageSize: 50, sort, direction,
          bandMin: selectedBand?.min_percent, bandMax: selectedBand?.max_percent,
        }), api.dataStatus(),
      ])
      setSummary(nextSummary)
      setBands(nextBands.items)
      setMisses(nextMisses)
      setDataStatus(nextStatus)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Unable to load local data')
    } finally { setLoading(false) }
  }, [direction, page, selectedBand, sort, threshold, windowKey])

  useEffect(() => { void loadDashboard() }, [loadDashboard])

  useEffect(() => {
    void api.currentSync()
      .then((current) => {
        if (!current) return
        setSync(current)
        setWindowKey(current.window)
      })
      .catch(() => { /* Dashboard data remains usable if run-state discovery fails. */ })
  }, [])

  useEffect(() => {
    if (!isSyncing) return
    const timer = window.setInterval(async () => {
      try {
        const current = await api.currentSync()
        if (current) {
          setSync(current)
          if (current.status === 'completed') void loadDashboard()
        }
      } catch { /* Keep the last known run state during a transient poll failure. */ }
    }, 1000)
    return () => window.clearInterval(timer)
  }, [isSyncing, loadDashboard])

  const applyThreshold = (value: number, custom = '') => {
    setThreshold(value)
    setCustomThreshold(custom)
    setSelectedBand(null)
    setPage(1)
  }
  const applyCustom = () => {
    const value = Number(customThreshold)
    if (Number.isInteger(value) && value >= 50 && value <= 99) applyThreshold(value, String(value))
  }
  const startSync = async () => {
    setError(null)
    setStarting(true)
    setSync((current) => ({
      ...current,
      status: 'queued',
      stage: 'Starting load…',
      window: windowKey,
      error: null,
      resumable: true,
    }))
    try {
      setSync(await api.startSync(windowKey))
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Unable to start reload')
    } finally {
      setStarting(false)
    }
  }
  const cancelSync = async () => {
    setCancelling(true)
    try {
      const cancelled = await api.cancelSync()
      if (cancelled) setSync(cancelled)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Unable to cancel the load')
    } finally {
      setCancelling(false)
      setStarting(false)
    }
  }
  const handleSort = useCallback((nextSort: string) => {
    if (sort === nextSort) setDirection((value) => value === 'desc' ? 'asc' : 'desc')
    else { setSort(nextSort); setDirection('desc') }
    setPage(1)
  }, [sort])
  const coverage = useMemo(() => dataStatus?.coverage_start && dataStatus.coverage_end ? `${new Date(dataStatus.coverage_start).toLocaleDateString()} – ${new Date(dataStatus.coverage_end).toLocaleDateString()}` : 'No local data yet', [dataStatus])
  const storageUsage = useMemo(() => {
    if (!dataStatus) return 'Loading…'
    const format = (bytes: number) => bytes < 1024 ** 2 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 ** 3).toFixed(2)} GB`
    const used = dataStatus.storage_bytes ?? 0
    const limit = dataStatus.storage_limit_bytes
    return limit == null
      ? `${format(used)} (no limit)`
      : `${format(used)} of ${format(limit)}`
  }, [dataStatus])
  const datasetScope = useMemo(() => {
    if (!dataStatus || dataStatus.scope === 'empty') return 'Not loaded yet'
    return WINDOW_LABELS[dataStatus.scope as WindowKey] ?? dataStatus.scope
  }, [dataStatus])
  const syncProgress = sync.total_markets > 0
    ? `Downloaded ${sync.processed_markets.toLocaleString()} of ${sync.total_markets.toLocaleString()} markets`
    : sync.processed_markets > 0
      ? `${sync.processed_markets.toLocaleString()} matching markets discovered`
      : sync.stage.includes('catalog')
        ? 'Scanning market catalog…'
        : 'Preparing download…'

  return <div className="app-shell">
    <header className="topbar"><div className="brand"><span><ScanLine /></span>Forecast Lens</div><div className="local-status"><i />{activePage === 'open' ? 'Live market view' : isSyncing ? sync.stage : dataStatus?.has_data ? 'Local data ready' : 'Awaiting first load'}</div></header>
    <div className="mobile-context">{activePage === 'open' ? <><RadioTower /> Open markets <span>• Live view</span></> : activePage === 'data' ? <><Database /> Local data</> : <><History /> Historical misses <span>• Phase 1</span></>}</div>
    <div className="layout">
      <aside className="sidebar"><p>Analyze</p><button className={activePage === 'history' ? 'nav-active' : ''} onClick={() => setActivePage('history')}><History />Historical misses</button><button className={activePage === 'open' ? 'nav-active' : ''} onClick={() => setActivePage('open')}><RadioTower />Open markets</button><p>Manage</p><button className={activePage === 'data' ? 'nav-active' : ''} onClick={() => setActivePage('data')}><Database />Local data</button><button><Settings2 />Settings</button><div className="storage-note"><strong>Stored on this Mac</strong><span>Market history stays outside the repository. API pace is configured locally.</span></div></aside>
      <main>
        {activePage === 'open' ? <OpenMarketsPage /> : activePage === 'data' ? <section className="data-page" aria-labelledby="local-data-heading">
          <div className="title-row"><div><h1 id="local-data-heading">Local data</h1><p>Inspect the cached Kalshi dataset stored only on this Mac.</p></div><div className="title-actions">{isSyncing && <button className="secondary-button cancel-button" disabled={cancelling} onClick={cancelSync}>{cancelling ? <LoaderCircle className="spin" /> : <XCircle />}{cancelling ? 'Cancelling…' : 'Cancel load'}</button>}<button className="primary-button" disabled={isSyncing || Boolean(legacyCacheError)} onClick={startSync}>{isSyncing ? <LoaderCircle className="spin" /> : <RefreshCw />}{isSyncing ? 'Loading…' : legacyCacheError ? 'Update cache first' : 'Load selected window'}</button></div></div>
          {legacyCacheError && <div className="error-banner" role="alert"><AlertCircle /><div><strong>Local cache needs an update</strong><span>{legacyCacheError} Stop the app, move or delete the folder set by KALSHI_DATA_DIR, then start it and load data again.</span></div></div>}
          {isSyncing && <div className="sync-banner" role="status"><div><strong>{sync.status === 'breaker_open' ? `Rate limited — retrying in ${sync.breaker_seconds_remaining}s` : sync.stage}</strong><span>{syncProgress}</span></div><progress max="100" value={sync.total_markets > 0 ? sync.progress_percent : undefined} /></div>}
          {sync.status === 'failed_resumable' && <div className="error-banner" role="alert"><AlertCircle /><div><strong>Reload paused</strong><span>{sync.error} Use Load selected window to resume.</span></div></div>}
          {sync.status === 'cancelled' && <div className="sync-banner" role="status"><div><strong>Load cancelled</strong><span>Your cursor and downloaded pages are saved.</span></div></div>}
          {error && <div className="error-banner" role="alert"><AlertCircle /><div><strong>Could not load data</strong><span>{error}</span></div></div>}
          <section className="kpis local-kpis" aria-label="Local dataset status">
            <article><span>Market aggregates</span><strong>{dataStatus?.aggregate_markets.toLocaleString() ?? '0'}</strong><small><Database />ordinary finalized markets</small></article>
            <article><span>Retained miss trades</span><strong>{dataStatus?.raw_trades.toLocaleString() ?? '0'}</strong><small><TrendingUp />only markets with a 50%+ losing side</small></article>
            <article><span>Raw market files</span><strong>{dataStatus?.raw_markets.toLocaleString() ?? '0'}</strong><small><Database />compressed Parquet partitions</small></article>
            <article><span>Coverage</span><strong className="coverage-value">{coverage}</strong><small><History />settlement dates</small></article>
            <article><span>Dataset scope</span><strong className="coverage-value">{datasetScope}</strong><small><History />v{dataStatus?.dataset_version ?? '2'} · combo markets excluded</small></article>
            <article><span>Storage used</span><strong className="coverage-value">{storageUsage}</strong><small><Database />configured in environment</small></article>
          </section>
          <section className="data-detail"><h2>Current load</h2><p><strong>{sync.stage}</strong>{sync.error ? ` — ${sync.error}` : isSyncing ? ` — ${syncProgress}` : dataStatus?.last_successful_sync ? ` — last published ${new Date(dataStatus.last_successful_sync).toLocaleString()}` : ' — no active download'}</p><button className="secondary-button" onClick={() => setActivePage('history')}><History />View historical misses</button></section>
        </section> : <>
        <div className="title-row"><div><h1>Historical mispredictions</h1><p>Find settled markets where the favored side crossed your confidence threshold and lost.</p></div><div className="title-actions">{isSyncing && <button className="secondary-button cancel-button" disabled={cancelling} onClick={cancelSync}>{cancelling ? <LoaderCircle className="spin" /> : <XCircle />}{cancelling ? 'Cancelling…' : 'Cancel load'}</button>}<button className="primary-button" disabled={isSyncing || Boolean(legacyCacheError)} onClick={startSync}>{isSyncing ? <LoaderCircle className="spin" /> : <RefreshCw />}{isSyncing ? 'Loading…' : legacyCacheError ? 'Update cache first' : dataStatus?.has_data ? 'Reload data' : 'Load data'}</button></div></div>

        {legacyCacheError && <div className="error-banner" role="alert"><AlertCircle /><div><strong>Local cache needs an update</strong><span>{legacyCacheError} Open Local data for the safe recovery steps.</span></div></div>}

        {isSyncing && <div className="sync-banner" role="status"><div><strong>{sync.status === 'breaker_open' ? `Rate limited — retrying in ${sync.breaker_seconds_remaining}s` : sync.stage}</strong><span>{syncProgress}</span></div><progress max="100" value={sync.total_markets > 0 ? sync.progress_percent : undefined} /></div>}
        {sync.status === 'failed_resumable' && <div className="error-banner" role="alert"><AlertCircle /><div><strong>Reload paused</strong><span>{sync.error} Your progress is saved; use Reload data to resume.</span></div></div>}
        {sync.status === 'cancelled' && <div className="sync-banner" role="status"><div><strong>Load cancelled</strong><span>Your cursor and downloaded pages are saved. Select Reload data to continue.</span></div></div>}
        {error && <div className="error-banner" role="alert"><AlertCircle /><div><strong>Could not load dashboard</strong><span>{error}</span></div></div>}

        <section className="filters" aria-label="Analysis controls">
          <label><span>Load time frame</span><select value={windowKey} onChange={(event) => { setWindowKey(event.target.value as WindowKey); setSelectedBand(null); setPage(1) }}>{Object.entries(WINDOW_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <div className="threshold-field"><span>Confidence threshold</span><div className="threshold-row">{[80, 85, 90, 95].map((value) => <button key={value} aria-pressed={threshold === value && customThreshold === ''} onClick={() => applyThreshold(value)}>{value}%+</button>)}<input aria-label="Custom confidence threshold" aria-invalid={customThreshold !== '' && (!Number.isInteger(Number(customThreshold)) || Number(customThreshold) < 50 || Number(customThreshold) > 99)} inputMode="numeric" placeholder="Custom" min="50" max="99" value={customThreshold} onChange={(event) => setCustomThreshold(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') applyCustom() }} onBlur={applyCustom} /></div></div>
          <div className="dataset-field"><span>Dataset</span><div><i /><p><strong>{coverage}</strong><small>{dataStatus?.last_successful_sync ? `Updated ${new Date(dataStatus.last_successful_sync).toLocaleString()}` : 'Click Load data to begin'}</small></p></div></div>
        </section>

        <section className="kpis" aria-label="Historical results summary">
          <article><span>Settled markets</span><strong>{summary.settled_markets.toLocaleString()}</strong><small><Database />in loaded period</small></article>
          <article><span>Crossed threshold</span><strong>{summary.crossed_markets.toLocaleString()}</strong><small><TrendingUp />{summary.settled_markets ? `${(summary.crossed_markets / summary.settled_markets * 100).toFixed(1)}% of markets` : 'no qualifying trades'}</small></article>
          <article><span>Wrong outcomes</span><strong>{summary.wrong_markets.toLocaleString()}</strong><small className="danger"><XCircle />favored side lost</small></article>
          <article><span>Miss rate</span><strong>{summary.miss_rate === null ? '—' : `${(summary.miss_rate * 100).toFixed(1)}%`}</strong><small><Divide />wrong ÷ crossed</small></article>
        </section>

        <section className="chart-panel" aria-labelledby="chart-heading"><header className="panel-heading"><h2 id="chart-heading">Wrong calls by peak confidence</h2><span>Select a bar to inspect its markets</span></header>{loading ? <div className="loading-state"><LoaderCircle className="spin" /> Loading analytics</div> : bands.length ? <ConfidenceChart bands={bands} selected={selectedBand} onSelect={(band) => { setSelectedBand((current) => current?.min_percent === band.min_percent ? null : band); setPage(1) }} /> : <div className="empty-chart"><BarChart3 />No confidence bands for this selection</div>}</section>
        <MissesTable data={misses} band={selectedBand} page={page} sort={sort} direction={direction} onClearBand={() => { setSelectedBand(null); setPage(1) }} onPage={setPage} onSort={handleSort} />
        </>}
      </main>
    </div>
  </div>
}

export default App
