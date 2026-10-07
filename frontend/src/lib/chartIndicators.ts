/**
 * 图表指标参数 (日 K 图): 默认值、别名列命名与请求编码。
 *
 * 与后端 `app/indicators/custom.py` 的契约保持一致 (见 docs/adr/ADR-005):
 * - 默认参数 → 使用规范列 (`ma5`/`macd_dif`/…), 不产生别名列、不带请求参数。
 * - 自定义参数 → 请求 `indicator_specs`, 后端返回别名列 (`<kind>_<params>_c`)。
 *
 * 别名列命名必须与后端一致, 否则图表取不到数据。
 */

export type ChartIndicatorKind = 'ma' | 'rsi' | 'macd' | 'kdj' | 'boll'

export interface ChartIndicatorParams {
  ma: number[]
  rsi: number[]
  macd: [number, number, number]
  kdj: number[]
  boll: [number, number]
}

export const DEFAULT_CHART_PARAMS: ChartIndicatorParams = {
  ma: [5, 10, 20, 60],
  rsi: [6, 14, 24],
  macd: [12, 26, 9],
  kdj: [9],
  boll: [20, 2],
}

/** 主图均线配色 (与内置规范列 MA5/10/20/60 的取色一致)。 */
export const MA_COLORS = ['#A1A1AA', '#3B82F6', '#F97316', '#8B5CF6', '#06B6D4', '#EF4444']
export const RSI_COLORS = ['#FACC15', '#3B82F6', '#8B5CF6', '#06B6D4']
export const KDJ_COLORS = { k: '#FACC15', d: '#3B82F6', j: '#8B5CF6' } as const
export const MACD_COLORS = { dif: '#FACC15', dea: '#8B5CF6' } as const
export const BOLL_COLOR = '#E879F9'

export interface SeriesCol {
  col: string
  label: string
  color: string
}

/** 图表实际读取的列 (默认即规范列; 自定义为别名列)。 */
export interface IndicatorColumns {
  ma: SeriesCol[]
  macd: { dif: SeriesCol; dea: SeriesCol; hist: SeriesCol }
  rsi: SeriesCol[]
  kdj: { k: SeriesCol; d: SeriesCol; j: SeriesCol }
  boll: { upper: string; lower: string }
}

const MIN_PERIOD = 2
const MAX_PERIOD = 500
const MAX_MA = 6
const MAX_RSI = 4

function eqArr(a: readonly number[] | undefined, b: readonly number[]): boolean {
  if (!a || a.length !== b.length) return false
  return a.every((v, i) => v === b[i])
}

export function isMaCustom(p: ChartIndicatorParams): boolean {
  return !eqArr(p.ma, DEFAULT_CHART_PARAMS.ma)
}
export function isRsiCustom(p: ChartIndicatorParams): boolean {
  return !eqArr(p.rsi, DEFAULT_CHART_PARAMS.rsi)
}
export function isMacdCustom(p: ChartIndicatorParams): boolean {
  return !eqArr(p.macd, DEFAULT_CHART_PARAMS.macd)
}
export function isKdjCustom(p: ChartIndicatorParams): boolean {
  return !eqArr(p.kdj, DEFAULT_CHART_PARAMS.kdj)
}
export function isBollCustom(p: ChartIndicatorParams): boolean {
  return !eqArr(p.boll, DEFAULT_CHART_PARAMS.boll)
}

function maCol(period: number, custom: boolean): string {
  return custom ? `ma_${period}_c` : `ma${period}`
}
function rsiCol(period: number, custom: boolean): string {
  return custom ? `rsi_${period}_c` : `rsi_${period}`
}

