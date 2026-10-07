import { useCallback, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type KlineRow } from '@/lib/api'
import { klineDailyQueryOptions } from '@/lib/kline'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { Modal } from '@/components/Modal'
import {
  DEFAULT_CHART_PARAMS,
  buildSpecString,
  deriveColumns,
  normalizeParams,
  type ChartIndicatorParams,
} from '@/lib/chartIndicators'
import {
  EChartsCandlestick,
  OVERLAY_INDICATORS,
  SUB_CHARTS,
  TA_SUB_CHARTS,
  type ChartMarker,
  type ChartPriceLine,
  type ChartRange,
  type OHLC,
  type VolumeCompareConfig,
} from '@/components/EChartsCandlestick'

const SUB_INFO_H = 16
const SUB_GAP = 4
const DEFAULT_VOLUME_COMPARE: VolumeCompareConfig = { enabled: true, days: 1 }

function normalizeVolumeCompare(config: VolumeCompareConfig): VolumeCompareConfig {
  return {
    enabled: config.enabled !== false,
    days: Math.max(1, Math.min(20, Math.round(Number(config.days) || 1))),
  }
}

interface Props {
  symbol: string
  height?: number
  className?: string
  dateRange?: { start: string; end: string }
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  showLimitMarkers?: boolean
  showIndicatorControls?: boolean
  showMarkerToggle?: boolean
  showMA?: boolean
  showInfoBar?: boolean
  /** 初始可见蜡烛根数; 'all' = 适配显示全部数据 */
  visibleBars?: number | 'all'
  linkedPrice?: number | null
  onDateClick?: (date: string) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  /** 扩展数据列参数（逗号分隔 config_id.field_name），透传给 klineDaily 接口 */
  extColumns?: string
}

function isValidRow(r: any): boolean {
  return r && r.date != null && r.open != null && r.close != null
}

export function toOHLC(rows: KlineRow[]): OHLC[] {
  return rows
    .filter(isValidRow)
    .map(r => ({
      ...(r as Record<string, unknown>),
      date: typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      close: Number(r.close),
      volume: Number(r.volume ?? 0),
    }))
}

function buildLimitUpMarkers(rows: KlineRow[]): ChartMarker[] {
  const markers: ChartMarker[] = []
  for (const r of rows) {
    const date = typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date)
    if (r.signal_broken_limit_up) {
      markers.push({ date, kind: 'neutral', above: true, color: '#8B5CF6', label: '炸' })
    } else if (r.signal_limit_up) {
      const boards: number = r.consecutive_limit_ups ?? 1
      markers.push({ date, kind: 'buy', above: true, color: '#FACC15', label: boards <= 1 ? '板' : String(boards) })
    }
  }
  return markers
}

export function getDefaultRange(): { start: string; end: string } {
  const now = new Date()
  const end = now.toISOString().slice(0, 10)
  const s = new Date(now)
  s.setMonth(s.getMonth() - 6)
  const start = s.toISOString().slice(0, 10)
  return { start, end }
}

/** 解析逗号/空格分隔的周期输入 → 正整数数组 (非法/越界交给 normalizeParams 回退默认)。 */
function parsePeriods(text: string): number[] {
  return text
    .split(/[^0-9]+/)
    .map(s => Number(s))
    .filter(n => Number.isFinite(n) && n > 0)
}

function SettingRow({ label, value, hint, onChange }: {
  label: string
  value: string
  hint: string
  onChange: (v: string) => void
}) {
  return (
    <label className="flex items-center gap-3">
      <span className="w-16 shrink-0 text-xs text-secondary">{label}</span>
      <input
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={hint}
        className="min-w-0 flex-1 rounded border border-border bg-base px-2 py-1 text-xs font-mono text-primary outline-none focus:border-accent"
      />
    </label>
  )
}

