# Goal Document: A 股研究偏差修复与执行现实性升级

## Go / No-Go

- **Judgment**: Go，分四个必做阶段和一个数据门控阶段执行。
- **Reason**: 基准指数、严格 PIT 股本、退市样本、历史 ST、日线容量和冲击均有明确数据源或现有代码入口，可以形成可验证闭环；精确排队成交缺少历史分钟或 Level-2 数据，只能在数据到位后升级，当前不得伪造精确模型。

## Target Outcome

将 `reversal_monthly`、`abnormal_turnover_monthly_raw` 和 `abnormal_turnover_monthly_size_neutral` 从“当前有偏样本内稳定”升级为经过以下约束重跑、可复现且可审计的研究结果：

1. 股票池包含研究区间内已退市股票，并按上市日/退市日逐日构建；
2. ST 状态按历史生效日判断，不再使用当前名称回填历史；
3. 异常换手所用流通股本严格来自交易日当时可见的 PIT 状态，缺失时退出因子横截面，不回退当前股本；
4. 相对中证全指等基准计算超额收益、跟踪误差和信息比率；
5. 回测同时报告成交额容量、市场冲击、未成交和清仓天数；
6. 没有分钟或 Level-2 数据时，排队成交只以明确标记的乐观/基准/保守代理场景报告。

最终产物必须允许回答：当前收益是否来自幸存者偏差、ST/退市困境样本遗漏、未来股本回填、市场 Beta、小盘暴露或不可实现的成交假设。

## Goal Definition

- **Type**: technical + data quality + research validity
- **Boundary**:
  - 只升级研究数据、研究股票池、研究目标权重回测器和研究报告。
  - 继续复用 `MarketDataMatrix`、`MatcherConfig`、`ResearchPortfolioResult` 和现有三条候选策略。
  - 生产撮合器、公开选股、API 和前端不在本目标内。
  - 数据区间以当前主样本 `2020-09-09..2026-09-24` 为第一验收区间；后续增量不改变历史口径。
- **Non-goals**:
  - 不新增策略或因子。
  - 不在本轮补 E/P、股息率、PIT 行业或历史指数成分。
  - 不构建通用交易所撮合系统。
  - 不用日线或分钟 K 伪造 Level-2 委托队列。
  - 不把研究数据直接并入生产股票主表，除非单独通过生产数据迁移评审。
- **Deferred work**:
  - 历史 Level-2 委托簿、逐笔委托和集合竞价队列回放。
  - 行业中性化和历史指数成分内选股。
  - 容量模型按实盘成交记录校准。
- **Verification rule**: 每个阶段必须同时通过数据质量审计、行为测试和真实数据重跑；“文件已生成”不构成完成。
- **Evidence source**:
  - Parquet 数据审计和 manifest；
  - pytest 合成行为测试；
  - 完整历史研究产物；
  - 旧口径与新口径的桥接对照表；
  - `ruff`、`py_compile`、`git diff --check`。
- **Pass criteria**:
  - 基准覆盖、PIT 来源、退市/ST 股票池和成交容量均有逐日可审计字段；
  - 严格 PIT 研究中当前股本回退使用次数为 0；
  - 所有数据缺失均 fail-closed 或显式降级，缺失不得自动解释为非 ST、可交易或流动性充足；
  - 零容量约束/零冲击配置能复现当前研究回测器结果；
  - 最终报告同时给出绝对收益、相对收益、容量曲线和偏差修复前后差异；
  - 三条候选策略按最终停止规则重新判定，不预设必须通过。
- **Confidence note**: 日线容量和冲击只能证明保守可实现边界，不能证明真实排队成交；只有 Level-2 数据到位后才能提高队列模型置信度。
- **Judgment owner**: 自动测试和数据审计负责技术验收；是否解除 `research_only` 由用户在阅读最终对照报告后明确决定。

## Current State

### 已有能力

- 当前正式股票矩阵覆盖 `2020-09-09..2026-09-24`，共 1,466 个交易日、5,569 只股票。
- `data/financials/shares/part.parquet` 已有 175,880 个股本变更点、5,782 只股票，PIT as-of 覆盖率 99.8168%。
- `backend/app/share_capital.py` 已按 `announce_date`/`period_end` 做 backward as-of join，但历史缺失时仍回退当前 instruments 股本。
- DaoShare 探测已确认 `stock_basic(list_status="D")` 返回 340 只退市股票，研究区间内可查询历史数据。
- DaoShare 接口目录包含 `stock_st`、`st`、`namechange`、`daily`、`adj_factor`、`daily_basic` 和 `index_daily`。
- 本地已有 612 个指数标的，`KlineRepository.get_index_daily()` 可读取指数日 K。
- 沪深300、中证500、中证1000和中证全指的本地日 K 当前仅覆盖 `2025-09-08..2026-09-24`，不足以覆盖完整策略区间。
- `research_target_weights.py` 已支持固定成本、停牌、一字板、整手、现金和每日账户账本。

