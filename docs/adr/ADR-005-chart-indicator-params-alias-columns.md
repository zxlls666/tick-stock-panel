# ADR-005: 图表指标参数化的别名列契约

- **状态：** 已采纳
- **日期：** 2026-10 (v0.2.3)
- **决策者：** Serge

## 上下文

- 背景：日 K 图需要像 TradingView 一样「可切换 + 可改参数」的指标能力。现状（见
  `.planning/2026-10-07-a/indicator-tv-parity-plan.md`）只支持在一组固定的内置指标间切换
  （`EChartsCandlestick.SUB_CHARTS`），周期全部硬编码（MA 5/10/20/60、MACD 12/26/9 …），
  唯一可调数字是量比窗口。
- 约束：
  - `pipeline.ENRICHED_COLUMNS` 的**规范列**（`ma20`、`macd_dif`、`boll_upper` …）是单一权威，
    被 screener、自定义信号 `ALLOWED_FIELDS`、回测 `feature_plan` 和 AI 提示词共同引用。
    **在规范列名下改周期会静默改变这些消费方的语义**，属错误的金融口径（CONTRIBUTING §3/§5）。
  - 存储是窄表（`ENRICHED_STORAGE_COLS`，15 列），指标在读取时现算；不改 parquet schema、不做迁移。
  - `/api/kline/daily` 是热路径（个股弹窗每个行情 tick 调用一次），未请求新能力时行为必须逐位不变。
  - 已有 `ext_columns` / `ta_columns` 的 **opt-in 附加列** 模式（ADR-002），可复用。
- 可选方案：
  1. 复用规范列名 + 传周期参数（`ma20` 变成用户周期）。
  2. 新增通用 `/api/factors/{id}/series` 端点，前端按日期二次合并。
  3. 在 `/api/kline/daily` 增 `indicator_specs`，后端按 spec 产**别名列**（本决策）。
  4. 前端在 ECharts 里本地计算自定义周期指标。

## 决策

- 新增纯函数模块 `backend/app/indicators/custom.py`：
  `parse_indicator_specs` / `compute_custom_indicators` / `max_period` / `IndicatorSpec`。
- **请求编码**（GET query，复用 `ext_columns` 风格）：逗号分隔条目，冒号分隔 kind 与参数，
  如 `ma:5:13,macd:8:17:9,rsi:7`。支持 `ma/ema/rsi/macd/boll/kdj`。
- **别名列命名**：`<kind>_<params_joined_by_underscore>_c`，多输出指标再加输出段
  （`macd_8_17_9_dif_c` / `_dea_c` / `_hist_c`；`boll_20_2_upper_c` / `_lower_c`；
  `kdj_9_k_c` / `_d_c` / `_j_c`）。别名列**绝不写入** `ENRICHED_COLUMNS` / `ENRICHED_STORAGE_COLS`，
  不改 parquet schema，无数据迁移。
- **口径**：与 `pipeline.compute_indicators` 同一公式（同一 `ewm_mean(alpha=...)/rolling_*`）。
  默认参数产出的别名列与规范列**数值逐位一致**，由测试锁定。
- **接入**：
  - `GET /api/kline/daily` 增可选参数 `indicator_specs`；`repository.get_daily` 命中时跳过只读列
    快路径，按 `max_period` **自适应加宽预热窗口**，缓存覆盖不到预热起点时回退扫描路径，
    补算别名列后再裁剪到请求区间；`get_daily_asset` 透传。**仅 stock 日 K 路径**。
  - fail-closed：非法 kind / 越界参数 / 非整数参数 → `ValueError` → HTTP **400**，不静默截断。
  - opt-in：未带 `indicator_specs` 时，请求、cache key、返回列与既有**逐位一致**。

## 理由

- 复用同端点既有的 `ext_columns` / `ta_columns` 模式：前端单次请求、无需日期对齐第二套逻辑，
  与 ADR-002 的取舍一致。
- 别名列保证规范列 SSOT 不被改写：screener / 回测 / 自定义信号 / AI 提示词的语义保持不变，
  避免「双重真相」。
- 自适应预热确保长周期别名列（如 MA250）在请求窗口首段不产生 NaN 误值；缓存不足时回退扫描
  是可验证的确定性行为，而非静默降级。
- 计算放后端（`app/indicators/`）：API 保持薄层，口径与既有指标同源，符合 CONTRIBUTING §2.3。

## 后果

- 正面：日 K 图可改指标周期且不改写任何规范列；口径单一、可测（默认参数逐位一致）；
  热路径默认零影响（未带参数不计算、不新增列）。
- 负面 / 边界：
  - 别名列仅在带 `indicator_specs` 时计算并返回；前端需按同一命名规则自行拼接列名——
    这是契约的一部分，前后端命名必须保持同步。
  - 仅 **stock** 日 K 路径接入；指数 / ETF 的 `indicator_specs` 被忽略（与 `ta_columns` 现状一致）。
  - 周期参数为**整数**（含 BOLL 的 `std`），Phase 1 不支持小数；周期上限 500。
  - 当日实时蜡烛由不含别名列的增量路径覆盖，**今日别名列可能为 null**（不影响历史段）。
  - 自定义指标不进入 screener SQL / 自定义信号条件（那是另一条口径线，需单独评审）。
  - 需要后续验证：前端 `SUB_CHARTS` / 渲染与别名命名规则同步；前端设置对话框落地（Phase 1 剩余）。

## 替代方案否决理由

- **复用规范列名 + 传周期**：会静默改写 screener / 回测 / 信号对 `ma20` 等列的语义，属错误的
  金融口径，且形成双重真相；否决。
- **新增 `/api/factors/{id}/series` 端点**：需第二条请求 + 前端日期对齐，对本轮需求无额外收益
  （与 ADR-002 对同一备选的否决理由一致）。
- **前端本地计算**：会在图表层产生第二套指标口径，与「后端 `compute_indicators` 是唯一权威」
  冲突；否决。
- **把自定义周期指标落 parquet**：改 schema、需迁移，且违背「窄表 + 运行时计算 + 可选」的既有设计。

## 相关记录

- 计划与差距矩阵：`.planning/2026-10-07-a/indicator-tv-parity-plan.md`
- 实现：`backend/app/indicators/custom.py`、`backend/app/api/kline.py`、`backend/app/tickflow/repository.py`
- 测试：`backend/tests/test_indicators_custom_params.py`、`backend/tests/test_kline_daily_indicator_specs.py`
- 同类前例：`docs/adr/ADR-002-ta-indicator-chart-and-screener.md`（同端点的 opt-in 附加列模式）
