import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'
import type { ConfidenceBand } from '../api/types'

echarts.use([BarChart, GridComponent, TooltipComponent, SVGRenderer])

interface Props {
  bands: ConfidenceBand[]
  selected: ConfidenceBand | null
  onSelect: (band: ConfidenceBand) => void
}

export function ConfidenceChart({ bands, selected, onSelect }: Props) {
  const elementRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!elementRef.current) return
    const chart = echarts.init(elementRef.current, undefined, { renderer: 'svg' })
    const style = getComputedStyle(elementRef.current)
    const danger = style.getPropertyValue('--chart-danger').trim()
    const muted = style.getPropertyValue('--chart-muted').trim()
    const ink = style.getPropertyValue('--chart-ink').trim()
    const line = style.getPropertyValue('--chart-line').trim()
    const selectedColor = style.getPropertyValue('--chart-accent').trim()
    chart.setOption({
      animationDurationUpdate: 180,
      grid: { left: 48, right: 12, top: 28, bottom: 38 },
      tooltip: { trigger: 'item', formatter: (value: { name: string; value: number }) => `${value.name}<br/>${value.value.toLocaleString()} wrong markets` },
      xAxis: { type: 'category', data: bands.map((band) => band.label), axisTick: { show: false }, axisLine: { lineStyle: { color: line } }, axisLabel: { color: muted } },
      yAxis: { type: 'value', minInterval: 1, axisLabel: { color: muted }, splitLine: { lineStyle: { color: line } } },
      series: [{
        type: 'bar',
        data: bands.map((band) => ({
          value: band.count,
          itemStyle: { color: selected?.min_percent === band.min_percent ? selectedColor : danger, borderRadius: [6, 6, 2, 2] },
        })),
        barMaxWidth: 78,
        label: { show: true, position: 'top', color: ink, fontWeight: 600 },
      }],
    })
    chart.on('click', (event) => {
      if (typeof event.dataIndex === 'number' && bands[event.dataIndex]) onSelect(bands[event.dataIndex])
    })
    const resize = new ResizeObserver(() => chart.resize())
    resize.observe(elementRef.current)
    return () => { resize.disconnect(); chart.dispose() }
  }, [bands, selected, onSelect])

  return <div ref={elementRef} className="confidence-chart" role="img" aria-label="Wrong predictions by peak confidence band" />
}