### 已知缺口

- 当前股票主表是单一快照，没有退市日，矩阵没有合并退市股票历史。
- 当前 ST 判断来自最新名称，不是 PIT 状态。
- 13,273 行历史行情缺少可信 PIT 股本；当前代码会回退最新股本。
- 当前完整历史报告没有指数基准、超额收益、跟踪误差或信息比率。
- 当前成本场景没有 ADV 容量、非线性冲击、部分成交或清仓天数。
- `data/kline_minute` 当前为 0B；没有历史 Level-2 数据。

### 已知风险

- 退市股票的复权、长期停牌、退市整理期和最终持仓估值处理可能显著改变结果。
- `namechange` 推导 ST 状态可能弱于直接 ST 状态接口，必须保留来源等级。
- 指数基准能消除市场 Beta，但不能单独消除规模暴露；因此同时报告中证全指和中证1000。
- 平方根冲击模型是场景模型，不是从本地成交记录拟合的真值。

## Plan Rewrite Notes

本目标重写并延续 `.planning/2026-09-28-a/research-target-weight-backtester-plan.md` 的 deferred work。

| Existing item | Decision | Reason |
|---|---|---|
| 基准指数与信息比率 | reorder，提前到 Phase 1 | 已有读取入口，成本低且最先检验市场 Beta/小盘 Beta |
| 完整 PIT 流通股本 | rewrite 为“严格模式” | 历史回填已完成 99.8168%，不是重新建设数据源 |
| 历史 ST、退市样本 | keep，合并为 PIT 股票池阶段 | 两者共同决定逐日可投资宇宙和涨跌停规则 |
| 容量与冲击 | keep，先做日线可验证版本 | 不等待分钟数据即可检验策略容量 |
| 排队成交 | split | 日线代理必做；精确队列由分钟/Level-2 数据门控 |
| E/P、股息率、PIT 行业 | remove from this goal | 与当前三条策略可信度闭环无直接依赖 |

## Drift Diagnosis

- **Goal drift**: 继续增加因子不能回答现有收益是否可实现，因此移出本目标。
- **Phase drift**: 数据缺口不能按“数据/代码/报告”横向并行；必须按“先基准和 PIT、再股票池、再执行现实性、最后统一重跑”的依赖顺序推进。
- **Validation drift**: 不能以“下载了退市/ST 文件”作为完成，必须证明逐日 universe、涨跌停规则和策略结果发生了可解释变化。
- **Compatibility drift**: 生产路径保留当前股本回退；研究路径显式 strict，不建立第二套隐式默认。
- **Cleanup drift**: 不顺手重构 provider、生产撮合器或前端。

## Priority Rationale

1. **先补基准和严格 PIT**：改动小、证据增益最大，可最快判断异常换手高收益是否主要来自市场/规模暴露或未来股本回填。
2. **再补退市和 ST**：这是幸存者偏差和困境股偏差的主要来源，依赖独立数据下载与复权审计，风险高于基准阶段。
3. **再补容量和冲击**：必须在最终股票池稳定后运行，否则会对错误 universe 校准容量。
4. **最后统一重跑**：所有新口径共享同一 manifest 和桥接报告，避免不同阶段结果不可比较。
5. **分钟/Level-2 最后且数据门控**：没有历史数据时继续设计精确排队模型只会制造伪精度。

## Data Contracts

### 1. Benchmark history

主基准：`000985.SH` 中证全指。

辅助基准：

- `000300.SH` 沪深300；
- `000905.SH` 中证500；
- `000852.SH` 中证1000。

使用项目现有指数日 K schema：

```text
symbol, date, open, high, low, close, volume, amount
```

第一正式交易日之前必须多加载一个交易日，用于计算首日 close-to-close 基准收益。

### 2. Strict PIT share capital

继续使用：

```text
data/financials/shares/part.parquet
```

增加研究来源分类，不要求持久化到生产 enriched：

```text
historical_pit | current_day | current_fallback | missing
```

严格研究规则：

```text
current_fallback / missing
=> float_shares = null
=> turnover_rate = null
=> 不参与异常换手截面排名
```

### 3. Delisted instruments and bars

研究隔离目录：