/** 指标参数设置面板: 改周期后由上层请求对应别名列 (见 lib/chartIndicators.ts 与 ADR-005)。 */
function IndicatorSettingsPanel({ params, onChange, onClose }: {
  params: ChartIndicatorParams
  onChange: (p: ChartIndicatorParams) => void
  onClose: () => void
}) {
  const [ma, setMa] = useState(params.ma.join(', '))
  const [rsi, setRsi] = useState(params.rsi.join(', '))
  const [macd, setMacd] = useState(params.macd.join(', '))
  const [kdj, setKdj] = useState(params.kdj.join(', '))
  const [boll, setBoll] = useState(params.boll.join(', '))

  const apply = () => {
    onChange(normalizeParams({
      ma: parsePeriods(ma),
      rsi: parsePeriods(rsi),
      macd: parsePeriods(macd),
      kdj: parsePeriods(kdj),
      boll: parsePeriods(boll),
    }))
    onClose()
  }
  const reset = () => {
    onChange(normalizeParams(DEFAULT_CHART_PARAMS))
    onClose()
  }

  return (
    <div className="space-y-3 p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-primary">指标参数</h3>
        <button onClick={onClose} aria-label="关闭" className="text-muted hover:text-secondary cursor-pointer">✕</button>
      </div>
      <p className="text-[11px] text-muted leading-4">
        改周期后自动向后端请求对应指标 (别名列), 不影响选股/回测口径。留空或非法值回退默认。
      </p>
      <SettingRow label="均线 MA" value={ma} hint="5, 10, 20, 60" onChange={setMa} />
      <SettingRow label="RSI" value={rsi} hint="6, 14, 24" onChange={setRsi} />
      <SettingRow label="MACD" value={macd} hint="12, 26, 9 (快,慢,信号)" onChange={setMacd} />
      <SettingRow label="KDJ" value={kdj} hint="9" onChange={setKdj} />
      <SettingRow label="BOLL" value={boll} hint="20, 2 (周期,倍数)" onChange={setBoll} />
      <div className="flex justify-end gap-2 pt-1">
        <button onClick={reset} className="px-3 py-1 rounded text-xs bg-elevated text-muted hover:text-secondary cursor-pointer">恢复默认</button>
        <button onClick={apply} className="px-3 py-1 rounded text-xs bg-accent/20 text-accent cursor-pointer">应用</button>
      </div>
    </div>
  )
}

