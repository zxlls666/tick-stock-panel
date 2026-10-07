import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type KlineRow } from '@/lib/api'
import { klineDailyQueryOptions } from '@/lib/kline'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { Modal } from '@/components/Modal'
import {
  buildPlan,
  buildSpecString,
  normalizeParams,
  type CatalogIndicator,
  type IndicatorParams,
} from '@/lib/indicatorCatalog'
import {
  EChartsCandlestick,
  type ChartMarker,
  type ChartPriceLine,
  type ChartRange,
  type OHLC,
  type VolumeCompareConfig,
} from '@/components/EChartsCandlestick'

const SUB_INFO_H = 16
const SUB_GAP = 4
const PANEL_H = 72
const PANEL_H_VOLUME = 84
const DEFAULT_VOLUME_COMPARE: VolumeCompareConfig = { enabled: true, days: 1 }
/** 默认开启的指标 (主图均线 + 成交量副图)。 */
const DEFAULT_ACTIVE = ['ma', 'vol']

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

/** 指标目录 — 全应用共享同一 query 缓存 (后端注册表, 见 ADR-006)。 */
function useIndicatorCatalog() {
  return useQuery({
    queryKey: QK.indicatorCatalog,
    queryFn: () => api.indicatorCatalog(),
    staleTime: 30 * 60_000,
  })
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
      <span className="w-20 shrink-0 text-xs text-secondary">{label}</span>
      <input
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={hint}
        className="min-w-0 flex-1 rounded border border-border bg-base px-2 py-1 text-xs font-mono text-primary outline-none focus:border-accent"
      />
    </label>
  )
}