```text
data/research/a_share_universe/
  delisted_instruments.parquet
  delisted_daily/date=YYYY-MM-DD/part.parquet
```

`delisted_instruments.parquet` 最小字段：

```text
symbol, name, exchange, list_date, delist_date, delist_reason, source
```

退市日 K 最小字段：

```text
symbol, date, open, high, low, close, volume, amount, adj_factor, source
```

逐日 universe 语义：

```text
list_date <= trade_date < delist_date
```

若供应商语义表明退市日仍可交易，以实际最后交易日和供应商定义为准，但必须在 manifest 中固定版本。

### 4. ST history

研究隔离文件：

```text
data/research/a_share_universe/st_history.parquet
```

最小字段：

```text
symbol, effective_date, st_flag, st_type, name, source, source_date
```

查询语义：

```text
st_at(symbol, t) = effective_date <= t 的最后一条状态
```

来源等级：

```text
direct_st_history > reconstructed_from_namechange > unknown
```

`unknown` 在严格研究中不得自动当作非 ST；应退出需要 ST 判定的 universe，并计数。

### 5. Capacity and impact inputs

容量只使用 `t-1` 收盘前已知信息：

```text
ADV20[t] = mean(amount[t-20:t-1])
vol20[t] = std(daily_return[t-20:t-1])
participation[t] = requested_notional[t] / ADV20[t]
```

容量场景：1%、2%、5%、10% ADV。

冲击场景：

```text
impact_rate = eta * vol20 * sqrt(filled_notional / ADV20)
eta in {0.10, 0.25, 0.50}
```

总成本：

```text
commission + stamp_tax + fixed_slippage + modeled_impact
```

冲击参数和上限必须写入 manifest；不得在结果出来后反向选择参数。

## Phases

### Phase 0: 冻结数据快照与研究口径

- **Status**: Complete（2026-09-28）。基线重跑指纹 `4d81b8422ff735c0ba74ce6cf37f363f0fc19a949e531c2f48ff4e861a2c9b22` 一致；冻结 `data_generation=0b7900c875e347088914a263585b81e5`。
- **Purpose**: 让后续所有差异都能归因于指定修复，而不是数据静默变化。
- **Entry condition**: 当前正式历史回测产物和 commit `8fab631` 可读取。
- **Phase rules**:
  - 只做只读审计和 manifest，不改变策略定义。
  - 固定源数据日期、行数、SHA-256、generation 和代码 commit。
  - 不读取或提交 token、密钥和原始认证配置。
- **Todos**:
  - [x] 建立本目标 run manifest 模板。
    - **Surface**: `.planning` 文档、研究脚本。
    - **Proof**: 模板含代码 commit、数据 SHA、行数、日期、source、参数和降级列表。
    - **Depends on**: none。
  - [x] 记录股票、股本、指数和现有研究产物基线。
    - **Surface**: 数据审计脚本或一次性审计命令。
    - **Proof**: 输出股票/日期/行数/覆盖率与现有 52 路径 summary 指纹。
    - **Depends on**: manifest 模板。
- **Exit proof**: 同一快照重跑审计得到相同指纹。
- **Stop condition**: 基线文件正在被写入，或数据 generation 在审计期间变化。

### Phase 1: 基准指数与严格 PIT 股本

- **Status**: Functionally complete（2026-09-28）；进入 Phase 2 前尚需清理本阶段新增文件的 16 个 Ruff finding。
- **Evidence**:
  - 四只指数各 1,877 行，覆盖 `2019-01-02..2026-09-24`；策略日缺失为 0。
  - 定向测试 `17 passed`；bridge 审计通过，`current_fallback_count=0`。
  - `reversal_monthly` strict 总收益不变，中证全指超额 +109.78%，IR 0.64。
  - `abnormal_turnover_monthly_raw` strict 总收益由 +230.19% 降至 +201.26%（-28.93pp），中证全指超额仍 +197.89%，IR 1.42。
  - `abnormal_turnover_monthly_size_neutral` 仅下降 0.52pp，中证全指超额 +155.15%，IR 1.18。
  - `missing_count=919130` 是 `1466 × 5569` 稠密矩阵中的缺失单元格（11.26%），不是 919,130 条实际行情；不得与此前真实行情 PIT 覆盖率 99.8168% 直接比较。
- **Purpose**: 先消除市场 Beta 比较缺失和当前股本回填偏差。
- **Entry condition**: Phase 0 快照冻结。
- **Phase rules**:
  - 基准数据走现有指数存储和 `get_index_daily()`，不新造第二套指数仓库。
  - `apply_historical_float_shares()` 若增加 strict 参数，生产默认行为必须保持不变。
  - 严格 PIT 缺失默认退出排名，不填零、不使用当前股本。
