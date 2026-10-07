/**
 * 图表指标目录 — 后端 `/api/indicators/catalog` 的类型、参数状态与列解析。
 *
 * 见 docs/adr/ADR-006: 选择器与渲染由**后端注册表**驱动; `col_template` 与后端
 * `indicators.custom` 的别名列命名一致 (ADR-005), 前端不再硬编码指标清单与列名。
 */
export type Pane = 'overlay' | 'panel'

export interface CatalogParamSpec {
  key: string
  label: string
  kind: 'scalar' | 'list'
  default: number[]
  min: number
  max: number
}

export interface CatalogOutputSpec {
  key: string
  type: 'line' | 'bar'
  expand_param: string | null
  label: string
  color: string | null
  canonical_col: string | null
  label_template: string
  canonical_template: string | null
  palette: string[]
  col_template: string | null
}

export interface CatalogIndicator {
  id: string
  label: string
  pane: Pane
  source: 'builtin' | 'ta'
  available: boolean
  special: string | null
  y_axis: { min?: number; max?: number } | null
  params: CatalogParamSpec[]
  outputs: CatalogOutputSpec[]
}

export interface IndicatorCatalogResponse {
  indicators: CatalogIndicator[]
}

/** id → 平铺后的参数值 (缺省 = 用描述符默认)。 */
export type IndicatorParams = Record<string, number[]>

export interface ResolvedOutput {
  key: string
  label: string
  color: string
  type: 'line' | 'bar'
  col: string
}

/** 渲染计划中的一条指标 (输出列已按当前参数解析)。 */
export interface RenderIndicator {
  id: string
  label: string
  pane: Pane
  special: string | null
  yAxis: { min?: number; max?: number } | null
  params: CatalogParamSpec[]
  outputs: ResolvedOutput[]
}

export function renderTemplate(tpl: string, values: Record<string, number>): string {
  return tpl.replace(/\{(\w+)\}/g, (_, k: string) => String(values[k] ?? ''))
}

function eqArr(a: readonly number[] | undefined, b: readonly number[]): boolean {
  return !!a && a.length === b.length && a.every((v, i) => v === b[i])
}

function clampInt(v: unknown, lo: number, hi: number, fallback: number): number {
  const n = Math.round(Number(v))
  if (!Number.isFinite(n)) return fallback
  return Math.max(lo, Math.min(hi, n))
}

/** 某个 indicator 的默认平铺参数。 */
function defaultFlat(ind: CatalogIndicator): number[] {
  const out: number[] = []
  for (const p of ind.params) out.push(...p.default)
  return out
}

/** 平铺参数 → {paramKey: 值 | 值数组} (供模板渲染)。 */
export function paramValues(ind: CatalogIndicator, flat: number[]): Record<string, number | number[]> {
  const values: Record<string, number | number[]> = {}
  let i = 0
  for (const p of ind.params) {
    if (p.kind === 'list') {
      values[p.key] = flat.slice(i)
      i = flat.length
    } else {
      values[p.key] = flat[i] ?? p.default[0]
      i += 1
    }
  }
  return values
}

export function isCustom(ind: CatalogIndicator, flat: number[] | undefined): boolean {
  return !!flat && !eqArr(flat, defaultFlat(ind))
}

export function defaultParams(catalog: CatalogIndicator[]): IndicatorParams {
  const out: IndicatorParams = {}
  for (const ind of catalog) if (ind.params.length) out[ind.id] = defaultFlat(ind)
  return out
}