/** 指标开关 chip (选择器面板用)。 */
function IndicatorChip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
        active ? 'bg-accent/20 text-accent' : 'bg-elevated text-muted hover:text-secondary'
      }`}
    >
      {label}
    </button>
  )
}

/** 选择器分组行。 */
function PickerGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-2">
      <span className="w-14 shrink-0 pt-0.5 text-[10px] text-muted">{label}</span>
      <div className="flex flex-wrap items-center gap-1.5">{children}</div>
    </div>
  )
}

/** 指标参数设置面板: 行由后端目录的 params schema 驱动 (ADR-006)。 */
function IndicatorSettingsPanel({ catalog, params, onChange, onClose }: {
  catalog: CatalogIndicator[]
  params: IndicatorParams
  onChange: (p: IndicatorParams) => void
  onClose: () => void
}) {
  const withParams = useMemo(() => catalog.filter(i => i.params.length > 0), [catalog])
  const [draft, setDraft] = useState<Record<string, string>>(() =>
    Object.fromEntries(withParams.map(i => [i.id, (params[i.id] ?? []).join(', ')])),
  )

  const apply = () => {
    const next: Record<string, number[]> = {}
    for (const [id, text] of Object.entries(draft)) next[id] = parsePeriods(text)
    onChange(normalizeParams(catalog, next))
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
      {withParams.map(i => (
        <SettingRow
          key={i.id}
          label={i.label}
          value={draft[i.id] ?? ''}
          hint={i.params.map(p => `${p.label} ${p.min}-${p.max}`).join(' / ')}
          onChange={v => setDraft(d => ({ ...d, [i.id]: v }))}
        />
      ))}
      <div className="flex justify-end gap-2 pt-1">
        <button onClick={() => { onChange(normalizeParams(catalog, {})); onClose() }} className="px-3 py-1 rounded text-xs bg-elevated text-muted hover:text-secondary cursor-pointer">恢复默认</button>
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
  showInfoBar = true,
  visibleBars = 60,
  linkedPrice,
  onDateClick,
  onPriceDoubleClick,
  extColumns,
}: Props) {
  const [activeIds, setActiveIds] = useState<string[]>(DEFAULT_ACTIVE)
  const [showMarkers, setShowMarkers] = useState(true)
  const [showSettings, setShowSettings] = useState(false)
  const [showPicker, setShowPicker] = useState(false)
  const [volumeCompare, setVolumeCompare] = useState<VolumeCompareConfig>(() =>
    normalizeVolumeCompare(storage.stockVolumeCompare.get(DEFAULT_VOLUME_COMPARE)),
  )
  // 原始参数持久化; 归一化需等目录到达 (params schema 决定取值范围)
  const [rawParams, setRawParams] = useState<unknown>(() => storage.chartIndicatorParams.get(null))
  const dateRange = externalDateRange ?? getDefaultRange()

  const catalogQuery = useIndicatorCatalog()
  const catalog = useMemo(() => catalogQuery.data?.indicators ?? [], [catalogQuery.data])

  const params = useMemo(() => normalizeParams(catalog, rawParams), [catalog, rawParams])
  const indicatorSpecs = useMemo(() => buildSpecString(catalog, params), [catalog, params])
  const plan = useMemo(() => buildPlan(catalog, activeIds, params), [catalog, activeIds, params])

  // 已开启的 TA 指标数据列 (逗号分隔); 未开启/未装 extra 时为空 → 请求与 cache key 不变
  const taColumns = useMemo(() => catalog
    .filter(i => i.source === 'ta' && i.available && activeIds.includes(i.id))
    .flatMap(i => i.outputs.map(o => o.canonical_col))
    .filter((c): c is string => !!c)
    .join(','), [catalog, activeIds])

  // 选择器分组 (目录驱动; TA 未装时该组不出现)
  const pickerGroups = useMemo(() => {
    const avail = catalog.filter(i => i.available)
    return [
      { label: '主图叠加', items: avail.filter(i => i.pane === 'overlay' && i.source === 'builtin') },
      { label: '副图', items: avail.filter(i => i.pane === 'panel' && i.source === 'builtin') },
      { label: 'TA', items: avail.filter(i => i.source === 'ta') },
    ].filter(g => g.items.length > 0)
  }, [catalog])

  // 查询配置统一来自 klineDailyQueryOptions, 与 StockPanel 信息条/邻近预取共享同一 cache key (只发一次请求)
  const kline = useQuery({
    ...klineDailyQueryOptions(symbol, dateRange, extColumns, taColumns || undefined, indicatorSpecs || undefined),
    enabled: !!symbol,
  })

  const rows = useMemo(() => toOHLC(kline.data?.rows ?? []), [kline.data?.rows])
  const stockInfo = kline.data?.stock_info
  const limitMarkers = useMemo(() => buildLimitUpMarkers(kline.data?.rows ?? []), [kline.data?.rows])
  const allMarkers = useMemo(() => [
    ...(markers ?? []),
    ...(showLimitMarkers ? limitMarkers : []),
  ], [limitMarkers, markers, showLimitMarkers])

  const toggleIndicator = useCallback((id: string) => {
    setActiveIds(prev => prev.includes(id) ? prev.filter(k => k !== id) : [...prev, id])
  }, [])

  const updateVolumeCompare = useCallback((patch: Partial<VolumeCompareConfig>) => {
    setVolumeCompare(prev => {
      const next = normalizeVolumeCompare({ ...prev, ...patch })
      storage.stockVolumeCompare.set(next)
      return next
    })
  }, [])

  const updateParams = useCallback((next: IndicatorParams) => {
    setRawParams(next)
    storage.chartIndicatorParams.set(next)
  }, [])

  // 容器高度: 主图 + 各副图 (成交量副图更高)
  const panelPlan = plan.filter(p => p.pane === 'panel')
  let subExtraH = 0
  panelPlan.forEach(p => { subExtraH += SUB_INFO_H + (p.special === 'volume' ? PANEL_H_VOLUME : PANEL_H) })
  if (panelPlan.length > 0) subExtraH += panelPlan.length * SUB_GAP + 14
  const chartHeight = height + subExtraH

  if (!symbol) return null

  return (
    <div className={className} style={{ minHeight: chartHeight }}>
      {showIndicatorControls && rows.length > 0 && (
        <>
        <div className="flex items-center gap-1.5 px-1 pb-0.5">
          <button
            onClick={() => setShowPicker(v => !v)}
            aria-expanded={showPicker}
            className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
              showPicker ? 'bg-accent/20 text-accent' : 'bg-elevated text-muted hover:text-secondary'
            }`}
          >
            指标 {showPicker ? '▴' : '▾'}
          </button>
          {activeIds.includes('vol') && (
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
        {showPicker && (
          <div className="flex flex-col gap-1 px-1 pb-1">
            {pickerGroups.map(g => (
              <PickerGroup key={g.label} label={g.label}>
                {g.items.map(ind => (
                  <IndicatorChip
                    key={ind.id}
                    label={ind.label}
                    active={activeIds.includes(ind.id)}
                    onClick={() => toggleIndicator(ind.id)}
                  />
                ))}
              </PickerGroup>
            ))}
          </div>
        )}
        </>
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
          showInfoBar={showInfoBar}
          showMarkers={showMarkers}
          stockInfo={stockInfo}
          symbol={symbol}
          linkedPrice={linkedPrice}
          onDateClick={onDateClick}
          onPriceDoubleClick={onPriceDoubleClick}
          visibleBars={visibleBars}
          volumeCompare={volumeCompare}
          indicators={plan}
        />
      )}
      {showSettings && (
        <Modal
          onClose={() => setShowSettings(false)}
          ariaLabel="指标参数设置"
          panelClassName="w-[92vw] max-w-sm bg-surface border border-border rounded-card shadow-xl"
        >
          <IndicatorSettingsPanel
            catalog={catalog}
            params={params}
            onChange={updateParams}
            onClose={() => setShowSettings(false)}
          />
        </Modal>
      )}
    </div>
  )
}