- **Todos**:
  - [x] 回填四只基准指数至完整研究区间并多含一个前置交易日。
    - **Surface**: `data/kline_index_daily/`、现有指数同步入口。
    - **Proof**: 四只指数覆盖所有正式策略交易日；无重复 `(symbol,date)`；OHLC 有效。
    - **Depends on**: Phase 0。
  - [x] 为历史股本解析增加研究 strict 模式和来源标记。
    - **Surface**: `backend/app/share_capital.py` 及测试。
    - **Proof**: 合成测试证明 strict 下缺失返回 null，默认模式仍回退当前股本。
    - **Depends on**: none。
  - [x] 让异常换手研究只消费 `historical_pit/current_day` 股本。
    - **Surface**: 研究矩阵/运行脚本。
    - **Proof**: 所有入选股票日的 `current_fallback_count == 0`，缺失退出数写入 summary。
    - **Depends on**: strict 模式。
  - [x] 增加基准收益和主动收益统计。
    - **Surface**: `run_a_share_strategy_validation.py` 或研究统计模块。
    - **Proof**: 合成序列验证 relative NAV、tracking error 和 information ratio。
    - **Depends on**: 基准数据。
- **Exit proof**:
  - 四只基准正式区间覆盖率 100%，或仅有经确认的非交易日差异且策略日缺失为 0；
  - 异常换手有限值均来自严格 PIT 股本；
  - 生成绝对/相对收益桥接报告。
- **Stop condition**:
  - 任一策略正式交易日无主基准收益；
  - strict 模式仍出现当前股本回退；
  - 默认生产股本行为发生回归。

### Phase 2: 退市样本、历史 ST 与逐日 universe

- **Status**: Complete（2026-09-28）。退市前复权、SH/SZ PIT ST、逐日 universe、历史涨跌停、严格 PIT 因子门、退市终止估值及三候选账户路径瀑布均已完成。异常换手两版进入 Phase 3；月频反转触发停止规则并暂停。
- **Purpose**: 修复幸存者偏差和历史风险警示状态偏差。
- **Entry condition**: Phase 1 功能验收通过，Phase 1 新增文件 Ruff 检查全绿；DaoShare 数据源只读访问可用。
- **Phase rules**:
  - 原始下载、标准化候选和正式发布文件分层保存，正式文件不可原地覆盖。
  - 退市股票与当前股票使用同一价格复权、因子和成交语义。
  - 缺失 ST 状态默认为 unknown，不默认为非 ST。
  - 股票池过滤顺序固定并写入 manifest：上市日 → 退市日 → ST → 上市满 6 个月 → PIT 股本/因子有效性。
- **Todos**:
  - [x] 拉取并标准化退市股票维表。
    - **Surface**: `delisted_instruments.parquet`。
    - **Proof**: 源返回的每只退市股票均有 loaded/no-data/error 状态；键唯一；日期关系合法。
    - **Depends on**: Phase 0 manifest。
  - [x] 拉取退市股票日 K 和复权因子。
    - **Surface**: `delisted_daily/`。
    - **Proof**: `(symbol,date)` 唯一；不超出上市/退市边界；OHLC、成交量、复权抽样核对通过。
    - **Depends on**: 退市维表。
  - [x] 获取直接 ST 历史；若覆盖不足，再用 `namechange` 重建并标记来源等级。
    - **Surface**: `st_history.parquet`。
    - **Proof**: 状态变更键唯一、相邻重复压缩、戴帽/摘帽样本人工核对、unknown 明细可读。
    - **Depends on**: none。
  - [x] 新增研究逐日 universe mask。
    - **Surface**: 建议 `backend/app/backtest/research_universe.py`，除非现有研究模块已有更小落点。
    - **Proof**: 合成测试覆盖上市前、退市后、历史 ST、摘帽、上市不足 6 个月和 unknown 状态。
    - **Depends on**: 退市/ST 正式文件。
  - [x] 将 PIT ST 状态接入历史涨跌停和目标权重形成。
    - **Surface**: 研究矩阵字段/研究运行脚本。
    - **Proof**: 历史 ST 日使用 5% 规则，非 ST 日使用对应板块规则；当前名称不再决定历史规则。
    - **Depends on**: universe mask。
  - [x] 定义退市持仓终止估值场景。
    - **Surface**: 研究回测器/运行脚本。
    - **Proof**: 正常、最后价折价 30%、剩余价值 0 三场景均可运行，退市后无永久最后价持仓。
    - **Depends on**: 退市日 K。
