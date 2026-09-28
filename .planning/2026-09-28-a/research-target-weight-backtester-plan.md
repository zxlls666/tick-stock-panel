# Goal Document: A 股研究目标权重回测器

## Go / No-Go

- **Judgment**: Go
- **Reason**: 两条研究策略已经能生成信号和目标权重；当前缺口是一个不修改生产撮合器、能真实处理目标权重叠加与交易约束的研究执行闭环。

## Target Outcome

新增一个研究专用目标权重回测模块，接受 `MarketDataMatrix` 与预先滞后好的 `[date × stock]` 目标权重，在次日开盘口径、整手、成本、停牌和一字板约束下生成可复核的每日账户账本、年度收益和组合统计；随后提供一个本地脚本，为短期反转与异常换手生成可复现的研究产物。

## Goal Definition

- **Type**: technical + learning
- **Boundary**:
  - 新增纯后端研究模块和定向测试。
  - 复用 `MarketDataMatrix`、`MatcherConfig`、现有研究策略与目标权重函数。
  - 第一期只支持日线、多头、无杠杆、目标权重、开盘调仓。
  - 研究脚本将 `--output` 视为单次运行目录；正式运行应传入 `data/research/a_share_strategy_validation/<run_id>`。
- **Non-goals**:
  - 不修改 `BacktestEngine` 生产撮合器。
  - 不接 API、任务队列、数据库或前端。
  - 不公开两条 `research_only` 策略。
  - 不新增 E/P、股息率、历史 ST 或退市数据。
  - 不实现日内成交、冲击模型、成交量容量和最低佣金。
- **Deferred work**:
  - 基准指数与信息比率的正式报告比较。
  - 历史 ST、退市样本和完整 PIT 流通股本。
  - 完整参数稳健性网格和长期全样本研究。
- **Verification rule**: 合成数据行为测试必须证明执行日期、现金、整手、成本、停牌、涨跌停和统计口径；现有相关回归测试必须保持通过。
- **Evidence source**: pytest、`git diff --check`、`py_compile`，以及研究脚本生成的 manifest/parquet/report 产物。
- **Pass criteria**:
  - 新模块所有定向测试通过。
  - 现金始终非负，股数为整手，权重输入失败关闭。
  - 涨停不买、跌停不卖、停牌不成交，阻塞金额可见。
  - 不修改生产撮合器和公开策略入口。
  - 真实数据 smoke run 能写出完整研究目录，或明确记录数据前提阻塞。
- **Confidence note**: 合成测试可以高置信证明撮合账本行为；真实策略有效性仍受幸存者偏差、历史 ST 缺失和股本回退影响。
- **Judgment owner**: pytest 与产物检查负责技术完成；是否继续扩大研究由用户决定。

## Current State

- `short_term_reversal_20d` 与 `abnormal_turnover_1m_12m` 已为 `research_only`。
- `_research_portfolio.py` 已提供五切片、月末持有、低值分组和规模残差。
- 生产撮合器对同一标的只保留一个仓位，不能表达多切片目标权重叠加。
- `MarketDataMatrix` 已包含 OHLC、成交量、`tradable`、`limit_up_locked`、`limit_down_locked`。
- `MatcherConfig` 已是成本口径 SSOT：买入为佣金+滑点，卖出再加印花税。
- 当前股票池缺退市样本和历史 ST；异常换手的流通股本仍可能回退当前值。

## Priority Rationale

- 先证明账户账本，再接真实策略；否则报告只是对未验证执行逻辑的包装。
- 先做纯函数模块，接口只有 `market + target_weights + config`，把复杂度藏在一个深模块内。
- 不从生产撮合器抽象公共层：目前只有一个新调用者，复制极小的一字板判断比扩大生产回归面更安全。

## Assumptions and Open Decisions

| Item | Status | Impact | Owner / Next step |
|---|---|---|---|
| `target_weights[t]` 已包含信号滞后 | confirmed | 回测器不得再次 shift | 测试锁定月末次日与五切片首日现金 |
| 权重之和不超过 1 | confirmed | 不支持杠杆 | 输入校验失败关闭 |
| 交易在每日开盘执行一次 | confirmed | 自然满足当日买入不能当日卖出 | 合成测试锁定 |
| 非交易日/停牌持仓估值 | assumed | 使用最近有效收盘价 | 测试记录 stale position |
| 普通非一字涨跌停 | deferred | 第一期与生产撮合器一致，只拒绝一字板 | manifest 明示限制 |
| 真实数据正式起止区间 | unresolved | 影响 smoke run 和最终报告 | 先用数据可用范围末端的短区间 smoke |

## Phases

### Phase 1: 锁定执行接口与最小行为

- **Purpose**: 用测试定义目标权重回测器的外部接口和账户不变量。
- **Entry condition**: 本计划已落盘。
- **Phase rules**:
  - 必须先看到缺模块/缺行为的 RED。
  - 只新增研究模块，不编辑生产撮合器。
  - 目标权重不在回测器内二次移位。
- **Todos**:
  - [x] 写“恒定价格、零成本、目标权重买入后净值不变”的失败测试。
    - **Surface**: `backend/tests/backtest/test_research_target_weights.py`
    - **Proof**: pytest 因模块或接口缺失而失败。
  - [x] 实现最小 `simulate_target_weights()`、结果数据结构和输入校验。
    - **Surface**: `backend/app/backtest/research_target_weights.py`
    - **Proof**: 首个测试转绿。
