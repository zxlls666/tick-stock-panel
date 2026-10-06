# ADR-002: 第三方 TA 指标接入主图副图与 screener SQL

- **状态：** 已采纳
- **日期：** 2026-10 (v0.2.3)
- **决策者：** Serge

## 上下文

- 背景：`backend/app/factors/ta_factors.py`（可选 extra `ta`）已把 polars-talib 的
  ADX/CCI/Williams %R/OBV/Stoch/Aroon 等注册为 `ta_*` base 因子，并接入历史帧、回测补算与
  注册可见性。当时的计划（`ta-indicator-adapter.md`）明确把「前端图表渲染」与「screener 的
  DuckDB 现算」列为延后项。
- 约束：
  - `ta_*` 列不落 parquet（运行时计算），不改 enriched schema，无数据迁移。
  - `/api/kline/daily` 与 screener 取数是热路径（个股弹窗每 tick 调用一次），未开启能力时行为
    必须逐位不变。
  - TA 指标需要预热窗口；单日截面（最新日缓存）与无 warmup 的只读列快路径无法计算。
  - `ta_*` 仅在后端装了可选 extra 时存在。
- 现有约定：同端点已有 `ext_columns` 参数模式；screener 是「Polars 先算好 enriched 帧 →
  DuckDB 只做 WHERE 过滤」，DuckDB 无法运行 polars-talib。
- 可选方案：
  1. 图表数据：给 `/api/kline/daily` 加可选 `ta_columns`（复刻 `ext_columns`）；或新增通用
     `/api/factors/{id}/series` 端点，前端按日期合并。
  2. screener：从 SQL 条件里自动识别 `ta_*`；或新增显式 `ta_columns` 请求字段。
  3. 渲染位置：主图行情副图（`StockDailyKChart`/`EChartsCandlestick`）；或分析页曲线
     （`AnalysisKChart`）。

## 决策

- **图表数据**：`GET /api/kline/daily` 增加可选 `ta_columns`（逗号分隔因子 id）。命中时
  `repository.get_daily` 跳过只读列快路径、把历史缓存切片向左放宽 90 天预热、在裁剪前用
  `attach_ta_columns` 补算，再收敛到请求区间；未请求时零变化。仅在 **stock** 日 K 路径接入。
- **screener**：`ta_factors.referenced_ta_columns(conditions)` 用词边界正则从 SQL 条件识别
  `ta_*` 引用；`ScreenerService.run` 命中时跳过单日缓存、改走带 warmup 的 `_compute_enriched_full`
  即时计算并补算这些列。未引用/未装 extra 时保持原缓存快路径。
- **渲染位置**：主图行情 K 线副图，振荡类精选 6 类（ADX/CCI/W%R/STOCH/AROON/OBV）；
  `EChartsCandlestick` 导出 `TA_SUB_CHARTS` 并新增 `extraSubCharts` 属性与内置子图合并；
  `StockDailyKChart` 按已开启副图拼接 `ta_columns`，并用 `/api/factors` 是否含 `ta_` 条目做
  能力门控。
- 不把 `ta_*` 接入当日单日增量帧（无预热窗口），不改因子定义/注册契约，不改 parquet schema。

## 理由

- 复用同端点既有的 `ext_columns` 参数模式，前端单次请求、无需日期对齐第二套逻辑。
- 全部 opt-in：未开启时不带参数、不计算、cache key 不变，对热路径零影响。
- screener 自动识别避免让调用方重复声明一遍 SSOT（条件里已写明列名），并修正了原先
  「引用不存在列 → Binder Error 被吞 → 静默空结果」的隐式错误。
- 复用既有 `attach_ta_columns`（与 ext 同原语）与带 warmup 的即时计算路径，不引入新计算栈。

## 后果

- 正面：TA 指标在图表可交互查看、在 screener 条件中真正生效；口径与既有 TA 层一致。
- 负面 / 边界：
  - 本轮只在 stock 的 `get_daily` 接 attach，指数/ETF 日 K 路径未改，这些标的上的 TA 副图为空线。
  - 当日实时蜡烛由不含 TA 的增量路径覆盖，今日值可能为 null（不影响历史段）。
  - 带 `ta_*` 条件的 screener 跳过 repo 缓存、改走即时计算（数十毫秒级），未带时保持原快路径。
  - 前端 TA 开关按 `/api/factors` 能力门控；后端未装 extra 时不显示。
- 需要后续验证：新增/调整 TA 因子时，`TA_SUB_CHARTS` 的 `column` 映射与 `/api/factors` 的
  `ta_` 前缀需保持同步。

## 替代方案否决理由

- **新增通用 `/api/factors/{id}/series` 端点**：需要第二条请求 + 前端日期对齐，且对本轮 TA 需求
  并无额外收益；改为复用同端点的 `ext_columns` 模式。
- **把 `ta_*` 落 parquet**：改 schema、需迁移，且违背「运行时计算、可选依赖」的既有设计。
- **在当日增量帧也计算 TA**：每 symbol 仅一根 K 线、无预热，只会产出全 null 列并增加每秒级开销。
- **screener 显式 `ta_columns` 请求字段**：与条件里的列名重复声明、易不一致；选择从条件自动识别。
- **纯 SQL 现算**：DuckDB 无法运行 polars-talib，仍是 Polars 预计算后注册视图。
- **分析页 `AnalysisKChart` 曲线**：现有结构是价位曲线，不是通用副图；振荡指标更适合主图副图。