- **Current evidence（2026-09-28）**:
  - `data/research/a_share_universe/20260928T-phase2-baostock-v1/`：研究区间退市标的 212，只 210 loaded、2 no-data、0 error，标的覆盖 99.06%，152,026 条可交易日 K。
  - `data/research/a_share_universe/20260928T-phase2-st-history-v1/`：SH/SZ 5,434 只全部 loaded、0 error，6,495 个压缩状态变化点；支持 `--resume`，本次复用 2,320 只并补抓 3,114 只。
  - `data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/`：210 只、152,026 行全部成功；44 只、23,146 行发生实际前复权，因子范围 0.55563–1.0；BaoStock volume 已由股归一为内部手。`adjusted-v1` 因 volume 单位错误已标 superseded。
  - `data/research/a_share_universe/20260928T-phase2-universe-audit-v2/`：1,466 日、5,779 只资产、7,398,095 条观察；SH/SZ ST unknown 为 0，347 只 BJ fail-closed；异常换手 PIT 股本门仅在 2021-06-10/11 各额外排除 1 只。
  - `data/research/a_share_strategy_validation/20260928T-phase2-waterfall-v2/`：15 条账户路径全部有限；`waterfall-v1` 因规模中性市值口径使用复权 close 已标 superseded，v2 固定为 `log(PIT float_shares × raw_close)`。
  - `abnormal_turnover_monthly_raw`：strict +201.26% → PIT ST +130.73% → 退市 100% +129.85% → 70% +127.48% → 0% +120.02%；最保守场景超额 +117.56%，IR 1.10。
  - `abnormal_turnover_monthly_size_neutral`：strict +174.52% → PIT ST +111.58% → 退市 100% +110.52% → 70% +107.79% → 0% +101.67%；最保守场景超额 +99.42%，IR 为正。
  - `reversal_monthly`：strict +131.67% → PIT ST +56.25% → 退市 100% +9.32% → 70% -64.23% → 0% -98.40%；74 个退市持仓事件，方向翻转，触发停止规则。
- **Exit proof**:
  - 所有源返回退市标的 100% 被记账为 loaded/no-data/error；
  - 研究区间内应有行情的退市标的，至少 95% 有可用历史，否则报告继续标记“部分幸存者偏差修复”；
  - 所有实际入选/持仓股票日的 ST 状态已知；
  - 新旧 universe 每日数量漏斗和收益差异可解释。
- **Stop condition**:
  - 复权口径无法与当前股票矩阵对齐；
  - 大量退市股票缺少最后交易阶段数据；
  - ST 生效日期无法确定且 unknown 会显著改变入选样本。

### Phase 3: 日线容量、冲击和排队代理

- **Status**: Complete（2026-09-28）。仅对异常换手 raw/size-neutral 执行；月频反转已暂停。正式 v2 完成 296 条路径，参与率无越界、eta 冲击单调、资金增大时未成交率不下降。
- **Purpose**: 把“能按开盘价无限成交”替换为可审计的成交上限和成本场景。
- **Entry condition**: Phase 2 的最终 universe 稳定。
- **Phase rules**:
  - ADV20 和波动率只使用 `t-1` 及更早数据。
  - 先实现零参数兼容测试，再增加部分成交。
  - 不保留与最新目标冲突的永久订单簿；下一交易日按最新目标重新计算。
  - 日线涨跌停成交只能叫 `queue_proxy`，不得叫精确队列模型。
- **Todos**:
  - [x] 增加 ADV20 参与率上限和部分成交。
    - **Surface**: `research_target_weights.py` 及测试。
    - **Proof**: 单日成交金额不超过参与率 × ADV20；未成交和目标偏离进入账本。
    - **Depends on**: Phase 2。
  - [x] 增加平方根冲击场景。
    - **Surface**: 研究回测器配置/运行脚本。
    - **Proof**: 冲击随订单规模和参与率单调不减；零 eta 精确复现旧结果。
    - **Depends on**: 容量限制。
  - [x] 增加资金规模场景。
    - **Surface**: 研究运行脚本。
    - **Proof**: 100 万、1000 万、5000 万、1 亿四档输出净收益、未成交率和清仓天数。
    - **Depends on**: 冲击模型。
  - [x] 增加乐观/基准/保守涨跌停排队代理。
    - **Surface**: 研究回测器。
    - **Proof**: 三场景定义固定；保守成交量不高于基准，基准不高于乐观。
    - **Depends on**: 容量限制。
  - [x] 输出容量曲线和流动性风险统计。
    - **Surface**: summary/report。
    - **Proof**: 最大/95% ADV 占比、容量阻塞金额、未成交率、平均现金、tracking error、最大清仓天数齐全。
    - **Depends on**: 所有执行模型。
