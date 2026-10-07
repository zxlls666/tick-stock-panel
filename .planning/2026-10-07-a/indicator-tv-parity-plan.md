# 差距清单与执行计划：图表指标能力对齐 TradingView

- **状态：** 待评审（用户「再定」）
- **日期：** 2026-10-07
- **范围：** 日 K 图的指标能力（叠加/副图/参数/图例/选择器），不改因子、策略、回测口径

---

## Go / No-Go

- **Judgment**: Go（分阶段，非一次性）
- **Reason**: 系统已经有「可切换的固定指标清单」这层底座（后端现算 + 前端副图开关），
  缺的是参数化、图例勾选、指标选择器三块。三块可独立交付，且都能复用既有的 **opt-in 附加列** 模式
  （`ext_columns` / `ta_columns`），不必改 enriched schema、不碰热路径默认行为。
- **前提**: 需要先冻结一个架构决策——**自定义周期的指标是否可以复用既有规范列名**。
  按 CONTRIBUTING §3/§5 的口径要求，我的判断是**不可以**（见下「关键架构约束」），只能用别名列，
  这样 screener/自定义信号/回测引用的 `ma20`、`macd_dif` 语义不会被悄悄改掉。

---

## 目标

让日 K 图在**不破坏现有数据契约**的前提下，具备 TradingView 的核心交互能力：

1. 用户能**开关任意已注册指标**（主图叠加 + 多副图面板，而非当前一组固定按钮）；
2. 用户能**改指标参数**（均线周期、MACD/RSI/KDJ/BOLL 的 N），改完立即重绘；
3. 有**图例勾选**与**指标选择器**，而不是只能点一排硬编码按钮；
4. 新增指标（尤其可选 `ta_*`）能**自动出现在选择器里**，无需前端再写死。

---

## 目标定义

- **Type**: technical + product（UI 能力），无数据/策略口径变化
- **边界**:
  - 只动 `backend/app/indicators/`（现算）、`backend/app/api/kline.py`（可选参数）、
    `repository.get_daily` 的 opt-in 分支、`frontend/src/components/EChartsCandlestick.tsx` +
    `StockDailyKChart.tsx`（及后续图例/选择器组件）。
  - 全部新增能力 **opt-in**：未带新参数时，请求、cache key、返回列、渲染结果与现在**逐位一致**。
  - 自定义周期指标以**别名列**返回，绝不改写 `ENRICHED_COLUMNS` 里的规范列。
- **Non-goals**:
  - 不做 TradingView 全量克隆（不做画线工具、多品种叠加、社区指标脚本语言）。
  - 不接入 TradingView 数据源或其官方 MCP，不做产品级 TV 依赖（合规边界：TV ToS 禁止基于其内容构建产品/处理其内容）。
  - 不改因子注册契约、不改策略/回测/监控对规范列的依赖。
  - 不落 parquet、不改 `ENRICHED_STORAGE_COLS`、不做数据迁移。
  - 第一期不做服务端布局持久化（先 localStorage，与 `storage.stockVolumeCompare` 一致）。
  - 不把指标参数引入 screener SQL / 自定义信号条件（那是另一条口径线，需要单独评审）。
- **Deferred work**:
  - 服务端保存指标布局模板 / 多端同步。
  - 主图价格尺度的 SuperTrend / HMA / Heikin-Ashi 叠加（`ta-adapter.md` §7 已标注未做）。
  - 分析页（`AnalysisKChart`）的振荡副图（其文件头注释已标为「后续扩展点」）。
- **Verification rule**:
  - 参数化计算：固定样本数值断言（自定周期 vs 手算/对照），且**默认参数下与现行逐位一致**。
  - API：未带新参数时响应字节等价（列集与值不变）；带了才产出别名列。
  - 前端：`pnpm build` 通过，手工覆盖加载/空/错/切换/窄屏。
  - 可选依赖缺失（未装 `ta` extra）时选择器不显示 TA 项且不报错。
