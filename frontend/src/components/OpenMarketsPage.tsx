import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, CalendarClock, ChevronLeft, ChevronRight, Clock3, ExternalLink, LoaderCircle, RefreshCw, TriangleAlert } from 'lucide-react'
import { api } from '../api/client'
import { ApiError, type OpenMarket, type OpenMarketsHorizon, type OpenMarketsResponse } from '../api/types'

const HORIZON_LABELS: Record<OpenMarketsHorizon, string> = {
  '24h': '24 hours',
  '3d': '3 days',
  '7d': '7 days',
  '14d': '14 days',
}

const emptyResponse: OpenMarketsResponse = { items: [], page: 1, page_size: 50, total: 0, pages: 0, scanned_markets: 0, matching_markets: 0, as_of: '', refresh_state: 'idle', stale: false, breaker_seconds_remaining: 0 }

function asDate(value: string | null | undefined) {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function formatCloseDistance(value: string | null | undefined, now: number) {
  const date = asDate(value)
  if (!date) return '—'
  const seconds = Math.round((date.getTime() - now) / 1000)
  if (seconds <= 0) return 'Closing now'
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return hours ? `${hours}h ${minutes}m` : `${Math.max(1, minutes)}m`
}

function formatCloseTime(value: string | null | undefined) {
  const date = asDate(value)
  return date ? date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—'
}

function formatBid(value: number | null | undefined) {
  if (value == null) return '—'
  const cents = value <= 1 ? value * 100 : value
  return `${Number.isInteger(cents) ? cents : cents.toFixed(1)}¢`
}

function formatPercent(value: number | null | undefined) {
  if (value == null) return '—'
  const percent = value <= 1 ? value * 100 : value
  return `${Number.isInteger(percent) ? percent : percent.toFixed(1)}%`
}

function formatContracts(value: number | null | undefined) {
  if (value == null) return '—'
  return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

function formatMoney(value: number | null | undefined) {
  if (value == null) return '—'
  return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

function summaryClose(response: OpenMarketsResponse) {
  return response.next_close_at
}

function summaryBid(response: OpenMarketsResponse) {
  return response.highest_bid
}

export function OpenMarketsPage() {
  const [threshold, setThreshold] = useState(80)
  const [customThreshold, setCustomThreshold] = useState('')
  const [horizon, setHorizon] = useState<OpenMarketsHorizon>('7d')
  const [page, setPage] = useState(1)
  const [data, setData] = useState<OpenMarketsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [circuitSeconds, setCircuitSeconds] = useState(0)
  const [now, setNow] = useState(() => Date.now())
  const circuitSecondsRef = useRef(0)
  const requestIdRef = useRef(0)
  const activeRequestRef = useRef<AbortController | null>(null)
  const mountedRef = useRef(true)

  useEffect(() => { circuitSecondsRef.current = circuitSeconds }, [circuitSeconds])

  useEffect(() => () => {
    mountedRef.current = false
    activeRequestRef.current?.abort()
  }, [])

  const load = useCallback(async (refresh = false) => {
    if (circuitSecondsRef.current > 0) return
    activeRequestRef.current?.abort()
    const controller = new AbortController()
    activeRequestRef.current = controller
    const requestId = requestIdRef.current + 1
    requestIdRef.current = requestId
    const isCurrent = () => mountedRef.current && requestIdRef.current === requestId && activeRequestRef.current === controller
    if (!isCurrent()) return
    if (refresh) { setRefreshing(true); setLoading(false) }
    else { setLoading(true); setRefreshing(false) }
    setError(null)
    try {
      const response = await api.openMarkets({ threshold, horizon, page, pageSize: 50, refresh, signal: controller.signal })
      if (!isCurrent()) return
      setData(response)
      setCircuitSeconds(response.breaker_seconds_remaining)
    } catch (nextError) {
      if (!isCurrent() || controller.signal.aborted) return
      if (nextError instanceof ApiError && nextError.breakerSecondsRemaining) {
        setCircuitSeconds(nextError.breakerSecondsRemaining)
      }
      setError(nextError instanceof Error ? nextError.message : 'Unable to load open markets')
    } finally {
      if (isCurrent()) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [horizon, page, threshold])

  useEffect(() => { void load() }, [load])

  useEffect(() => {
    const refreshTimer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load()
    }, 60_000)
    return () => window.clearInterval(refreshTimer)
  }, [load])

  useEffect(() => {
    const clockTimer = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => window.clearInterval(clockTimer)
  }, [])

  useEffect(() => {
    if (!circuitSeconds) return
    const countdown = window.setInterval(() => setCircuitSeconds((remaining) => Math.max(0, remaining - 1)), 1_000)
    return () => window.clearInterval(countdown)
  }, [circuitSeconds])

  const applyThreshold = (value: number, custom = '') => {
    setThreshold(value)
    setCustomThreshold(custom)
    setPage(1)
  }
  const applyCustomThreshold = () => {
    const value = Number(customThreshold)
    if (Number.isInteger(value) && value >= 50 && value <= 99) applyThreshold(value, String(value))
  }
  const changeHorizon = (value: OpenMarketsHorizon) => {
    setHorizon(value)
    setPage(1)
  }

  const response = data ?? emptyResponse
  const matchingCount = response.matching_markets
  const nextClose = summaryClose(response)
  const highestBid = summaryBid(response)
  const lastRefresh = response.as_of ? formatCloseTime(response.as_of) : data ? 'Just now' : '—'
  const stale = response.stale
  const pageCount = Math.max(1, response.pages)

  return <section className="open-markets-page" aria-labelledby="open-markets-heading">
    <div className="title-row"><div><h1 id="open-markets-heading">Open markets</h1><p>Markets currently favoring one outcome at or above your selected bid threshold.</p></div><div className="title-actions"><button className="primary-button" disabled={loading || refreshing || circuitSeconds > 0} onClick={() => void load(true)}>{refreshing ? <LoaderCircle className="spin" /> : <RefreshCw />}{refreshing ? 'Refreshing…' : 'Refresh'}</button></div></div>

    {circuitSeconds > 0 && <div className="error-banner circuit-banner" role="alert"><AlertCircle /><div><strong>Live pricing is temporarily paused</strong><span>Kalshi’s circuit breaker will retry in {circuitSeconds}s. Showing the last available results when possible.</span></div></div>}
    {stale && <div className="stale-banner" role="status"><TriangleAlert /><div><strong>Showing cached open-market data</strong><span>Live data could not be refreshed. Last refresh: {lastRefresh}.</span></div></div>}
    {error && <div className="error-banner" role="alert"><AlertCircle /><div><strong>Could not load open markets</strong><span>{error}</span></div></div>}

    <section className="filters open-filters" aria-label="Open market controls">
      <div className="threshold-field"><span>Best bid threshold</span><div className="threshold-row">{[80, 85, 90, 95].map((value) => <button key={value} aria-pressed={threshold === value && customThreshold === ''} onClick={() => applyThreshold(value)}>{value}%+</button>)}<input aria-label="Custom open market threshold" aria-invalid={customThreshold !== '' && (!Number.isInteger(Number(customThreshold)) || Number(customThreshold) < 50 || Number(customThreshold) > 99)} inputMode="numeric" placeholder="Custom" min="50" max="99" value={customThreshold} onChange={(event) => setCustomThreshold(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') applyCustomThreshold() }} onBlur={applyCustomThreshold} /></div></div>
      <label><span>Closing within</span><select aria-label="Closing horizon" value={horizon} onChange={(event) => changeHorizon(event.target.value as OpenMarketsHorizon)}>{(Object.entries(HORIZON_LABELS) as [OpenMarketsHorizon, string][]).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
      <div className="dataset-field"><span>Refresh policy</span><div><i /><p><strong>Every 60 seconds</strong><small>Only while this page is visible</small></p></div></div>
    </section>

    <section className="kpis open-kpis" aria-label="Open markets summary">
      <article><span>Matching markets</span><strong>{matchingCount.toLocaleString()}</strong><small><CalendarClock />threshold and horizon filters</small></article>
      <article><span>Next close</span><strong className="summary-time">{nextClose ? formatCloseDistance(nextClose, now) : '—'}</strong><small><Clock3 />{nextClose ? formatCloseTime(nextClose) : 'no scheduled close'}</small></article>
      <article><span>Highest best bid</span><strong>{formatBid(highestBid)}</strong><small>across matching markets</small></article>
      <article><span>Last refresh</span><strong className="summary-time">{lastRefresh}</strong><small>{stale ? 'cached result' : 'local time'}</small></article>
    </section>

    <section className="table-panel open-table" aria-labelledby="open-markets-table-heading">
      <header className="table-heading"><div><h2 id="open-markets-table-heading">Soonest-closing opportunities</h2><span>Fixed order: soonest close first · {response.total.toLocaleString()} matching markets</span></div></header>
      {loading && !data ? <div className="loading-state"><LoaderCircle className="spin" /> Loading open markets</div> : !response.items.length ? <div className="empty-state"><CalendarClock /><div><strong>No open markets match this selection</strong><span>Try a lower threshold or a longer closing horizon.</span></div></div> : <><div className="table-scroll"><table><thead><tr><th>Market</th><th>Favored option</th><th>Best bid</th><th>YES %</th><th>NO %</th><th>24h volume</th><th>Liquidity</th><th>Closes in</th><th>Local close time</th></tr></thead><tbody>{response.items.map((market) => <OpenMarketRow key={market.ticker} market={market} now={now} />)}</tbody></table></div><div className="pagination"><span>Page {response.page} of {pageCount}</span><div><button aria-label="Previous page" disabled={response.page <= 1 || loading} onClick={() => setPage((current) => Math.max(1, current - 1))}><ChevronLeft /></button><button aria-label="Next page" disabled={response.page >= pageCount || loading} onClick={() => setPage((current) => Math.min(pageCount, current + 1))}><ChevronRight /></button></div></div></>}
    </section>
  </section>
}

function OpenMarketRow({ market, now }: { market: OpenMarket; now: number }) {
  const [opening, setOpening] = useState(false)
  const [linkError, setLinkError] = useState<string | null>(null)
  const openInKalshi = async () => {
    if (!market.event_ticker || opening) return
    const popup = window.open('', '_blank')
    if (popup) popup.opener = null
    setOpening(true)
    setLinkError(null)
    try {
      const { url } = await api.openMarketLink(market.event_ticker)
      if (!popup) throw new Error('Your browser blocked the new tab. Allow pop-ups for this local dashboard and try again.')
      popup.location.replace(url)
    } catch (error) {
      popup?.close()
      setLinkError(error instanceof Error ? error.message : 'Unable to open this Kalshi market')
    } finally {
      setOpening(false)
    }
  }
  return <tr><td className="market-name"><strong>{market.title}</strong><span>{market.subtitle ?? market.ticker}{market.event_ticker ? ` · ${market.event_ticker}` : ''}</span><button className="market-cta" type="button" disabled={!market.event_ticker || opening} onClick={() => void openInKalshi()} aria-label={`Open ${market.title} on Kalshi`}>{opening ? 'Opening…' : 'Open in Kalshi'} <ExternalLink aria-hidden="true" /></button>{linkError && <small className="market-link-error" role="alert">{linkError}</small>}</td><td><span className="favored-option">{market.qualifying_label}</span>{market.can_close_early && <span className="early-close-badge">May close early</span>}</td><td className="best-bid">{formatBid(market.qualifying_bid_percent)}</td><td>{formatPercent(market.yes_bid_percent)}</td><td>{formatPercent(market.no_bid_percent)}</td><td>{formatContracts(market.volume_24h)}</td><td>{formatMoney(market.liquidity_dollars)}</td><td>{formatCloseDistance(market.close_at, now)}</td><td>{formatCloseTime(market.close_at)}</td></tr>
}