- **Current evidence（2026-09-28）**:
  - `data/research/a_share_strategy_validation/20260928T-phase3-capacity-v2/`：2 策略 × 4 资金规模 ×（1 桥接 + 4 ADV 上限 × 3 eta × 3 queue proxy）= 296 条账户路径；summary 无重复，357,568 条 daily 全部有限。
  - 1000 万最保守组合（1% ADV、eta=0.50、conservative queue）：raw 总收益 +87.94%、超额 +85.84%、IR 0.89；size-neutral +71.63%、超额 +69.71%、IR 0.79。
  - 1000 万全网格最大未成交率：raw 40.88%、size-neutral 37.80%；最大清仓天数分别 17 和 11 天。高未成交说明容量风险真实存在，但未触发收益方向停止条件。
  - 所有实际成交参与率不超过配置上限；eta 提高时资产路径总冲击成本单调不减；资金从 100 万增至 1 亿时同场景未成交率不下降。
  - queue proxy 固定定义：optimistic 允许日线锁板排队成交；base 仅拒绝一字板；conservative 拒绝任何收盘仍锁板的逆向订单。它是确定性日线边界，不是 Level-2 队列模型。
- **Exit proof**:
  - 所有无前视测试通过；
  - 资金规模增加时成本或未成交比例不下降，若因组合路径变化出现例外必须解释并另报资产级单调性；
  - 零容量约束、零冲击、基准 queue proxy 能桥接当前结果；
  - 报告明确 `queue_model=proxy`。
- **Stop condition**:
  - 使用当日完整成交额决定当日开盘成交；
  - 订单超过容量仍全额成交；
  - 冲击模型出现负成本或非有限值。

### Phase 4: 统一重跑与研究发布门

- **Status**: Complete（2026-09-28）。完整 universe 的 base/20/40 bps、三子时期、相邻参数、年度主动收益、Phase 1–3 单文件瀑布和最终 verdict 均已完成。
- **Purpose**: 用同一数据快照和口径重新判定三条候选策略。
- **Entry condition**: Phase 1–3 全部通过。
- **Phase rules**:
  - 所有场景重新模拟账户路径，不事后机械扣减。
  - 参数和数据版本在运行前冻结。
  - 旧口径结果保留，不覆盖；新 run 使用新 run_id。
- **Todos**:
  - [x] 重跑三条候选策略的 base、20/40 bps、容量、资金规模和终止估值场景。
    - **Surface**: `data/research/a_share_strategy_validation/<run_id>/`。
    - **Proof**: manifest、summary、daily、yearly、benchmark、universe funnel、capacity 文件齐全。
    - **Depends on**: Phase 1–3。
  - [x] 生成旧口径 → 严格 PIT → 加退市/ST → 加容量/冲击瀑布表。
    - **Surface**: `report.md`。
    - **Proof**: 每一步只改变一组假设，收益差异可复算。
    - **Depends on**: 全量重跑。
  - [x] 按最终门槛给出 continue/pause/reject。
    - **Surface**: report 和 planning findings。
    - **Proof**: 每条判断引用具体数据行和停止规则。
    - **Depends on**: 瀑布表。
- **Current evidence（2026-09-28）**:
  - 正式最终产物：`data/research/a_share_strategy_validation/20260928T-phase4-final-v3/`；`summary=33`、`verdicts=3`、`bias_waterfall=14`、`yearly_active=19`，账户净值和收益全部有限，键无重复；另内含 reversal/turnover universe funnel、1000 万 capacity summary 和输入/输出 SHA-256。
  - `abnormal_turnover_monthly_raw`：base +127.48%、20 bps +108.71%、40 bps +78.19%；三个子时期超额均为正，相邻参数均为正，年度正超额不由单年垄断，最终为 `bias_reduced_candidate`。
  - `abnormal_turnover_monthly_size_neutral`：base +107.79%、20 bps +90.00%、40 bps +61.58%；三个子时期超额均为正，相邻参数均为正，最终为 `bias_reduced_candidate`。
  - `reversal_monthly`：base -64.23%、20 bps -70.45%、40 bps -78.58%；三个子时期超额全部为负、相邻参数全部为负，最终 `pause`。
  - 通过仍不解除 `research_only`；异常换手只允许进入人工评审或 paper-only 观察。2026 年两版异常换手主动收益均为负，须继续样本外监控。
