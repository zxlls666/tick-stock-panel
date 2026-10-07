# ADR-006: 图表指标注册表与通用渲染契约

- **状态：** 已采纳
- **日期：** 2026-10 (v0.2.3)
- **决策者：** Serge

## 上下文

- 背景：Phase 2 之后，日 K 图的指标集合仍**前端硬编码** —— `EChartsCandlestick.SUB_CHARTS` /
  `TA_SUB_CHARTS` / `OVERLAY_INDICATORS` 各自写 `buildSeries` / `buildInfo` / `legend`，
  `StockDailyKChart` 还要拼 `ta_columns` 与别名列名。新增一个指标要同时改多处前端代码。
- 目标（Phase 3 验收）：**新增一个指标只需后端注册即可在前端出现**。
- 约束：
  - `app/factors/registry.py` 是**因子/评分**元数据的权威（id/label/group/公式/依赖/warmup/unit），
    被 screener、自定义信号白名单、回测物化与 AI 提示词共同引用 —— **不得为其添加图表字段或
    改变其契约**（对齐 `docs/ta-adapter.md` §1）。
  - 渲染必需的元数据因子注册表没有：面板位、参数 schema、输出（列/标签/颜色/线或柱）、Y 轴范围。
  - 别名列命名（ADR-005）目前在后端 `indicators/custom.py` 与前端 `lib/chartIndicators.ts`
    **各写一份**，有漂移风险，需收口为单一来源。
  - 可选 extra `ta` 未安装时必须整体降级（不显示、不报错）。
  - 前端渲染需数据驱动；成交量（量比标注）是唯一的特例。
- 可选方案：
  1. 扩 `FactorSpec` 加图表字段。
  2. 新模块 `app/indicators/registry.py` + `GET /api/indicators/catalog`（本决策）。
  3. 前端 TS 常量即注册表。
  4. 复用 `/api/factors` 让前端按 group 推断面板位/输出。

## 决策

- 新增 `app/indicators/registry.py`，定义**图表指标描述符**（与因子注册表解耦，只**引用**既有
  列 id / `ta_*` id，不重复定义计算口径）：

  ```text
  ChartIndicator {
    id: str                    # 稳定标识 ("ma" / "macd" / "ta_adx_14")
    label: str                 # 选择器显示名
    pane: "overlay" | "panel"  # 主图叠加 / 副图
    source: "builtin" | "ta"
    params: [ParamSpec]        # 空 = 无参；ParamSpec{ key, label, kind: scalar|list, default, min, max }
    outputs: [OutputSpec]      # OutputSpec{ key, label, color, type: line|bar, canonical_col, col_template }
    y_axis?: { min?, max? }    # 固定范围（如 RSI 0~100）
    special?: str              # 预留特例标记（如成交量）
  }
  ```

- `GET /api/indicators/catalog` 返回上述目录 + 每项 `available`（ta 未装则 `ta_*` 不可用）；
  **不改任何计算端点**，纯只读元数据。
- **别名列命名模板**（`col_template`，如 `ma{p}_c` / `macd_{fast}_{slow}_{signal}_dif_c`）
  随描述符下发，前后端**共用同一份命名**；`lib/chartIndicators.ts` 的硬编码命名退休。
- 前端改为：选择器与渲染器读 catalog；通用渲染器按 `outputs` + `pane` 画线/柱，`params` schema
  驱动参数面板；成交量走 `special` 特例。参数化请求（`indicator_specs`，ADR-005）不变 ——
  仅当参数偏离默认时按 `col_template` 拼别名列。
- 分步落地：先**并存**（新 catalog 端点 + 前端接入），验证后再**退休**旧硬编码，缩小单次改动面。

## 理由

- 图表元数据（渲染）与因子元数据（评分）关注点不同，分离避免污染既有契约。
- 描述符只引用列 id / ta id，计算仍由 `pipeline` / `indicators.custom` / `ta_factors` 各自权威负责，
  不产生第二套口径。
- `col_template` 收口命名，消除前后端两份别名规则的漂移。

## 后果

- 正面：新增通用指标 = 注册一条描述符即可在前端出现；命名单一来源；ta 能力门控与既有 `/api/factors` 一致。
- 负面 / 边界：
  - 需为现有指标一次性补齐描述符（约 14 条）。
  - 成交量（量比标注）与 Heikin-Ashi 一类需要 `special` 或价量特例；通用渲染器须预留扩展点。
  - 前端通用渲染器落地前，旧硬编码仍并存；只有切换完成后才能删除。
  - 描述符只覆盖**图表**指标；因子库/评分不属于本契约。
- 需要后续验证：前端切换后回归全绿 + 页面验收（选择器/图例/参数面板与既有行为一致）。

## 替代方案否决理由

- **扩 `FactorSpec` 加图表字段**：颜色/面板位对评分与回测无意义，且会改动被广泛引用的契约，风险大。
- **前端常量当注册表**：不满足「后端注册即出现」，仍要改前端。
- **复用 `/api/factors` 推断**：缺渲染必需字段，按 group 猜面板位/输出不可靠且脆弱。

## 相关记录

- 计划：`.planning/2026-10-07-a/indicator-tv-parity-plan.md`（Phase 3）
- 前序：`docs/adr/ADR-005-chart-indicator-params-alias-columns.md`、`docs/ta-adapter.md`
- 实现：`backend/app/indicators/registry.py`、`backend/app/api/indicators.py`