/** 由参数派生图表要读取的列 (默认走规范列, 自定义走别名列)。 */
export function deriveColumns(p: ChartIndicatorParams): IndicatorColumns {
  const maCustom = isMaCustom(p)
  const ma: SeriesCol[] = p.ma.map((period, i) => ({
    col: maCol(period, maCustom),
    label: `MA${period}`,
    color: MA_COLORS[i % MA_COLORS.length],
  }))

  const rsiCustom = isRsiCustom(p)
  const rsi: SeriesCol[] = p.rsi.map((period, i) => ({
    col: rsiCol(period, rsiCustom),
    label: `RSI${period}`,
    color: RSI_COLORS[i % RSI_COLORS.length],
  }))

  const macdCustom = isMacdCustom(p)
  const [f, s, g] = p.macd
  const macdBase = macdCustom ? `macd_${f}_${s}_${g}` : 'macd'
  const macd = {
    dif: { col: macdCustom ? `${macdBase}_dif_c` : 'macd_dif', label: 'DIF', color: MACD_COLORS.dif },
    dea: { col: macdCustom ? `${macdBase}_dea_c` : 'macd_dea', label: 'DEA', color: MACD_COLORS.dea },
    hist: { col: macdCustom ? `${macdBase}_hist_c` : 'macd_hist', label: 'MACD', color: MA_COLORS[0] },
  }

  const kdjCustom = isKdjCustom(p)
  const [n] = p.kdj
  const kdjBase = kdjCustom ? `kdj_${n}` : 'kdj'
  const kdj = {
    k: { col: kdjCustom ? `${kdjBase}_k_c` : 'kdj_k', label: 'K', color: KDJ_COLORS.k },
    d: { col: kdjCustom ? `${kdjBase}_d_c` : 'kdj_d', label: 'D', color: KDJ_COLORS.d },
    j: { col: kdjCustom ? `${kdjBase}_j_c` : 'kdj_j', label: 'J', color: KDJ_COLORS.j },
  }

  const bollCustom = isBollCustom(p)
  const [bp, bs] = p.boll
  // 中轨 = (上轨+下轨)/2 (对称带), 无需额外列
  const boll = bollCustom
    ? { upper: `boll_${bp}_${bs}_upper_c`, lower: `boll_${bp}_${bs}_lower_c` }
    : { upper: 'boll_upper', lower: 'boll_lower' }

  return { ma, macd, rsi, kdj, boll }
}

export const DEFAULT_INDICATOR_COLUMNS: IndicatorColumns = deriveColumns(DEFAULT_CHART_PARAMS)

/** 拼 /api/kline/daily 的 indicator_specs 参数; 全默认时返回空串 (不带参数)。 */
export function buildSpecString(p: ChartIndicatorParams): string {
  const entries: string[] = []
  if (isMaCustom(p)) entries.push(`ma:${p.ma.join(':')}`)
  if (isRsiCustom(p)) entries.push(`rsi:${p.rsi.join(':')}`)
  if (isMacdCustom(p)) entries.push(`macd:${p.macd.join(':')}`)
  if (isKdjCustom(p)) entries.push(`kdj:${p.kdj.join(':')}`)
  if (isBollCustom(p)) entries.push(`boll:${p.boll.join(':')}`)
  return entries.join(',')
}

function clampInt(v: unknown, lo: number, hi: number, fallback: number): number {
  const n = Math.round(Number(v))
  if (!Number.isFinite(n)) return fallback
  return Math.max(lo, Math.min(hi, n))
}

function normArr(raw: unknown, def: number[], maxLen: number): number[] {
  if (!Array.isArray(raw)) return [...def]
  const seen = new Set<number>()
  const out: number[] = []
  for (const v of raw) {
    const n = clampInt(v, MIN_PERIOD, MAX_PERIOD, NaN)
    if (!Number.isFinite(n) || seen.has(n)) continue
    seen.add(n)
    out.push(n)
    if (out.length >= maxLen) break
  }
  return out.length > 0 ? out : [...def]
}

function normTriple(raw: unknown, def: [number, number, number]): [number, number, number] {
  if (!Array.isArray(raw) || raw.length !== 3) return [...def]
  const [f, s, g] = raw.map((v, i) => clampInt(v, MIN_PERIOD, MAX_PERIOD, def[i]))
  if (f >= s) return [...def] // 后端要求 fast < slow; 非法则回退默认
  return [f, s, g]
}

function normBoll(raw: unknown, def: [number, number]): [number, number] {
  if (!Array.isArray(raw) || raw.length !== 2) return [...def]
  return [
    clampInt(raw[0], MIN_PERIOD, MAX_PERIOD, def[0]),
    clampInt(raw[1], 1, 10, def[1]),
  ]
}

/** 归一化来自 localStorage 的任意输入, 越界/非法一律回退默认。 */
export function normalizeParams(raw: unknown): ChartIndicatorParams {
  const r = (raw ?? {}) as Partial<ChartIndicatorParams>
  return {
    ma: normArr(r.ma, DEFAULT_CHART_PARAMS.ma, MAX_MA),
    rsi: normArr(r.rsi, DEFAULT_CHART_PARAMS.rsi, MAX_RSI),
    macd: normTriple(r.macd, DEFAULT_CHART_PARAMS.macd),
    kdj: normArr(r.kdj, DEFAULT_CHART_PARAMS.kdj, 1),
    boll: normBoll(r.boll, DEFAULT_CHART_PARAMS.boll),
  }
}

/** 从行对象读取列值 (规范列或别名列), 统一转 number | null。 */
export function colVal(d: Record<string, unknown> | null | undefined, col: string): number | null {
  if (!d) return null
  const v = d[col]
  if (v == null) return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}