- **Exit proof**: 最终产物审计通过，三条策略均有明确 verdict 和失效条件。
- **Stop condition**: 任一场景使用不同数据快照、不同策略定义或无法桥接的统计口径。

### Phase 5: 历史分钟/Level-2 执行升级（数据门控）

- **Purpose**: 在有真实历史微观结构数据后提高成交模型置信度。
- **Entry condition**:
  - 至少有完整历史 1 分钟 K，覆盖候选策略实际调仓日和股票；
  - 若声称精确排队，必须另有 Level-2 委托簿/逐笔委托/逐笔成交/集合竞价数据。
- **Phase rules**:
  - 只有分钟 K 时只实现分钟参与率和 VWAP 回放，不声称精确队列。
  - 只有 Level-2 数据才能实现队列位置和封单前后成交。
  - 数据不足时本阶段保持 blocked，不用随机概率替代事实。
- **Todos**:
  - [ ] 分钟数据到位后实现开盘后 5/15/30/60 分钟 VWAP 与分钟参与率场景。
  - [ ] Level-2 到位后单独设计队列模型和校准测试。
- **Exit proof**: minute/Level-2 覆盖报告和与日线代理的差异报告通过。
- **Stop condition**: 覆盖不足却试图外推全历史，或把分钟成交量误当盘口队列。

## Acceptance Standards

### A. 数据质量验收

| Domain | Required acceptance | Failure behavior |
|---|---|---|
| 基准指数 | 主基准覆盖所有策略正式交易日；键唯一；首日有前置收盘 | 缺主基准日则该 run fail-closed |
| PIT 股本 | 严格研究实际入选股票日 `current_fallback=0` | 缺失股票退出异常换手排名并计数 |
| 退市维表 | 源返回标的 100% 有 loaded/no-data/error 状态；键唯一 | 未记账标的阻塞 Phase 2 |
| 退市日 K | 日期边界合法、OHLC 有效、复权可解释；应有数据覆盖率 ≥95% | 低于阈值继续标记部分修复，不得宣称无偏 |
| ST 历史 | 实际入选/持仓股票日状态已知；变更键唯一 | unknown 退出 strict universe |
| 容量输入 | ADV20/vol20 仅使用 `t-1` 以前；非正 ADV 视为不可成交 | 不使用当前日 amount 或填零 |
| 分钟/Level-2 | 覆盖率单独报告 | 无数据保持 blocked |

### B. 行为验收

| Behavior | Required proof |
|---|---|
| 基准对齐 | 合成序列验证 active return、relative NAV、TE、IR |
| strict PIT | 默认模式兼容旧行为；strict 模式无当前股本回退 |
| universe | 上市前/退市后/ST/摘帽/次新边界测试 |
| 退市持仓 | 退市后不永久按最后价估值；三种终止估值场景可运行 |
| 容量 | 单日 filled notional 不超过参与率 × ADV20 |
| 部分成交 | 未成交进入账本并影响实际权重/现金 |
| 冲击 | 订单规模增加时资产级冲击不下降；eta=0 复现旧结果 |
| queue proxy | 保守成交量 ≤ 基准 ≤ 乐观；结果明确标 proxy |
| 无未来函数 | 修改 T 日 amount/close 不得影响 T 日开盘容量和冲击输入 |

### C. 统计口径验收

定义固定如下：

```text
portfolio_nav[t] = equity[t] / initial_capital
benchmark_return[t] = benchmark_close[t] / benchmark_close[t-1] - 1
benchmark_nav[t] = cumulative_product(1 + benchmark_return[t])
active_return[t] = portfolio_return[t] - benchmark_return[t]
relative_nav[t] = portfolio_nav[t] / benchmark_nav[t]
excess_total_return = relative_nav[last] - 1
tracking_error = std(active_return, ddof=0) * sqrt(252)
information_ratio = mean(active_return) / std(active_return, ddof=0) * sqrt(252)
```

当主动收益标准差为 0 时，IR 为 null，不填 0 或无穷大。

报告至少包含：

- 组合绝对总收益/年化/波动/Sharpe/最大回撤；
- 基准总收益/年化；
- 相对净值、超额总收益、年化超额；
- tracking error、information ratio、relative drawdown；
- 每年组合/基准/超额收益；
- universe 漏斗、PIT 缺失、ST unknown、退市覆盖；
- ADV 占比、未成交、冲击成本、清仓天数和资金规模曲线。

### D. 最终研究通过门槛

“internally stable”只能升级为“bias-reduced candidate”，必须同时满足：