export function StockDailyKChart({
  symbol,
  height = 520,
  className,
  dateRange: externalDateRange,
  markers,
  ranges,
  priceLines,
  showLimitMarkers = true,
  showIndicatorControls = true,
  showMarkerToggle = true,
  showMA = true,
  showInfoBar = true,
  visibleBars = 60,
  linkedPrice,
  onDateClick,
  onPriceDoubleClick,
  extColumns,
}: Props) {
  const [activeIndicators, setActiveIndicators] = useState<string[]>(['vol'])
  const [showMarkers, setShowMarkers] = useState(true)
  const [volumeCompare, setVolumeCompare] = useState<VolumeCompareConfig>(() =>
    normalizeVolumeCompare(storage.stockVolumeCompare.get(DEFAULT_VOLUME_COMPARE)),
  )
  const [indicatorParams, setIndicatorParams] = useState<ChartIndicatorParams>(() =>
    normalizeParams(storage.chartIndicatorParams.get(null)),
  )
  const [showSettings, setShowSettings] = useState(false)
  const dateRange = externalDateRange ?? getDefaultRange()

  // 自定义指标参数 → 请求编码 (全默认时为空 → 请求/cache key 与既有逐位一致)
  const indicatorSpecs = useMemo(() => buildSpecString(indicatorParams), [indicatorParams])
  const columns = useMemo(() => deriveColumns(indicatorParams), [indicatorParams])

  // 已开启的第三方 TA 子图对应的数据列 (逗号分隔); 未开启时为空 → 请求与 cache key 不变
  const taColumns = useMemo(
    () => activeIndicators
      .map(k => TA_SUB_CHARTS.find(s => s.key === k)?.column)
      .filter((c): c is string => !!c)
      .join(','),
    [activeIndicators],
  )

  // 查询配置统一来自 klineDailyQueryOptions, 与 StockPanel 信息条/邻近预取共享同一 cache key (只发一次请求)
  const kline = useQuery({
    ...klineDailyQueryOptions(symbol, dateRange, extColumns, taColumns || undefined, indicatorSpecs || undefined),
    enabled: !!symbol,
  })

  // TA 能力门控: 后端未装可选 extra `ta` 时因子库无 ta_ 条目, 不显示 TA 开关
  const taCap = useQuery({
    queryKey: QK.factorLibrary('stock'),
    queryFn: () => api.factorLibrary('stock'),
    staleTime: 5 * 60_000,
  })
  const taSubCharts = (taCap.data?.factors ?? []).some(f => f.id.startsWith('ta_')) ? TA_SUB_CHARTS : []

  const rows = useMemo(() => toOHLC(kline.data?.rows ?? []), [kline.data?.rows])
  const stockInfo = kline.data?.stock_info
  const limitMarkers = useMemo(() => buildLimitUpMarkers(kline.data?.rows ?? []), [kline.data?.rows])
  const allMarkers = useMemo(() => [
    ...(markers ?? []),
    ...(showLimitMarkers ? limitMarkers : []),
  ], [limitMarkers, markers, showLimitMarkers])

  const toggleIndicator = useCallback((key: string) => {
    setActiveIndicators(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  }, [])

  const updateVolumeCompare = useCallback((patch: Partial<VolumeCompareConfig>) => {
    setVolumeCompare(prev => {
      const next = normalizeVolumeCompare({ ...prev, ...patch })
      storage.stockVolumeCompare.set(next)
      return next
    })
  }, [])

  const updateIndicatorParams = useCallback((next: ChartIndicatorParams) => {
    const norm = normalizeParams(next)
    setIndicatorParams(norm)
    storage.chartIndicatorParams.set(norm)
  }, [])

  const activeSubDefs = activeIndicators
    .map(key => SUB_CHARTS.find(s => s.key === key))
    .filter((d): d is typeof SUB_CHARTS[number] => !!d)
  let subExtraH = 0
  activeSubDefs.forEach(def => { subExtraH += SUB_INFO_H + def.height })
  if (activeSubDefs.length > 0) subExtraH += activeSubDefs.length * SUB_GAP + 14
  const chartHeight = height + subExtraH

  if (!symbol) return null

  return (
    <div className={className} style={{ minHeight: chartHeight }}>
      {showIndicatorControls && rows.length > 0 && (
        <div className="flex items-center gap-1.5 px-1 pb-0.5">
          {[...SUB_CHARTS, ...taSubCharts].map(ind => (
            <button
              key={ind.key}
              onClick={() => toggleIndicator(ind.key)}
              className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                activeIndicators.includes(ind.key)
                  ? 'bg-accent/20 text-accent'
                  : 'bg-elevated text-muted hover:text-secondary'
              }`}
            >
              {ind.label}
            </button>
          ))}
          {OVERLAY_INDICATORS.map(ind => (
            <button
              key={ind.key}
              onClick={() => toggleIndicator(ind.key)}
              className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                activeIndicators.includes(ind.key)
                  ? 'bg-accent/20 text-accent'
                  : 'bg-elevated text-muted hover:text-secondary'
              }`}
            >
              {ind.label}
            </button>
          ))}
          {activeIndicators.includes('vol') && (
            <div className="ml-0.5 flex h-5 items-center gap-1.5 border-l border-border/70 pl-2">
              <span className="text-[10px] text-muted">量比</span>
              <button
                type="button"
                role="switch"
                aria-checked={volumeCompare.enabled}
                aria-label="开启量能对比"
                title={volumeCompare.enabled ? '关闭量能对比' : '开启量能对比'}
                onClick={() => updateVolumeCompare({ enabled: !volumeCompare.enabled })}
                className={`relative h-3.5 w-6 shrink-0 rounded-full transition-colors ${
                  volumeCompare.enabled ? 'bg-accent' : 'bg-elevated'
                }`}
              >
                <span className={`absolute left-0 top-0.5 h-2.5 w-2.5 rounded-full bg-white transition-transform ${
                  volumeCompare.enabled ? 'translate-x-3' : 'translate-x-0.5'
                }`} />
              </button>
              <select
                aria-label="量能对比周期"
                value={volumeCompare.days}
                disabled={!volumeCompare.enabled}
                onChange={event => updateVolumeCompare({ days: Number(event.target.value) })}
                className="h-5 rounded border border-border bg-base px-1 text-[10px] text-secondary outline-none disabled:opacity-40"
              >
                {Array.from({ length: 20 }, (_, index) => index + 1).map(days => (
                  <option key={days} value={days}>前{days}日均量</option>
                ))}
              </select>
            </div>
          )}
          <button
            onClick={() => setShowSettings(true)}
            title="指标参数设置"
            className="ml-auto px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors bg-elevated text-muted hover:text-secondary"
          >
            参数
          </button>
          {showMarkerToggle && showLimitMarkers && (
            <button
              onClick={() => setShowMarkers(v => !v)}
              className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                showMarkers
                  ? 'text-[#FACC15] bg-[#FACC15]/10'
                  : 'bg-elevated text-muted hover:text-secondary'
              }`}
            >
              异动
            </button>
          )}
        </div>
      )}
      {kline.isLoading && <div className="text-sm text-muted py-4">加载中…</div>}
      {kline.isError && <div className="text-sm text-danger py-2">日K加载失败</div>}
      {!kline.isLoading && !kline.isError && (kline.data?.rows?.length ?? 0) > 0 && rows.length === 0 && (
        <div className="text-sm text-danger py-2">数据格式异常，请刷新页面</div>
      )}
      {rows.length > 0 && (
        <EChartsCandlestick
          data={rows}
          markers={allMarkers}
          ranges={ranges}
          priceLines={priceLines}
          height={chartHeight - 22}
          showMA={showMA}
          showInfoBar={showInfoBar}
          showMarkers={showMarkers}
          stockInfo={stockInfo}
          symbol={symbol}
          linkedPrice={linkedPrice}
          onDateClick={onDateClick}
          onPriceDoubleClick={onPriceDoubleClick}
          visibleBars={visibleBars}
          activeIndicators={activeIndicators}
          volumeCompare={volumeCompare}
          extraSubCharts={taSubCharts}
          columns={columns}
        />
      )}
      {showSettings && (
        <Modal
          onClose={() => setShowSettings(false)}
          ariaLabel="指标参数设置"
          panelClassName="w-[92vw] max-w-sm bg-surface border border-border rounded-card shadow-xl"
        >
          <IndicatorSettingsPanel
            params={indicatorParams}
            onChange={updateIndicatorParams}
            onClose={() => setShowSettings(false)}
          />
        </Modal>
      )}
    </div>
  )
}