/** 归一化来自 localStorage 的参数;越界/非法回退默认。 */
export function normalizeParams(catalog: CatalogIndicator[], raw: unknown): IndicatorParams {
  const r = (raw ?? {}) as Record<string, unknown>
  const out: IndicatorParams = {}
  for (const ind of catalog) {
    if (!ind.params.length) continue
    const def = defaultFlat(ind)
    const given = r[ind.id]
    if (!Array.isArray(given)) {
      out[ind.id] = def
      continue
    }
    const flat: number[] = []
    let i = 0
    for (const p of ind.params) {
      if (p.kind === 'list') {
        const seen = new Set<number>()
        const list: number[] = []
        for (const v of given.slice(i)) {
          const n = clampInt(v, p.min, p.max, Number.NaN)
          if (!Number.isFinite(n) || seen.has(n)) continue
          seen.add(n)
          list.push(n)
        }
        flat.push(...(list.length ? list : p.default))
        i = given.length
      } else {
        flat.push(clampInt(given[i], p.min, p.max, p.default[0]))
        i += 1
      }
    }
    // MACD 类要求 fast<slow (与后端一致, 非法回退默认)
    const badMacd = ind.id === 'macd' && !(flat[0] < flat[1])
    out[ind.id] = badMacd ? def : flat
  }
  return out
}

/** 拼 /api/kline/daily 的 indicator_specs;全默认时为空串。 */
export function buildSpecString(catalog: CatalogIndicator[], params: IndicatorParams): string {
  const entries: string[] = []
  for (const ind of catalog) {
    if (!ind.params.length || !isCustom(ind, params[ind.id])) continue
    entries.push(`${ind.id}:${(params[ind.id] ?? defaultFlat(ind)).join(':')}`)
  }
  return entries.join(',')
}

/** 解析一条指标的输出列 (默认走规范列, 自定义走别名列模板)。 */
export function resolveIndicator(ind: CatalogIndicator, flat: number[] | undefined): RenderIndicator {
  const values = paramValues(ind, flat ?? defaultFlat(ind))
  const custom = isCustom(ind, flat)
  const outputs: ResolvedOutput[] = []
  let z = 0

  for (const out of ind.outputs) {
    if (out.expand_param) {
      const list = values[out.expand_param] as number[]
      for (const v of list) {
        const tmplValues: Record<string, number> = {}
        for (const [k, val] of Object.entries(values)) tmplValues[k] = Array.isArray(val) ? val[0] : val
        tmplValues[out.expand_param] = v
        const tpl = custom && out.col_template ? out.col_template : out.canonical_template
        outputs.push({
          key: `${out.key}:${v}`,
          label: renderTemplate(out.label_template, tmplValues),
          color: out.palette[z % out.palette.length] ?? '#8E8E96',
          type: out.type,
          col: renderTemplate(tpl ?? '', tmplValues),
        })
        z += 1
      }
    } else {
      const tmplValues: Record<string, number> = {}
      for (const [k, val] of Object.entries(values)) tmplValues[k] = Array.isArray(val) ? val[0] : val
      const tpl = custom && out.col_template ? out.col_template : out.canonical_col
      outputs.push({
        key: out.key,
        label: out.label,
        color: out.color ?? '#8E8E96',
        type: out.type,
        col: out.canonical_col && !custom ? out.canonical_col : renderTemplate(tpl ?? out.canonical_col ?? '', tmplValues),
      })
    }
  }

  return {
    id: ind.id,
    label: ind.label,
    pane: ind.pane,
    special: ind.special,
    yAxis: ind.y_axis,
    params: ind.params,
    outputs,
  }
}

/** 生成渲染计划 (仅取已启用且 available 的指标, 保持 activeIds 顺序)。 */
export function buildPlan(
  catalog: CatalogIndicator[],
  activeIds: string[],
  params: IndicatorParams,
): RenderIndicator[] {
  const byId = new Map(catalog.map(i => [i.id, i]))
  const plan: RenderIndicator[] = []
  for (const id of activeIds) {
    const ind = byId.get(id)
    if (!ind || !ind.available) continue
    plan.push(resolveIndicator(ind, params[id]))
  }
  return plan
}

/** 从行对象读取列值并转 number | null。 */
export function colVal(d: Record<string, unknown> | null | undefined, col: string): number | null {
  if (!d || !col) return null
  const v = d[col]
  if (v == null) return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}