1. 加入退市和 PIT ST 后，base 总收益方向不翻转；
2. 相对中证全指的完整样本年化超额为正；
3. 信息比率为正，且不完全由单一年份贡献；
4. 20 bps 和 40 bps 场景方向不翻转；
5. 至少两个子时期超额收益同方向；
6. 相邻参数方向不翻转；
7. 1000 万资金规模下，容量和冲击后收益仍为正；
8. 退市极端终止估值场景不改变总体方向；
9. 所有数据降级和未知状态都已计数并披露。

注意：通过上述门槛仍不自动解除 `research_only`；它只允许进入下一轮人工评审或 paper-only 观察。

## Stop Rules

任一条件触发即暂停对应策略或阶段：

- 加入退市/ST 后收益方向翻转；
- 主基准超额收益消失或 IR ≤ 0；
- 严格 PIT 后异常换手结果显著依赖被排除的 0.1832% 缺失行；
- 收益集中于单一年份或 2021 年后持续失效；
- 1000 万规模下容量/冲击吞噬收益；
- 退市极端估值场景使结论翻转；
- 退市日 K 覆盖低于 95%；
- ST unknown 覆盖足以改变 Top-N 选择；
- 需要 Level-2 才能支持的结论，却只有日线或分钟 K。

## Cut List

第一轮明确不做：

- 不新增通用 provider dataset 框架；先用研究隔离数据证明价值，再决定是否产品化。
- 不把 340 只退市股票直接合并进生产 instruments。
- 不修改生产撮合器表达容量和冲击。
- 不做动态参数优化或根据结果挑选 eta。
- 不引入新的运行时第三方统计依赖。
- 不实现永久挂单簿；每日按最新目标重算。
- 不使用随机成交概率掩盖缺少队列数据。

## Dry-Run Findings

- 基准阶段没有代码结构阻塞：现有指数目录、同步服务和 `get_index_daily()` 已足够，主要缺历史数据。
- PIT 股本不是从零建设：当前覆盖率 99.8168%，最小正确改动是 strict 模式和来源审计。
- 退市/ST 必须先形成正式数据文件，再接 universe；直接在策略里请求外部 API 会破坏可复现性。
- 退市样本加入后矩阵资产轴会变化，所有目标权重必须基于新矩阵重新生成，不能把旧权重补零后复用。
- 容量模型必须在最终 universe 后执行，因为退市/ST 股票通常也是流动性最差、最受容量约束的样本。
- 本地分钟目录为空，因此 Phase 5 当前为 blocked；这不阻塞 Phase 1–4。
- 最小模块边界是：研究数据文件 + 一个逐日 universe helper + 现有研究回测器扩展 + 现有运行脚本编排，不需要大型通用框架。

## Final Validation

建议最终验证命令骨架：

```bash
uv run --project backend pytest \
  backend/tests/test_financial_shares.py \
  backend/tests/backtest/test_research_universe.py \
  backend/tests/backtest/test_research_target_weights.py \
  backend/tests/test_research_a_share_factors.py \
  backend/tests/test_price_limits.py -q

uv run --project backend ruff check \
  backend/app/share_capital.py \
  backend/app/backtest/research_universe.py \
  backend/app/backtest/research_target_weights.py \
  backend/scripts/run_a_share_strategy_validation.py

python -m py_compile \
  backend/app/share_capital.py \
  backend/app/backtest/research_universe.py \
  backend/app/backtest/research_target_weights.py \
  backend/scripts/run_a_share_strategy_validation.py

git diff --check
```

真实数据最终审计必须至少断言：

```text
benchmark_missing_strategy_days == 0
selected_current_share_fallback_count == 0
summary_duplicate_rows == 0
all_equity_and_returns_finite == true
queue_model in {proxy, minute, level2}
manifest_contains_all_data_hashes == true
```

## Rollback and Isolation

- 所有新数据写入新的 run/candidate 目录，验证后再发布正式文件；不原地覆盖。
- 生产股本默认回退行为保持不变；strict 仅由研究调用显式开启。
- 退市/ST 数据保留在 `data/research/a_share_universe/`，删除该目录即可回退数据接线。
- 容量/冲击配置默认关闭时必须精确复现当前研究回测结果。
- 每个完整历史 run 使用独立 `run_id`，旧结果永久保留用于桥接。

## First Execution Step

先执行 Phase 0 和 Phase 1：冻结当前数据指纹，回填 `000985.SH`、`000300.SH`、`000905.SH`、`000852.SH` 的完整历史；随后以 TDD 给 `apply_historical_float_shares()` 增加研究 strict 模式，并重跑三条候选策略的基准超额和严格 PIT 对照。该步骤通过前，不下载或接入退市/ST 数据。
