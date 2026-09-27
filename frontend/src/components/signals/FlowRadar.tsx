import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type FlowRadarGroup, type FlowRadarHorizon, type FlowRadarSignal, type FlowRadarV2Signal } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Skeleton } from '@/components/data/Skeleton'

const HORIZONS = ['1', '5', '10', '20']

function pct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(2)}%`
}

function count(value: number | null | undefined): string {
  if (value == null) return '—'
  return value.toLocaleString('zh-CN')
}

function HorizonTable({ horizons }: { horizons: Record<string, FlowRadarHorizon> }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-left text-[11px]">
        <thead className="text-muted">
          <tr>
            <th className="py-1 pr-3 font-medium">持有</th>
            <th className="py-1 pr-3 font-medium">可成交样本</th>
            <th className="py-1 pr-3 font-medium">费后均值</th>
            <th className="py-1 pr-3 font-medium">费后胜率</th>
            <th className="py-1 pr-3 font-medium">费后中位数</th>
            <th className="py-1 pr-3 font-medium">最大有利</th>
            <th className="py-1 pr-3 font-medium">最大不利</th>
            <th className="py-1 pr-3 font-medium">收盘路径</th>
            <th className="py-1 font-medium">卖出受阻</th>
          </tr>
        </thead>
        <tbody>
          {HORIZONS.map(horizon => {
            const row = horizons[horizon]
            if (!row) return null
            return (
              <tr key={horizon} className="border-t border-border/70 text-secondary">
                <td className="py-1.5 pr-3 font-mono text-foreground">{horizon}日</td>
                <td className="py-1.5 pr-3 font-mono">{count(row.n)}</td>
                <td className="py-1.5 pr-3 font-mono">{pct(row.mean_net_ret)}</td>
                <td className="py-1.5 pr-3 font-mono">{pct(row.win_rate_net)}</td>
                <td className="py-1.5 pr-3 font-mono">{pct(row.median_net_ret)}</td>
                <td className="py-1.5 pr-3 font-mono">{pct(row.mean_mfe)}</td>
                <td className="py-1.5 pr-3 font-mono">{pct(row.mean_mae)}</td>
                <td className="py-1.5 pr-3 font-mono">{pct(row.mean_price_ret)}</td>
                <td className="py-1.5 font-mono">{count(row.n_exit_blocked)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

const FAILURE_LABEL: Record<string, string> = {
  signal_days: '可用信号日不足 100',
  mean: '均值门槛未过',
  median: '中位数门槛未过',
  win_rate: '正收益信号日不超过一半',
  negative_rate: '负超额信号日不超过一半',
  newey_west: 'Newey-West t 未过',
  years: '不少于 30 个信号日的年份不足 3 个',
  year_sign: '年度方向不足三分之二',
}

function V2Card({ signal }: { signal: FlowRadarV2Signal }) {
  const observed = signal.gate.observed
  const rate = signal.direction === 'long' ? observed.win_rate : observed.negative_rate
  const rateLabel = signal.direction === 'long' ? '正收益日' : '负超额日'
  return (
    <article className="rounded-card border border-border bg-base p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium text-foreground">{signal.name}</h3>
        <span className={`rounded px-1.5 py-0.5 text-[10px] ${
          signal.status === 'historical_rejected'
            ? 'bg-amber-400/10 text-amber-300'
            : signal.status === 'historical_candidate'
              ? 'bg-emerald-400/10 text-emerald-300'
              : 'bg-sky-400/10 text-sky-300'
        }`}>{signal.status_label}</span>
        <span className="rounded bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted">
          {signal.direction === 'long' ? '做多候选' : '风险过滤'}
        </span>
        {signal.definition_matches_spec === false && (
          <span className="rounded bg-warning/10 px-1.5 py-0.5 text-[10px] text-warning">条件已偏离规格，统计按文件里的条件</span>
        )}
      </div>
      <p className="mt-1 font-mono text-[11px] text-muted">{signal.column}</p>
      <p className="mt-3 text-xs leading-5 text-secondary">{signal.risk_label}</p>
      <p className="mt-2 text-[11px] leading-5 text-secondary"><span className="text-muted">公式 </span>{signal.formula}</p>
      <p className="mt-2 text-[11px] text-muted">
        个股触发 {count(signal.events)} · 信号日 {count(signal.signal_days)} · 每日股票数 {signal.names_per_day.min ?? '—'} / {signal.names_per_day.median ?? '—'} / {signal.names_per_day.max ?? '—'}
      </p>
      <p className="mt-2 text-[11px] leading-5 text-secondary">
        5 日{signal.direction === 'long' ? '费后' : '超额'}均值 {pct(observed.mean)} · 中位数 {pct(observed.median)} · {rateLabel} {pct(rate)} · NW t {observed.newey_west_t ?? '—'}（lag {observed.newey_west_lag}）
      </p>
      <p className="mt-1 text-[11px] text-muted">
        {signal.gate.failures.length === 0
          ? (signal.status === 'draft'
            ? '冻结门槛的各项都满足。状态仍是研究中，要等历史确认报告落盘才迁出。'
            : '当前重算的冻结门槛各项都满足。官方状态以已落盘的历史确认为准。')
          : `未过：${signal.gate.failures.map(item => FAILURE_LABEL[item] ?? item).join('、')}`}
      </p>
      <p className="mt-2 text-[11px] leading-5 text-muted">{signal.status_note} 市值和市场状态只作披露。</p>
      {(signal.disclosure.by_size.length > 0 || signal.disclosure.by_regime.length > 0) && (
        <div className="mt-3 grid grid-cols-1 gap-2 lg:grid-cols-2">
          <GroupTable
            title="市值披露"
            note=""
            rows={signal.disclosure.by_size.map(row => ({
              key: row.key,
              n: row.signal_days,
              mean_net_ret: row.mean,
              win_rate_net: row.win_rate ?? null,
            }))}
          />
          <GroupTable
            title="市场状态披露"
            note=""
            rows={signal.disclosure.by_regime.map(row => ({
              key: row.key,
              n: row.signal_days,
              mean_net_ret: row.mean,
              win_rate_net: row.win_rate ?? null,
            }))}
          />
        </div>
      )}
    </article>
  )
}

function GroupTable({ title, rows, note }: { title: string; rows: FlowRadarGroup[]; note: string }) {
  return (
    <div className="rounded-md border border-border/80 bg-base px-3 py-2">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-[11px] font-medium text-foreground">{title}</span>
        <span className="text-[10px] text-muted">{note}</span>
      </div>
      {rows.length === 0 ? (
        <p className="text-[11px] text-muted">这个持有期没有可成交样本。</p>
      ) : (
        <ul className="space-y-1">
          {rows.map(row => (
            <li key={row.key || 'empty'} className="flex items-center justify-between gap-3 text-[11px] text-secondary">
              <span>{row.key || '未知'}</span>
              <span className="font-mono text-foreground/80">
                n {count(row.n)} · {pct(row.mean_net_ret)} · 胜率 {pct(row.win_rate_net)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function SignalCard({ signal }: { signal: FlowRadarSignal }) {
  const [open, setOpen] = useState(true)
  return (
    <article className="rounded-card border border-border bg-base p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-medium text-foreground">{signal.name}</h3>
            <span className="rounded bg-amber-400/10 px-1.5 py-0.5 text-[10px] text-amber-300">{signal.gate.label}</span>
            {!signal.enabled && <span className="rounded bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted">库中已停用，雷达仍按定义重算</span>}
            {!signal.definition_matches_spec && (
              <span className="rounded bg-warning/10 px-1.5 py-0.5 text-[10px] text-warning">条件已偏离规格，下面的统计按文件里的条件</span>
            )}
          </div>
          <p className="mt-1 font-mono text-[11px] text-muted">{signal.column}</p>
        </div>
        <button type="button" onClick={() => setOpen(value => !value)} className="text-[11px] text-secondary hover:text-foreground">
          {open ? '收起' : '展开'}
        </button>
      </div>
      <p className="mt-3 text-xs leading-5 text-secondary">{signal.risk_label}</p>
      {open && (
        <div className="mt-3 space-y-3">
          <p className="text-[11px] leading-5 text-secondary"><span className="text-muted">公式 </span>{signal.formula}</p>
          <p className="text-[11px] leading-5 text-secondary">
            <span className="text-muted">预热 </span>{signal.warmup_bars} 根。
            <span className="text-muted"> 失效 </span>{signal.invalid_when}
          </p>
          <p className="text-[11px] text-muted">
            触发 {count(signal.triggers)} · 次日可买 {count(signal.entry.tradable)} · 无次日行情 {count(signal.entry.no_next_bar)} · 停牌 {count(signal.entry.suspended)} · 一字涨停 {count(signal.entry.limit_up_locked)}
          </p>
          <HorizonTable horizons={signal.horizons} />
          <div className="grid grid-cols-1 gap-2 lg:grid-cols-3">
            <GroupTable title="年份 · 费后5日" rows={signal.by_year} note="" />
            <GroupTable title="市值 · 费后5日" rows={signal.by_size} note={signal.size_note} />
            <GroupTable title="市场状态 · 费后5日" rows={signal.by_regime} note={signal.regime_note} />
          </div>
          <div>
            <h4 className="mb-1 text-[11px] font-medium text-foreground">最近交易日触发</h4>
            {signal.candidates.length === 0 ? (
              <p className="text-[11px] text-muted">这一天没有触发。</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[720px] text-left text-[11px]">
                  <thead className="text-muted">
                    <tr>
                      <th className="py-1 pr-3 font-medium">代码</th>
                      <th className="py-1 pr-3 font-medium">名称</th>
                      <th className="py-1 pr-3 font-medium">分数</th>
                      {signal.evidence.map(item => (
                        <th key={item.column} className="py-1 pr-3 font-medium">{item.label}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {signal.candidates.map(row => (
                      <tr key={row.symbol} className="border-t border-border/70 text-secondary">
                        <td className="py-1.5 pr-3 font-mono text-foreground">{row.symbol}</td>
                        <td className="py-1.5 pr-3">{row.name || '—'}</td>
                        <td className="py-1.5 pr-3 font-mono">{row.score ?? '—'}</td>
                        {signal.evidence.map(item => (
                          <td key={item.column} className="py-1.5 pr-3 font-mono">
                            {row.evidence[item.column] ?? '—'}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </article>
  )
}

/** 盘后雷达：证据、历史统计和风险标签都从接口现算，页面不保存触发行。 */
export function FlowRadar() {
  const [tick, setTick] = useState(0)
  const report = useQuery({
    queryKey: QK.flowRadar(tick),
    queryFn: () => api.flowRadar(tick > 0),
    staleTime: 10 * 60 * 1000,
    refetchOnWindowFocus: false,
    placeholderData: previous => previous,
  })

  const recalculate = () => setTick(value => value + 1)

  if (report.isLoading) {
    return (
      <section className="space-y-3 rounded-card border border-border bg-surface p-4">
        <p className="text-xs text-secondary">正在从日线行情重算 V1 六条和 V2 两条。触发行不会另存。</p>
        <Skeleton h="h-16" />
        <Skeleton h="h-16" />
      </section>
    )
  }

  if (report.isError || !report.data) {
    return (
      <section className="rounded-card border border-border bg-surface p-4">
        <p className="text-sm text-secondary">雷达没有算出来。可以再试一次。</p>
        <button
          type="button"
          onClick={recalculate}
          className="mt-3 h-8 rounded-btn border border-border px-3 text-xs text-secondary hover:text-foreground"
        >
          重新计算
        </button>
      </section>
    )
  }

  const data = report.data
  return (
    <section className="space-y-3">
      <div className="rounded-card border border-border bg-surface p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="max-w-3xl space-y-2">
            <p className="text-xs leading-5 text-secondary">
              最近行情日 {data.as_of ?? '—'}。研究窗口 {data.study_start} .. {data.study_end ?? '—'}。
              行业分组：{data.industry.status}。{data.industry.detail}
            </p>
            <p className="text-[11px] leading-5 text-muted">{data.cost.detail} 触发结果不落库，刷新会按自定义信号表达式重算。</p>
          </div>
          <button
            type="button"
            onClick={recalculate}
            disabled={report.isFetching}
            className="h-8 shrink-0 rounded-btn border border-amber-400/30 bg-amber-400/5 px-3 text-xs text-amber-300 hover:bg-amber-400/10 disabled:opacity-50"
          >
            {report.isFetching ? '计算中' : '从行情重算'}
          </button>
        </div>
      </div>
      {data.signals.map(signal => <SignalCard key={signal.id} signal={signal} />)}
      {(data.v2 ?? []).length > 0 && (
        <div className="space-y-3">
          <div className="px-1">
            <h2 className="text-sm font-medium text-foreground">V2</h2>
            <p className="mt-1 text-[11px] leading-5 text-muted">
              {(data.v2 ?? []).every(item => item.status === 'draft')
                ? '和上面六条分开。状态保持研究中，历史确认报告落盘前不迁出。'
                : '和上面六条分开。官方状态来自已落盘的历史确认，门槛数字仍按当前行情重算。'}
            </p>
          </div>
          {data.v2?.map(signal => <V2Card key={signal.id} signal={signal} />)}
        </div>
      )}
    </section>
  )
}
