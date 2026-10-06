# 第三方 TA 指标适配层（已实现）

> **状态声明**：本层**已实现**，落点为 `backend/app/factors/ta_factors.py`，并接入历史帧与回测
> 补算路径。依赖 `polars-talib` 为**可选 extra `ta`**：未安装时本层全量 no-op，主流程与既有
> 因子不受影响（对齐 CONTRIBUTING §2.1 的 `vectorbt` 模式）。本文描述现状，不含未实现的目标契约。

## 1. 目的

用 `polars-talib`（MIT，二进制 wheel 内置 TA-Lib 0.8.1 的 C 库）补齐内置流水线**缺失**的标准
指标，并提供一个与内置指标对照的 **parity 基线**，供重构与第三方库升级时回归。

与 `docs/factor-system-design.md`（设计提案）不同，本层不改变因子注册表契约：新指标以
`kind="base"` 条目注册，`id` 即帧列名，空 `dependencies`（列自身即值）——与
`app/factors/ext_factors.py` 完全同构。

## 2. 启用

```bash
cd backend
uv sync --extra ta          # 安装 polars-talib>=0.2
```

未安装时：`ta_factors.available()` 为 `False`，`ta_factor_specs()`/`ta_factor_ids()` 为空，
`attach_ta_columns` 直接返回原帧，因子库不出现 `ta_*` 条目。

## 3. 指标清单

列名以 `ta_` 前缀避免与内置列冲突；多输出指标的 struct 字段摊平为独立标量列。

| 因子 id | 说明 | 预热 | scale_free |
| --- | --- | --- | --- |
| `ta_adx_14` | ADX(14)，0~100 | 28 | ✓ |
| `ta_cci_14` | CCI(14) | 14 | ✓ |
| `ta_willr_14` | Williams %R(14)，-100~0 | 14 | ✓ |
| `ta_obv` | 经典 OBV（非归一化） | 1 | ✗ |
| `ta_stoch_k` / `ta_stoch_d` | Stoch(14,3,3) | 20 | ✓ |
| `ta_aroon_up` / `ta_aroon_down` | Aroon(14)，0~100 | 14 | ✓ |
| `ta_supertrend` | SuperTrend(10,3)，价格尺度 | 12 | ✗ |
| `ta_hma_20` | HMA(20)，价格尺度 | 20 | ✗ |
| `ta_ha_open/high/low/close` | Heikin-Ashi，价格尺度 | 1 | ✗ |

`unit` 统一为 `"none"`（未逐因子核对，遵守 `registry.py` 的诚实声明约定）。

## 4. 接入路径

- **历史帧**：`indicators/pipeline.py` 的 `compute_signals`（在 ext 注入之后），只计算被
  `needed` 或**自定义信号表达式引用**的 TA 因子。
- **回测补算**：`backtest/factor.py` 的 `_compute_missing_factors`，与 `ext_factors` 同一拦截位置。
- **注册可见性**：`factors/registry.py` 的 `all_factors()` 惰性调用 `ta_factors.ensure_synced()`，
  使因子库列表、`custom_signals.allowed_fields()`、AI 提示词自动可见。
- **主图副图渲染**：`GET /api/kline/daily` 新增可选 `ta_columns`（逗号分隔因子 id）。命中时
  `repository.get_daily` 在含 warmup 的整段上补算再裁剪，前端 `StockDailyKChart` 按已开启的
  TA 副图拼接该参数（`EChartsCandlestick` 的 `TA_SUB_CHARTS`：ADX/CCI/W%R/STOCH/AROON/OBV）。
  未开启时不带参数，请求与既有逐位一致。
- **screener SQL**：`services/screener.py` 的 `run()` 用 `ta_factors.referenced_ta_columns()` 从
  条件串识别 `ta_*` 引用；命中时跳过单日缓存、改走带 warmup 的 `_compute_enriched_full` 即时计算
  并补算这些列，使 DuckDB WHERE 里的 `ta_*` 条件真正生效（否则 Binder Error 会被吞成静默空结果）。