- **Evidence source**: 定向 `pytest`、`pnpm build`、`git diff --check`、真实页面验收截图。
- **Pass criteria**:
  - 每个阶段有独立 release gate，通过前不进入下一阶段。
  - 现有相关回归（kline / factor / screener / 涨跌停 / 成本）保持通过。
  - 热路径（个股弹窗每 tick 取数）默认请求零变化。
- **Confidence note**: 参数化计算的正确性可高置信用测试锁定；「像不像 TV」的体验是主观项，
  由用户在每阶段 gate 上判断，不由测试代替。
- **Judgment owner**: 技术完成由测试/构建判定；是否推进到下一阶段由用户决定。

---

## 现状（现状能力清单）

**后端**
- 指标目录单一权威：`backend/app/indicators/pipeline.py:111-205` 的 `ENRICHED_COLUMNS`
  （+ `:208-225` 分类索引）。覆盖 MA(5/10/20/30/60)、EMA(5-60)、MACD(12/26/9)、BOLL(20,±2σ)、
  KDJ(9,3,3)、RSI(6/14/24)、ATR(14)、量均线(5/10)/量比、60 日高低、动量(5-60)、年化波动、
  `prev_close/change_pct/amplitude`、20 个 `signal_*`（bool）。
- **周期全部硬编码**：`compute_indicators()`（`pipeline.py:365`）里全是字面量（`:397-445` 等），
  `_ema_alpha(span)`（`:228`）只做 span→α 换算。**没有任何用户可传的周期参数**。
- 存储是**窄表**（`ENRICHED_STORAGE_COLS`，15 列）；指标在读取时现算
  （`repository.get_daily`，`repository.py:1402`），带 150 日预热与进程内缓存。
- `compute_indicators(df, needed=...)` 已有**子集裁剪**机制（`_INDICATOR_DEPS` / `_resolve_needed`，
  `:317-362`），但仅内部使用（回测 feature_plan、regime、screener TA），**未通过 HTTP 暴露**。
- API：`GET /api/kline/daily`（`backend/app/api/kline.py:358`）返回**整张 enriched 行**
  （`rows = df.to_dicts()`），只支持**附加** `ext_columns` / `ta_columns`（`:365-391`）；
  无「请求子集」、无「周期覆盖」参数。

**前端**
- 日 K 引擎 `frontend/src/components/EChartsCandlestick.tsx`：ECharts（**不是** lightweight-charts；
  后者只用于分时线图，且 `CandlestickChart.tsx` 是无引用的死代码）。
- 可切换指标定义：
  - `SUB_CHARTS`（`:137-323`）：`vol` / `macd` / `rsi` / `kdj`（副图，周期写死）。
  - `OVERLAY_INDICATORS`（`:423-425`）：`boll`（主图叠加）。
  - `TA_SUB_CHARTS`（`:333-420`）：`ta_adx/ta_cci/ta_willr/ta_stoch/ta_aroon/ta_obv`，需后端装可选 extra `ta`。
- 交互在 `frontend/src/components/StockDailyKChart.tsx`：`activeIndicators` 状态（`:134`）+
  一排固定 toggle 按钮（`:196-268`）+ `taColumns` 拼接（`:142-148`）+ TA 能力门控（`:157-162`）。
- **全仓库唯一可调数字**：量比对比窗口（前 N 日均量，1-20，`:242-252`），存于
  `storage.stockVolumeCompare`。
- 分析页 `frontend/src/components/stock-analysis/AnalysisKChart.tsx`：只画 **11 组关键价位**
  （`LEVEL_GROUPS`，`:47-59`），**无** MACD/KDJ/RSI 副图。
- 查询键集中在 `frontend/src/lib/queryKeys.ts`，K 线查询封装在 `frontend/src/lib/kline.ts`
  （`klineDailyQueryOptions`）——新增参数必须进 cache key。

---

## 现状 vs TradingView 差距矩阵

