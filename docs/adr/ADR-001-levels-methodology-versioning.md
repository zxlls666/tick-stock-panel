# ADR-001: 关键价位口径版本化

- **状态：** 已采纳
- **日期：** 2026-10 (v0.2.3)
- **决策者：** Serge

## 上下文

- 背景：`backend/app/indicators/levels.py` 的 `compute_levels` 产出 11 组关键价位（sr /
  pivot / extreme / boll / keltner_s / keltner_m / keltner_l / atr_stop / gap / fib /
  round），经 `GET /api/stock-analysis/levels` 下发、经 `/analyze` 流式 meta 事件下发，并随
  AI 个股报告持久化，前端按组渲染与回放。
- 约束：任一分组的数值、点数、语义或分组变化都会改变下发内容；旧报告不会自动重算。若没有
  显式版本，事后无法判断一条历史报告或一次回放用的是哪种口径，口径变更会**静默改写**旧报告
  的含义。
- 仓库既有范式：
  - `backend/app/backtest/factor.py` 的 `FACTOR_METHODOLOGY_VERSION = "factor_v2"`（常量 +
    结果对象默认字段 + API 兜底）。
  - `backend/app/tickflow/policy.py` 的 `_CACHE_SCHEMA_VERSION`（内联 bump 日志注释 + 测试钉
    下限）。
- 契约约束：`compute_levels` 的返回类型是 `dict[str, list[dict]]`，`summarize_levels` 与前端
  `Record<LevelType, PriceLevel[]>` 都按组遍历，版本不能塞进该字典值。
- 可选方案：
  1. 不版本化，口径变更只靠文档与提交记录。
  2. 逐分组维护版本（11 个常量）。
  3. 单个常量覆盖整个 `compute_levels` 输出，作为返回值的**兄弟字段**全链路下发。
  4. 把版本塞进 `compute_levels` 返回值。

## 决策

- 在 `levels.py` 新增单一常量 `LEVELS_METHODOLOGY_VERSION = "levels_v1"`，覆盖整个
  `compute_levels` 输出（11 组）。
- 版本作为**兄弟字段**随响应/事件下发，**不进入** `compute_levels` 的返回字典：
  `/levels` 的空数据分支与正常分支、`/analyze` 的 meta 事件都带该字段。
- 版本随 AI 报告持久化；保存请求字段可选，缺省时后端兜底为当前版本（兼容旧客户端）。
- 前端类型与回放链路同步该可选字段（`StockLevels` / `AiStockReport` / 流式 meta /
  商店的任务与历史记录）。
- **bump 政策**：任何改变 `compute_levels` 输出（数值 / 点数 / 语义 / 分组）的改动，都必须
  bump 该常量并在常量旁追加一行说明；测试钉死当前值为 `levels_v1`。

## 理由

- 只需一个版本号即可覆盖实际消费契约：前端与报告消费的是整份 11 组结果，而不是单组；逐组
  版本没有消费方，属于过度设计。
- 与仓库既有的 `factor_v2` / `_CACHE_SCHEMA_VERSION` 模式一致，便于共同理解与审查。
- 作为兄弟字段而非返回值成员，保持 `compute_levels` 纯函数契约不变，`summarize_levels` 与
  前端渲染无需改动数据结构。
- 字段可选 + 后端兜底 + 旧报告按空处理，符合 CONTRIBUTING §8「新增字段优先提供默认值，
  读取历史数据时允许字段缺失」，无需数据迁移。

## 后果

- 正面：口径变更变为显式、可追溯；旧报告保留其生成时的版本，不再被静默改写；回放可核对口径。
- 负面 / 边界：bump 依赖人工纪律，当前没有 CI 强制；版本只标识整份输出，无法定位到具体某组
  变化（由 bump 日志注释补充说明）。
- 需要后续验证：口径发生实质变更时，必须同步 bump 常量、更新 bump 日志并复跑
  `backend/tests/test_levels.py` 的版本钉死断言。
- 本决策不改动任何计算：`levels_v1` 即当前线上口径，落地时**不产生口径变更**。

## 替代方案否决理由

- **不版本化**：无法判断历史报告与回放使用的口径，口径变更会静默改写旧报告语义。
- **逐分组版本**：11 个常量，无消费方需要单组粒度，增加维护面而无收益。
- **版本塞进 `compute_levels` 返回值**：破坏 `dict[str, list[dict]]` 契约，`summarize_levels`
  与前端按组遍历会受影响。
- **把版本当查询键（如 `stockLevels(symbol, days)` 入参）**：版本是响应内容而非影响结果的入参，
  不应参与缓存键。