- **Exit proof**: 首个 RED→GREEN 证据记录在 `progress.md`。
- **Stop condition**: 若必须修改生产撮合器才能完成首个行为，停止并重新审查 seam。

### Phase 2: 补齐真实执行约束

- **Purpose**: 锁定成本、整手、现金、停牌和一字板行为。
- **Entry condition**: Phase 1 通过。
- **Phase rules**:
  - 先卖后买。
  - 被阻塞的交易不伪造成交；目标在下一日仍存在时重新尝试。
  - 买入资金不足时按需求比例缩放，不按资产顺序抢现金。
- **Todos**:
  - [x] 测试并实现佣金/印花税/滑点精确扣减。
  - [x] 测试并实现一字涨停不买、一字跌停不卖。
  - [x] 测试并实现停牌不成交与最近有效收盘估值。
  - [x] 测试并实现 100 股整手和现金非负。
  - [x] 测试负权重、NaN、超 100% 和 shape 不匹配失败关闭。
- **Exit proof**: 新模块定向测试全部通过。
- **Stop condition**: 若合成场景出现现金为负、卖空或未来数据使用，立即停止。

### Phase 3: 组合账本与统计

- **Purpose**: 从每日真实净值计算研究需要的可审计指标。
- **Entry condition**: Phase 2 通过。
- **Phase rules**:
  - 不复用按逐笔交易近似的 `_calc_stats()`。
  - 统计必须从每日净值和实际成交金额计算。
- **Todos**:
  - [x] 输出每日 equity/return/drawdown/cash/exposure/tracking error/turnover/cost/blocked/stale。
  - [x] 输出年度收益。
  - [x] 输出总收益、年化收益、年化波动、Sharpe、Sortino、最大回撤、Calmar、月胜率、年化换手、成本和期末预计清算成本。
  - [x] 用已知收益路径验证统计。
- **Exit proof**: 统计测试通过，结果无 NaN/inf JSON 风险。
- **Stop condition**: 指标无法由账本复算时停止，不进入脚本阶段。

### Phase 4: 研究运行脚本与产物

- **Purpose**: 把两条现有研究策略接到目标权重回测器，形成可复现研究目录。
- **Entry condition**: Phase 3 通过。
- **Phase rules**:
  - 脚本是本地研究入口，不接公开 API。
  - 产物必须包含偏差警告、参数、成本和数据 generation。
  - 第一版只做四个主组合与有限成本场景，不做参数搜索框架。
- **Todos**:
  - [x] 新增 `backend/scripts/run_a_share_strategy_validation.py`。
  - [x] 生成短期反转五切片、短期反转月频、异常换手月频原始、异常换手月频规模中性目标权重。
  - [x] 写 `manifest.json`、`summary.parquet`、`daily.parquet`、`yearly_returns.parquet`、`report.md`。
  - [x] 运行短区间 smoke；本次输出位于 `/tmp/tsp-a-share-validation-smoke`，仅验证管道。
- **Exit proof**: 研究目录可被重新读取，manifest 与 parquet 行数/字段检查通过。
- **Stop condition**: 无法确认数据 generation 或正式区间预热不足时，不发布收益结论。

### Phase 5: 回归验证与收口

- **Purpose**: 证明新研究模块没有污染现有策略与矩阵执行。
- **Entry condition**: Phase 4 完成或明确阻塞。
- **Phase rules**:
  - 不提交、不发布。
  - 不覆盖用户已有工作区修改。
- **Todos**:
  - [x] 运行新增测试、研究策略测试、因子注册/存储、矩阵策略、成本模型、涨跌停测试。
  - [x] 运行 `git diff --check` 与新增模块 `py_compile`。
  - [x] 更新 `task_plan.md`、`findings.md`、`progress.md`。
- **Exit proof**: 新鲜验证输出记录完整；git 状态复核无越界文件。
- **Stop condition**: 任一相关回归失败则不宣称完成。

## Dry-Run Findings

- `MarketDataMatrix` 已提供回测器需要的价格、可交易和一字板字段，无需新增数据结构。
- `MatcherConfig` 可以直接复用成本方法，无需创建第二套成本配置。
- 生产 `_calc_stats()` 基于逐笔收益近似，不适合目标权重账户；研究模块需自行从每日净值计算。
- `_research_portfolio.py` 的权重已经负责执行滞后，回测器再次 shift 会造成前视修复过度。
- 真实数据脚本需要 warmup 后再裁正式区间；异常换手至少 250 个有效交易日。
- 项目现有 CodeGraph 索引在 `/Users/serge/Projects`，已核对 `MarketDataMatrix`、成本和相关测试的影响范围。

## Final Validation

```bash
uv run --project backend pytest \
  backend/tests/backtest/test_research_target_weights.py \
  backend/tests/test_research_a_share_factors.py \
  backend/tests/test_factor_registry.py \
  backend/tests/test_factor_store.py \
  backend/tests/backtest/test_matrix_strategy.py \
  backend/tests/backtest/test_cost_model.py \
  backend/tests/test_price_limits.py -q

git diff --check
python -m py_compile \
  backend/app/backtest/research_target_weights.py \
  backend/scripts/run_a_share_strategy_validation.py
```

## First Execution Step

先写 `test_constant_price_zero_cost_preserves_equity`，运行并确认因为 `research_target_weights` 尚不存在而 RED；然后只实现让该测试通过的最小回测接口。