| 能力 | TradingView | 本仓库现状 | 差距 | 落点 |
| --- | --- | --- | --- | --- |
| 指标库广度 | 100+ 内置+社区 | 内置 ~14 类固定 + 可选 `ta_*` 10 个 | 🔴 大 | `pipeline.py:111-205`、`ta_factors.py` |
| 添加/移除指标 | 任意 | 固定按钮行，只能切已内置的 | 🟡 中 | `StockDailyKChart.tsx:196-268` |
| 主图叠加 | 任意 | 仅 MA + BOLL | 🟡 中 | `EChartsCandlestick.tsx:423` |
| 副图面板 | 任意多个 | vol/MACD/RSI/KDJ/TA，集合固定 | 🟡 中 | `SUB_CHARTS`/`TA_SUB_CHARTS` |
| **参数可调** | 全参数可改 | **全硬编码**（唯一例外：量比窗口） | 🔴 大 | `pipeline.py:365+`、`kline.py:358` |
| 图例勾选 | legend 可点 | info bar 有值但**不可点** | 🟡 中 | `EChartsCandlestick` info bar |
| 指标设置对话框 | 有 | **无** | 🔴 大 | 前端新增 |
| 指标选择器（studies picker） | 有 | **无**（一排按钮） | 🟡 中 | 前端新增 |
| 布局/指标模板 | 可存/套用 | 无（仅量比一条 localStorage） | 🟡 中 | `storage` + 前端 |
| 数据获取 | 客户端算/子集 | 整表返回，仅可附加列，无周期传参 | 🟡 中 | `kline.py:358`、`repository.py:1402` |
| 分析页振荡副图 | 同 TV | 仅 11 组价位，无副图 | 🟡 中 | `AnalysisKChart.tsx`（已标未来项） |
| 样式（颜色/线宽） | 可改 | 写死在 `buildSeries` | 🟢 小 | `EChartsCandlestick.tsx` |

---

## 关键架构约束与决策

**约束（来自 CONTRIBUTING §3 / §5）：规范列是 SSOT。** `ma20`、`macd_dif`、`boll_upper`、
`rsi_14` 等列名被 screener、自定义信号 `ALLOWED_FIELDS`、回测 `feature_plan`、AI 提示词共同引用。
若让用户改周期后**复用列名**，等于在不知情的情况下改变这些消费方的语义（例如 `ma20` 变成 `ma10`），
属于错误的金融口径，禁止。

**因此决策方向（待本文评审确认，实现后补 ADR）：**
1. 新增一个 **chart indicator spec** 的 opt-in 请求参数（形如 `indicators=[{id,params}]` 或
   等价的紧凑编码），前端只在用户改过参数/叠加非默认指标时才带上。
2. 后端按 spec 计算**别名列**（例如 `ma_20_c`、`macd_12_26_9_c` 之类稳定命名，避免与规范列冲突），
   叠加在既有 enriched 帧上返回；`needed` 子集机制用于只算被请求的项。
3. 规范列照旧整表返回、语义不变；未带 spec 时**零变化**。
4. 新指标（含 `ta_*`）通过注册表暴露 `{id, 面板/叠加, 参数 schema}`，前端选择器据此自动列出。

> 命名与编码方式（别名规则、spec 编码、缓存键）是本文最需要用户拍板的点，见「假设与待决」。

---

## 优先级理由

- 先做**参数化**（Phase 1）：这是「像 TradingView 调指标」的核心痛点，也是当前 0 能力的短板；
  且它把后端 opt-in 别名列这套接缝一次性立起来，后续图例/选择器都建在它上面。
- 再做**图例勾选 + 选择器**（Phase 2）：纯前端交互，风险低，可独立验收。
- 最后做**指标注册化 / 任意叠加**（Phase 3）：动注册契约，面最大，需先有 Phase 1/2 的稳定接口。
- 不从生产指标列抽象公共层：第一期只有一个新调用方，先复制最小 compute 分支，
  不扩大 screener/回测面的回归风险（对齐 `research-target-weight-backtester-plan.md` 的同类取舍）。

---

## 假设与待决