**刻意不接入当日单日增量帧**（`compute_enriched_today`）：该路径每个 symbol 仅一根 K 线，无预热
窗口，TA 指标无从计算；接入只会产出全 null 列并增加每秒级开销。若在当日路径引用 `ta_*`，按
既有的「缺列告警跳过」优雅降级处理。

## 5. `needed` 语义

`attach_ta_columns(df, *, needed)` 为**显式 opt-in**：

- `needed is None` 或空集：不计算（保护热路径）。
- 计算 `needed ∩ ta_factor_ids()` 中缺失的列；列已存在时跳过（幂等）。
- 计算前按 `(symbol, date)` 排序（插件 `.over("symbol")` 要求组内有序）；预热期 NaN 统一归一为
  `null`；单个指标源失败只告警跳过该源（fail-soft）。

## 6. parity 对照

`parity_report(df, names)` 比较帧上内置指标列与第三方实现，返回
`{name: {covered, max_abs_diff, mean_abs_diff, expected_mismatch, note?}}`，只统计双方均为有限值的样本。
定位为测试/升级回归工具，**不进热路径**。

实测结论（内置 `compute_indicators` vs `polars-talib`，多标的样本）：

| 指标 | 结果 |
| --- | --- |
| `ma5/10/20/60` | **精确匹配**（max_abs_diff < 1e-9） |
| `ema5/10/20/60`、`rsi_*`、`macd_*`、`boll_*`、`atr_14` | **存在口径差异**（EMA 种子、RSI/ATR 平滑、BOLL 自由度不同），非可直接替换 |
| `kdj_k/d/j` | **预期不匹配**（内置为通达信 RSV/EWM α=1/3 口径） |

因此：第三方列**只作新增独立列 + 对照，绝不回灌内置因子列** —— 内置 pipeline 仍是既定因子的
唯一权威，避免「双重真相」。

## 7. 口径与边界（金融契约）

- 输入为 enriched 的**前复权** `open/high/low/close/volume`，与内置指标同一价格口径。
- 预热期 NaN → null（评分/IC 口径不容忍 NaN）。
- 不回灌内置列（见 §6）；`ta_*` 列不进入 `ENRICHED_COLUMNS`/`ENRICHED_STORAGE_COLS`，不改
  Parquet schema，无数据迁移。
- 图表副图与 screener SQL 已接入（见 §4）。已知限制：① 本轮只在 **stock** 的 `get_daily` 接
  attach，指数/ETF 日 K 路径未改，这些标的上的 TA 副图为空线；② 当日实时蜡烛由增量路径覆盖
  （不含 TA），今日值可能为 null，不影响历史段；③ 前端 TA 开关按 `/api/factors` 是否含 `ta_`
  条目做能力门控，后端未装 extra 时不显示；④ DuckDB 无法运行 `polars-talib`，screener 仍是
  Polars 预计算后注册视图，不是纯 SQL 现算。
- 前端图表仅渲染副图类振荡指标（ADX/CCI/W%R/STOCH/AROON/OBV）；价格尺度的
  Supertrend/HMA/Heikin-Ashi 未做主图叠加。

## 8. 测试

`backend/tests/test_factors_ta.py`：

- 缺失降级（monkeypatch `available()→False`）：specs/ids 为空、attach 原样返回、`all_factors()` 无 `ta_*`。
- 数值：各列物理区间、预热期为 null、NaN 已归一。
- 集成：`all_factors()`、`allowed_fields()`、`compute_signals`、`_compute_missing_factors`。
- parity：MA 精确匹配、KDJ 标记预期不匹配、EMA/RSI/BOLL 差异被记录。

接入路径测试：

- `tests/test_kline_daily_ta_columns.py`：`get_daily` 带 `ta_columns` 时含 warmup 补算 + 裁剪，
  不带时不产出 ta 列；`/api/kline/daily` 解析并透传已注册 id。
- `tests/test_screener_ta_conditions.py`：`referenced_ta_columns` 识别纯度；`run` 里 `ta_*`
  条件真正命中（非静默空）；未装 extra 时优雅降级不抛异常。

内置目录计数黄金断言（`test_factor_registry.py` / `test_factor_store.py`）通过
`tests/conftest.py` 的 `isolate_ta_factors` 夹具与 extra 安装状态解耦。
