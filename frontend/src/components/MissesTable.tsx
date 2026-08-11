import { flexRender, getCoreRowModel, useReactTable, type ColumnDef } from '@tanstack/react-table'
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, X } from 'lucide-react'
import { useMemo } from 'react'
import type { ConfidenceBand, MissedMarket, MissesResponse } from '../api/types'

interface Props {
  data: MissesResponse
  band: ConfidenceBand | null
  page: number
  sort: string
  direction: 'asc' | 'desc'
  onClearBand: () => void
  onPage: (page: number) => void
  onSort: (sort: string) => void
}

const formatDate = (value: string | null) => value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '—'

export function MissesTable({ data, band, page, sort, direction, onClearBand, onPage, onSort }: Props) {
  const columns = useMemo<ColumnDef<MissedMarket>[]>(() => {
    const sortable = (field: string, label: string) => <button className="sort-button" onClick={() => onSort(field)}>{label} {sort === field && (direction === 'desc' ? <ArrowDown /> : <ArrowUp />)}</button>
    return [
      { accessorKey: 'title', header: () => sortable('title', 'Market'), cell: ({ row }) => <div className="market-name"><strong>{row.original.title}</strong><span>{row.original.ticker}</span></div> },
      { accessorKey: 'peak_confidence', header: () => sortable('peak_confidence', 'Peak confidence'), cell: ({ getValue }) => <strong className="confidence">{Number(getValue()).toFixed(0)}%</strong> },
      { accessorKey: 'losing_side', header: () => sortable('losing_side', 'Losing side'), cell: ({ getValue }) => <span className="losing-side">{String(getValue()).toUpperCase()}</span> },
      { accessorKey: 'first_crossed_at', header: () => sortable('first_crossed_at', 'First crossed'), cell: ({ getValue }) => <time>{formatDate(getValue() as string | null)}</time> },
      { accessorKey: 'settled_at', header: () => sortable('settled_at', 'Settled'), cell: ({ getValue }) => <time>{formatDate(getValue() as string)}</time> },
    ]
  }, [direction, onSort, sort])

  const table = useReactTable({ data: data.items, columns, getCoreRowModel: getCoreRowModel(), manualPagination: true, rowCount: data.total })
  const first = data.total === 0 ? 0 : (page - 1) * data.page_size + 1
  const last = Math.min(page * data.page_size, data.total)

  return <section className="table-panel" aria-labelledby="misses-heading">
    <header className="panel-heading table-heading">
      <div><h2 id="misses-heading">{band ? `${band.label} confidence misses` : 'Wrong high-confidence predictions'}</h2><span>{data.total.toLocaleString()} markets</span></div>
      {band && <button className="secondary-button" onClick={onClearBand}><X /> Show all bands</button>}
    </header>
    {data.total === 0 ? <div className="empty-state"><strong>No missed markets found</strong><span>Try a lower threshold or load a wider time frame.</span></div> : <div className="table-scroll"><table>
      <thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead>
      <tbody>{table.getRowModel().rows.map((row) => <tr key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody>
    </table></div>}
    <footer className="pagination"><span>{first.toLocaleString()}–{last.toLocaleString()} of {data.total.toLocaleString()}</span><div><button aria-label="Previous page" disabled={page <= 1} onClick={() => onPage(page - 1)}><ChevronLeft /></button><button aria-label="Next page" disabled={page >= data.pages} onClick={() => onPage(page + 1)}><ChevronRight /></button></div></footer>
  </section>
}