| Item | Status | Impact | Owner / Next step |
| --- | --- | --- | --- |
| 自定义周期不得复用规范列名，只能用别名列 | confirmed（按 CONTRIBUTING 口径） | 决定整条实现路线 | 评审时确认，实现后写 ADR |
| 别名列命名规则 | unresolved | 影响前后端契约与 cache key | 评审拍板（建议 `<base>_<params>_c`） |
| spec 请求编码（JSON body vs 紧凑 query） | unresolved | 影响 `GET` 是否改 `POST` | 评审拍板（建议优先保持 GET + 紧凑编码） |
| 参数范围与非法值处理 | unresolved | 影响 fail-closed 行为 | 评审后写入规格（超范围拒绝，不静默截断） |
| 是否进 Figma/TV 式模板持久化 | deferred | 仅体验 | Phase 2 之后再说 |
| 分析页是否加副图 | deferred | 独立小项 | 可单独提为可选 Phase 2b |

---

## 阶段计划

（**Phase 0 是本次交付**：产出本文并等评审。下列 Phase 1-3 是候选路线，用户选定后再逐个开 gate。）

### Phase 0：差距冻结与范围选定（本次）

- **Purpose**: 固化差距矩阵、架构约束与候选路线，等用户选定范围再动代码。
- **Entry condition**: 无。
- **Phase rules**:
  - 只写文档，不改代码。
  - 不虚构未实现能力（对齐 AGENTS.md / `secondary-development.md` 的「不据设计示例虚构 API」）。
- **Todos**:
  - [x] 盘点后端指标目录、参数化程度、API 附加能力。
  - [x] 盘点前端可切换指标、图例/选择器/参数 UI 现状。
  - [x] 形成差距矩阵与候选路线（轻/中/重）。
- **Exit proof**: 本文落盘，用户确认走哪条路线。
- **Stop condition**: 用户未选范围前不进入 Phase 1。

### Phase 1：指标参数化（轻，推荐）

- **Purpose**: 让用户能改 MA/MACD/RSI/KDJ/BOLL 的参数，且不污染规范列。
- **Entry condition**: Phase 0 通过 + 用户选定「轻」。
- **Phase rules**:
  - 先写「默认参数 = 现行逐位一致」的 RED/GREEN 测试。
  - 别名列 opt-in；未带 spec 时响应字节等价。
  - 参数越界 fail-closed，不静默截断。
  - 只改 stock 日 K 路径，指数/ETF 暂不接（与 `ta-adapter.md` §7 现状一致）。
- **Todos**:
  - [x] 后端：spec 解析 + 参数化 compute + 别名列（新增 `indicators/custom.py`，不改规范列）。
    - **Surface**: `backend/app/indicators/custom.py`、`backend/app/api/kline.py`、`repository.get_daily`
    - **Proof**: `tests/test_indicators_custom_params.py`：默认参数产出的别名列与规范列逐位一致。
  - [x] 后端：读取路径接入（按 max_period 自适应预热 + 缓存覆盖判断）+ 未带 spec 时零变化回归。
    - **Surface**: `tests/test_kline_daily_indicator_specs.py`、`tests/test_kline_daily_ta_columns.py`
    - **Proof**: 51 passed（含 get_daily/cache/hot-path/ta/needed 回归）；新增 ruff 告警 0。
  - [x] 前端：设置面板（周期）+ localStorage 持久化 + 请求带 spec + 按别名列渲染。
    - **Surface**: `lib/chartIndicators.ts`、`StockDailyKChart.tsx`、`EChartsCandlestick.tsx`、`lib/kline.ts`、`lib/storage.ts`、`lib/queryKeys.ts`、`lib/api.ts`
    - **Proof**: `pnpm build`（tsc -b + vite）通过。
  - [x] 收口：ADR-005 + `docs/features.md` 指标章节更新。
    - **Surface**: `docs/adr/ADR-005-chart-indicator-params-alias-columns.md`、`docs/features.md`
  - [x] 浏览器验收（agent-browser 真机 2026-10-07）：改 MA(5,13,34)/RSI(7)/MACD(8,17,9) 即时重绘；切标的数据不串；请求带 `indicator_specs`；后端返回别名列。截图在会话 scratchpad。
  - [x] 全量回归：后端 `pytest` 1946 passed / 2 failed（auth 与 fuyao 为**既存失败**，`git stash -u` 空树复现确认与本改动无关）；`pnpm build` 通过。
- **Exit proof**: 定向 pytest + `pnpm build` 通过；页面截图；`git diff --check` 干净。
- **Stop condition**: 若必须改写规范列语义才能实现，立即停止并回到 Phase 0 重新设计。

### Phase 2：图例勾选 + 指标选择器（中）

- **Purpose**: 把「一排按钮」升级为 TV 式图例 + studies 选择器。
- **Entry condition**: Phase 1 通过 + 用户选定「中」。
- **Phase rules**:
  - 纯前端为主；如选择器需要后端提供「指标元数据」，新增只读端点，不改计算。
  - 保留现有按钮行作为降级（窄屏）。
  - **合规边界（2c）**：只做**外链跳转**与**导出本仓库自有数据**；不接入 TV 数据源或官方 MCP、不引入 TV/MCP 依赖、不在运行时向外部 TV 发起任何请求（对齐 Non-goals 与 README「遵守服务条款」立场）。
- **Todos**:
  - [ ] 图例组件：每个已绘制序列可点勾选/隐藏（替代只读 info bar）。
  - [ ] 选择器面板：分组（叠加 / 副图 / TA），可增删。
  - [ ] 可选 2b：分析页 `AnalysisKChart` 增加振荡副图（复用 `SUB_CHARTS` 模式）。
  - [ ] 可选 2c：TradingView 深链 / CSV 导出——「在 TV 打开」外链跳转 + 把**本仓库自有** K 线/指标数据导出为 CSV；不接 TV 数据、不引 TV 依赖。
    - **Surface**: `EChartsCandlestick.tsx`（图表工具条）、`lib/api.ts`（数据来自既有后端接口，不新增 TV 请求）
    - **Proof**: `pnpm build` + 页面验收（深链可打开对应标的、CSV 能正确读入）；人工核对运行时不产生对外 TV 域名请求
    - **待核对**: A 股代码→TV 交易所前缀（如 `SSE:` / `SZSE:`）的映射规则需实测确认，不在计划里臆断 URL 格式
- **Exit proof**: `pnpm build` + 交互验收（加载/空/错/窄屏/深浅色）。
- **Stop condition**: 选择器需要为每个指标写死前端逻辑时，停止，转 Phase 3 注册化。

### Phase 3：指标注册化 / 任意叠加（重）

- **Purpose**: 让新指标（`ta_*`、因子 DSL）自动进入选择器，无需前端写死。
- **Entry condition**: Phase 2 通过 + 用户选定「重」。
- **Phase rules**:
  - 需要注册契约（id / 面板位 / 参数 schema / compute），先出 ADR 再实现。
  - 不破坏现有因子注册表契约（对齐 `ta-adapter.md` §1 的既有约束）。
- **Todos**:
  - [ ] 定义并落地指标注册表 + 元数据端点。
  - [ ] 前端选择器改为读注册表渲染；参数 schema 驱动设置对话框。
  - [ ] 把 `ta_*` 与因子平台接入同一注册可见性。
- **Exit proof**: 新增一个指标只需后端注册即可在前端出现；回归全绿。
- **Stop condition**: 需要改 enriched schema 或数据迁移时停止，重新评审。

---

## 最终验证命令（预期，按选定阶段执行）

```bash
cd backend
uv run pytest tests/test_indicators_custom_params.py tests/test_kline_daily_ta_columns.py \
  tests/test_factor_registry.py -q
uv run ruff check app/indicators/pipeline.py app/api/kline.py

cd ../frontend
pnpm build

cd ..
git diff --check
```

---

## 第一步（选定范围后）

先写 `test_custom_params_default_matches_canonical`：断言「带默认参数的别名列 == 现行规范列逐位相等」，
运行确认因 `spec` 解析/别名列尚不存在而 RED；再只实现让该测试通过的最小 spec→别名列分支。
