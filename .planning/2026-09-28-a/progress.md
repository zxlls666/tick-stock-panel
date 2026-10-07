# Progress Log

## Session: 2026-09-28

### Current Status

- **Phase:** 8 - Full-history robustness validation
- **Status:** full-history cost/subperiod/parameter study completed; monthly reversal and abnormal turnover are internally stable but remain research-only; five-sleeve reversal is paused
- **Data/research snapshot:** 2026-09-28; local mining artifacts mainly `data_as_of=2026-09-07` or `2026-09-24`

### Actions Taken

- 用户确认后实现 `short_term_reversal_20d` 与 `abnormal_turnover_1m_12m`。
- 五切片和月末持有放在 `backend/app/strategy/builtin/_research_portfolio.py`，不改生产撮合器的单标的仓位。
- 异常换手使用 enriched 换手率：`apply_historical_float_shares` 在历史日优先公告日不晚于交易日的流通股本，缺失时回退当前股本。因子因此不能标成完整 PIT。
- 两条策略均为 `research_only`，不进公开选股目录。
- E/P、股息率、退市和历史 ST 仍未实现。

- 完整阅读 `CONTRIBUTING.md` 与 `docs/secondary-development.md`。
- 从 `/Users/serge/Desktop/A股交易策略研究报告.html` 提取策略主线与引用。
- 盘点因子注册表、财务 PIT、矩阵排名策略、内置策略、自定义信号和自动挖掘产物。
- 检查本地财务 parquet schema：109,810 行、29 列；无股息率，无明确 TTM 盈利字段。
- 对照 E/P、短期反转、异常换手、清洗后小市值、低波防御组合和中期动量反证。
- 读取并核对 Liu、Stambaugh、Yuan 工作论文、Factor Investing in China 复刻论文和 MSCI 低波文章。
- 形成四级逐项矩阵和五个缺失策略的最小研究规格，写入 `findings.md`。
- 随后按确认实现上述两项研究策略；未改 `.gitignore`、`.commandcode/`、`.hermes/`。

### Verification Results

| Check | Expected | Actual | Status |
|---|---|---|---|
| 报告副本可读 | 能提取策略与来源 | 已用标准库解析 Desktop 副本 | PASS |
| 财务 PIT 口径 | 公告后生效，缺值非 0 | `date > announce_date`，null 保留 | PASS |
| E/P 可用性 | 应有 PIT TTM 盈利 | 仅 `eps_basic`，无明确 TTM 字段/因子 | BLOCKED |
| 股息率可用性 | 应有历史 PIT 股息率 | 生产数据和因子注册均无 | BLOCKED |
| 短期反转底层因子 | 应有 20 日累计收益 | `momentum_20d` 已存在；研究权重在 `research_portfolio.py` | PASS (research only) |
| 异常换手精确公式 | 应有 20/250 日均换手比 | `abnormal_turnover_1m_12m` 已注册；股本缺失时仍回退当前流通股本 | PASS (research only, PIT partial) |
| 无偏小市值宇宙 | 含退市、历史 ST | 数据集未落地；当前 ST 为静态名称过滤 | BLOCKED |
| 现有低波策略同口径 | 应含低波/价值/股息/质量 | 实际为正动量 + 波动阈值 + MA20 | FAIL / DIFFERENT |
| A 股成交约束基础 | T+1、一字板、成本可配置 | 引擎具备基础支持，但非完整盘口模型 | PARTIAL PASS |
| 工作区保护 | 不覆盖已有修改 | 未触碰用户已有 `.gitignore`、`.commandcode/`、`.hermes/`；研究实现改动限于因子、矩阵、策略与对应测试 | PASS |

### Key Evidence Recorded

- 本地 `momentum_20d/30d/60d` 的 IC 均为负，支持当前样本中的反转方向，但不是无偏最终证明。
- 本地 `annual_vol_20d` 与 `amplitude` 为负向 IC，支持低波线索。
- `log_float_mv` 有负向 IC，但跨年稳定度不足。
- 财务质量因子整体弱或方向不稳；不得照搬自动挖掘的单次方向。
- 一手研究给出的异常换手精确式为过去 20 日平均换手 / 过去 250 日平均换手，低值优先。
- 一手研究要求壳股清洗并包含退市样本；当前项目尚不满足。

### Git Status Recheck

- Modified: `.gitignore`（用户已有，未触碰）
- Untracked: `.commandcode/`、`.hermes/`（用户已有，未触碰）
- Modified: 研究因子/矩阵/测试相关文件；另有用户已有 `.gitignore`
- Untracked: `.planning/` 与三份研究策略文件、对应测试；另有用户已有 `.commandcode/`、`.hermes/`

### Fresh Verification

- `uv run --project backend pytest backend/tests/test_research_a_share_factors.py backend/tests/test_factor_registry.py backend/tests/test_factor_store.py backend/tests/test_strategy_registry.py backend/tests/test_factor_api.py backend/tests/test_strategy_scoring.py backend/tests/backtest/test_matrix_strategy.py -q`
- 结果：`74 passed`。
- 同时通过 `git diff --check` 与三份新增策略模块的 `py_compile`。

### Session: Research Target-Weight Backtester

- 用户要求先落盘方案再执行。
- 已新增 `.planning/2026-09-28-a/research-target-weight-backtester-plan.md`。
- 方案固定边界：研究模块独立于生产撮合器；先 TDD 纯撮合账本，再接研究脚本；不接 API/前端，不补新数据源。
- Phase 7 已进入 `in_progress`。
- **RED 证据 1**：`uv run --project backend pytest backend/tests/backtest/test_research_target_weights.py -q` 在收集阶段因 `ModuleNotFoundError: app.backtest.research_target_weights` 失败；失败原因正是目标模块尚未实现。
- **GREEN 证据 1**：最小接口落地后，同一测试 `1 passed`。
- **RED 证据 2**：补入成本、一字板、停牌、按比例买入、每日净值统计和失败关闭测试后，`6 failed, 5 passed`；失败点分别是缺账本字段、未拒绝一字板/停牌、缺组合统计。
- **GREEN 证据 2**：实现账户账本与约束后，`backend/tests/backtest/test_research_target_weights.py` 为 `11 passed`。
- 真实数据首次 smoke 返回四个 `nan`；根因是稀疏股票面板中 `0 * NaN` 参与全轴 dot，污染现金与净值。新增失败测试后修复为只对可成交子集求和，定向测试增至 `12 passed`。
- 新增 `backend/scripts/run_a_share_strategy_validation.py`，输出四个研究组合的 `manifest.json`、`summary.parquet`、`daily.parquet`、`yearly_returns.parquet`、`report.md`。
- smoke 区间 `2026-07-01` 至 `2026-09-24`、Top-N 20、单边滑点 10 bps，四个组合均生成 62 个正式交易日的有限净值；结果仅用于管道验证，不作为策略结论：
  - `reversal_five_sleeves`: -1.61%
  - `reversal_monthly`: +12.48%
  - `abnormal_turnover_monthly_raw`: +11.51%
  - `abnormal_turnover_monthly_size_neutral`: +4.24%
- 相关回归验证：`109 passed, 1 warning`；warning 来自既有 Polars `join_asof` sortedness 提示。

### Session: Full-History Robustness Validation

- 将 `backend/scripts/run_a_share_strategy_validation.py` 扩展为正式批量研究入口；场景定义先通过新测试 RED（缺 `_cost_scenarios`）再 GREEN。
- 完整数据区间：`2020-09-09` 至 `2026-09-24`，矩阵 `1466 × 5569`，generation `0b7900c875e347088914a263585b81e5`。
- 运行 52 条账户路径：20 成本场景、12 子时期、20 参数敏感性；耗时 22.355 秒。
- 正式产物：`data/research/a_share_strategy_validation/20260928T-full-history-v1/`，共 4.5 MB。
- 质量检查：`summary=(52,27)`、`daily=(57585,29)`、`yearly=(276,11)`；无重复 summary，equity 和 daily_return 全部有限。
- 成本场景：gross、base（佣金 2 bps + 卖出印花税 5 bps + 滑点 5 bps）、纯 10/20/40 bps 单边冲击。
- 参数敏感性：Top-N 10/20/30；反转 10/20/30 日；异常换手 15/200、20/250、30/300；每次只改变一个参数并共享最晚成熟起点。
- 判断：
  - `reversal_five_sleeves`: **pause**。40 bps 后总收益 -52.75%，早期子时期 -19.49%，10 日窗口 -32.67%。
  - `reversal_monthly`: **internally stable**。40 bps 后仍 +38.69%，三个子时期和所有相邻参数均为正；但最大回撤约 -46.38%。
  - `abnormal_turnover_monthly_raw`: **internally stable / highest priority**。base 年化 28.30%、Sharpe 1.09；40 bps 后仍 +155.72%；三个子时期和相邻参数均为正。
  - `abnormal_turnover_monthly_size_neutral`: **internally stable**，但 base 年化 21.92%、Sharpe 0.86，弱于原始版本且最大回撤约 -46.84%。
- 上述 internally stable 仅表示当前有偏数据内稳定，不能发布；仍缺退市样本、历史 ST、完整 PIT 股本和基准比较。

### Session: Bias Repair and Execution Realism Planning

- 新增执行与验收文档：`.planning/2026-09-28-a/research-data-bias-and-execution-realism-plan.md`。
- 文档将工作拆为：快照冻结、基准+严格 PIT、退市+历史 ST、容量+冲击、统一重跑、分钟/Level-2 数据门控。
- 明确当前 PIT 股本已覆盖 99.8168%，后续目标是 strict 模式而不是重复回填。
- 明确 `queue_model=proxy` 是日线阶段唯一诚实口径；精确排队必须等待 Level-2。
- 当前只落盘计划，未修改代码、数据或生产配置。

### Session: Bias Repair Phase 0–1 Execution

- **Phase 0**: 新增 `research_run_manifest` 模板与 `audit_research_data_baseline.py`；冻结产物 `data/research/a_share_strategy_validation/20260928T-phase0-baseline/`。
  - 退出证明：同快照重跑 `content_sha256` 相同 = `4d81b8422ff735c0ba74ce6cf37f363f0fc19a949e531c2f48ff4e861a2c9b22`。
  - 冻结 `data_generation=0b7900c875e347088914a263585b81e5`，enriched 1466 日，shares 175880，summary sha 与 full-history-v1 一致。
- **Phase 1 基准**: 回填 `000985/000300/000905/000852` 至 `2019-01-02..2026-09-24`（各 1877 行）；策略日缺失 0，前置交易日 `2020-09-08` 存在；股票 generation 未漂移。
- **Phase 1 strict PIT**: `apply_historical_float_shares(strict=True)` + `float_shares_source`；默认生产回退行为保持不变。
- **Phase 1 bridge**: `20260928T-phase1-bridge/`；`current_fallback_count=0`，`missing_count=919130`。
  - `reversal_monthly`: legacy=strict 总收益 +131.67%；相对中证全指超额 +109.78%，IR 0.64。
  - `abnormal_turnover_monthly_raw`: strict 后总收益 230.19%→201.26%（Δ -28.9pp）；全指超额仍 +197.89%，IR 1.42。
  - `abnormal_turnover_monthly_size_neutral`: Δ -0.52pp；全指超额 +155.15%，IR 1.18。
- 未触发 Phase 1 研究停止条件；仍未解除 research_only。
- 独立复核（2026-09-28）：
  - `backend/tests/test_financial_shares.py` 与三组 Phase 0–1 定向测试共 `17 passed in 0.76s`。
  - 四只指数各 1,877 行，日期均为 `2019-01-02..2026-09-24`。
  - bridge parquet 为 3 行、无空统计值，结果与报告一致。
  - `ruff check` 尚有 16 项：1 个 import block、1 个 typing import、2 个未使用 datetime import、12 个中文 docstring 标点；不触发研究停止条件，但进入 Phase 2 前需最小清理并复跑验证。
- 口径澄清：`missing_count=919130` 是 `1466 × 5569 = 8,164,154` 稠密矩阵中的空单元格（11.26%），包含股票未上市/已无行情等结构性空位；不能解释为 919,130 条真实行情缺 PIT 股本，也不能与实际行情行覆盖率 99.8168% 直接比较。
- 下一步：先收口 Phase 1 Ruff，再进入 Phase 2 退市/ST。

### Session: Bias Repair Phase 2 Data and Universe Audit

- BaoStock 研究隔离数据已落盘：`20260928T-phase2-baostock-v1/`，覆盖研究区间 212 只退市股；210 loaded、2 no-data、0 error，152,026 条可交易日 K，标的覆盖率 99.06%。
- 新增全市场 SH/SZ PIT ST 构建器 `backend/scripts/build_a_share_st_history.py`，支持 `--resume`；正式续传复用 2,320 个 raw parquet，只补抓余下 3,114 只。
- 正式 ST 产物 `20260928T-phase2-st-history-v1/`：5,434 只全部 loaded、0 error，6,495 个状态变化点，键重复 0。
- 新增研究 instruments 合并：对生产轴按 symbol 覆盖 BaoStock 退市日期，而不是简单追加重复标的；对只有退市行情的资产追加独立行。
- 新增 `build_observation_mask` 和 `audit_a_share_research_universe.py`，在生产行情和退市行情联合观察轴上生成逐日 funnel、symbol audit、unknown 明细和 manifest。
- 正式 universe 审计 `20260928T-phase2-universe-audit-v1/`：
  - 1,466 个交易日，5,779 只观察资产，7,398,095 条 `(symbol,date)` 观察；
  - 5,569 个生产行情资产 + 210 个退市行情资产；
  - SH/SZ 日观察的 ST unknown 为 0；
  - 269,940 个 unknown 观察全部来自 347 只 BJ 股票，strict universe fail-closed 排除；
  - 首日 eligible 3,558，末日 eligible 4,965；210 个退市资产中 200 个曾通过 observation-only universe。
- 本轮只审计 universe，不发布收益：退市价格仍为未复权，PIT 因子有效性尚未接入，北交所 ST 尚缺。
- 新增研究 overlay：`mask_research_values()` 在截面排序前将非 eligible 单元置空；`with_research_price_limits()` 在不修改生产矩阵的前提下，按 PIT `price_limit_pct` 重算研究涨跌停锁定。
- TDD 证据：新增行为测试先因缺 `merge_research_instruments` / `build_observation_mask` / `mask_research_values` 失败，最小实现后通过；相关 Ruff 全绿。

### Session: Phase 2 adjusted delisted bars and account waterfall

- 退市前复权正式快照：`data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/`；210 只、152,026 行，44 只/23,146 行发生实际复权，最后交易日因子均为 1；volume 已从 BaoStock 股转换为内部手。`adjusted-v1` 已标 superseded。
- universe 审计升级为 reversal/turnover 双 PIT funnel：`20260928T-phase2-universe-audit-v2/`；SH/SZ ST unknown 为 0，347 只 BJ 继续 fail-closed。
- 全轴 smoke 验证退市标的可进入真实信号：反转 2,843 次、异常换手 24 次；strict PIT turnover `current_fallback=0`。
- 新增正式瀑布入口 `backend/scripts/run_bias_repair_phase2_bridge.py`；每阶段重新形成权重并模拟账户，不事后扣减。
- `phase2-waterfall-v1` 因规模中性使用通用复权 close 市值已标 superseded；研究 strict overlay 修为 `log(PIT float_shares × raw_close)` 后生成 `phase2-waterfall-v2`。
- Phase 2 v2 结果：
  - 异常换手 raw：最保守 0% 恢复总收益 +120.02%，相对中证全指超额 +117.56%，退市持仓 1 次。
  - 异常换手 size-neutral：最保守总收益 +101.67%，超额 +99.42%，退市持仓 1 次。
  - 月频反转：100% 恢复仅 +9.32%，70% 为 -64.23%，0% 为 -98.40%，退市持仓 74 次；触发方向翻转停止规则，暂停后续容量扩展。
- Phase 2 定向测试 `43 passed`，Ruff 全绿，`git diff --check` 通过；15 条 v2 summary 唯一，19,250 条 daily 的 equity/return 全部有限。

### Session: Phase 3 ADV capacity, impact, and queue proxy

- 研究回测器新增默认关闭的 `max_adv_participation`、`impact_eta`、`queue_proxy`；禁用时对 Phase 2 的 15 行 summary 逐列逐值精确兼容。
- ADV20 和 vol20 均只使用 `t-1` 及更早有效观测；缺 ADV/vol 时当日 fail-closed 为零成交并记入未成交。
- queue proxy 三档固定为 optimistic/base/conservative；日线结果只标 `queue_model=proxy`，不声称 Level-2 精确排队。
- 正式产物：`data/research/a_share_strategy_validation/20260928T-phase3-capacity-v2/`，296 条账户路径、summary 无重复、357,568 条 daily 全部有限、参与率越界 0。
- 1000 万最保守场景（1% ADV、eta=0.50、conservative）：raw +87.94%、超额 +85.84%、IR 0.89；size-neutral +71.63%、超额 +69.71%、IR 0.79。
- 1000 万全网格最大未成交率 raw 40.88%、size-neutral 37.80%；最大清仓天数 17/11。容量风险显著，但未触发收益方向停止条件。
- 定向验证 `49 passed`，Ruff 全绿，`git diff --check` 通过；eta 成本和资金规模未成交率单调性检查均通过。

### Session: Phase 4 final publication gate

- 正式最终产物：`data/research/a_share_strategy_validation/20260928T-phase4-final-v3/`；v3 在 v2 结论基础上补齐双 universe funnel、1000 万 capacity summary 和输入/输出 SHA-256。
- 产物包括 `summary.parquet`、`daily.parquet`、`yearly_returns.parquet`、`yearly_active.parquet`、`bias_waterfall.parquet`、`verdicts.parquet`、`manifest.json`、`report.md`。
- Phase 1–3 瀑布统一为 `legacy → strict PIT → PIT ST → delisted recovery 70% → 1000万最保守容量/冲击`；反转已暂停，因此不伪造容量阶段。
- 最终结果：
  - abnormal turnover raw：`bias_reduced_candidate`；base +127.48%，20 bps +108.71%，40 bps +78.19%。
  - abnormal turnover size-neutral：`bias_reduced_candidate`；base +107.79%，20 bps +90.00%，40 bps +61.58%。
  - reversal monthly：`pause`；base -64.23%，20 bps -70.45%，40 bps -78.58%，三个子时期超额和相邻参数均为负。
- 两条异常换手三个子时期超额均为正，年度正超额不由单一年份垄断；但 2026 年主动收益均为负，继续样本外监控。
- 最终定向验证 `50 passed`，新增 verdict/waterfall 单测 `2 passed`；Ruff 全绿，`git diff --check` 通过。
- 未提交、未 push；用户已有 `.gitignore`、`.commandcode/`、`.hermes/`、`.planning/.active_plan` 未触碰或纳入提交。

### Session: Phase 10 paper-only OOS observer

- 新增 `backend/scripts/run_a_share_oos_observer.py`：同一条一次性 CLI 供手动运行和未来外部 cron/launchd 调用；脚本不常驻、不安装调度、不下真实订单。
- 冻结 OOS 起点 `2026-09-25`、20/250 异常换手、Top 20、70% 退市恢复和四个独立影子账户；每次固定从 OOS 起点重放，20 个前置交易日只作 ADV20/vol20 预热，不计入 OOS 统计。
- 交易所日历用于识别真实已完成月末及下一交易日；月末当天即使未来行情尚未落地，也会输出 pending `target_weights`。
- 输出冻结为 `signals.parquet`、`target_weights.parquet`、`daily_accounts.parquet`、`summary.parquet`、`monthly_metrics.parquet`、`signal_overlap.parquet`、`data_quality.parquet`、`manifest.json` 和 `report.md`；同一 `as_of` observation 禁止覆盖。
- Fail-closed 门包括 generation 漂移、行情末日不等于 `as_of`、末日覆盖低于近 20 日中位数 95%、instruments 快照日期不一致、ST/退市 manifest 覆盖不足、strict PIT 当前股本回退非零及不完整交易日历。
- TDD/质量验证：observer 定向测试 `13 passed`，Ruff 全绿，CLI `--help` 正常。
- 真实边界 dry-run（`as_of=2026-09-28`）正确失败于 `latest market coverage is incomplete: latest=3152, baseline=5550`，且没有留下半成品 observation。当前另有 ST manifest 仅到 `2026-09-24`、instruments 快照为 `2026-09-29`，因此首个成功 observation 仍等待上游数据对齐。
- 调度决策：先人工核对 1–2 个完整月末换仓周期；确认幂等、输入同步和失败不覆盖后，再由外部调度器调用同一 CLI，仓库内不新增第二套运行逻辑。

### Session: First complete OOS manual closure (2026-09-29)

- 通过现有 `/api/kline/repair_daily` 官方路径从 `2026-09-28` 重拉到当日；job `2bdda80b32` 成功，universe 5,882，只读审计显示 `2026-09-28/29` 日线与 enriched 分区分别为 5,557/5,559 只，generation 更新为 `3684e7d12caa4cdcb23ed66370de5477`。
- 根因取证：当天早先盘后任务曾在 instruments 缩成 3,413 只后运行，导致 9 月 28 日只落 3,152 只；repair 后恢复完整轴。后续调度必须保证盘前 instruments 不被盘后不完整同步覆盖。
- 修复 `build_a_share_st_history.py --resume`：现在会按每只股票缓存末日增量追加，不再把旧缓存直接冒充新覆盖。RED/GREEN 后测试为 3 passed。
- 新 ST 快照：`data/research/a_share_universe/20260929T-oos-st-history-v2/`；5,435 只全部 loaded、0 error，旧 6,495 个变化全部保留，新增 3 个状态（300096 摘帽、300211 摘帽、301716 新上市初始状态）。
- 新退市覆盖证明：`data/research/a_share_universe/20260929T-oos-delisted-coverage-v1/`；BaoStock `query_stock_basic` 与 adjusted-v2 的 212 只退市集合逐字段比较，added/removed/changed 均为 0，因此复用冻结 adjusted-v2 行情。
- 首次 observer 真实运行暴露退市 bars 早于局部市场轴；新增 observer 局部裁剪函数与回归测试，不放宽共享 `append_research_bars()` 的 fail-closed 约束。
- 首次生成包又暴露“尚无信号的现金账户被报告为相对基准超额”；原包已归档到 `data/research/a_share_oos/_superseded/as_of=2026-09-29-prestart-active-metrics-v1/`，正式包在首个目标生效前将 excess/IR 设为 null、月度指标留空。
- 正式首包：`data/research/a_share_oos/as_of=2026-09-29/`。四账户、2 个 OOS 交易日、全程现金、0 信号/0 target、strict PIT fallback=0；末日 eligible=4,969，ST unknown=348（北京市场 fail-closed）。9 月 29 日不是月末，首个信号应在 9 月 30 日收盘形成并于 10 月 8 日开盘影子执行。

### Session: First month-end OOS signal (2026-09-30)

- 生产 9 月 30 日日线/enriched 各 5,561 行，generation `089d618543344735bbdc773fdab75dc6`；当前生产 instruments 已滚到 10 月 1 日，因此未修改或倒填生产快照。
- 生成研究隔离 instruments：`data/research/a_share_universe/20260930T-oos-instruments-v1/`。成员资格由 `listing_date <= 2026-09-30` 与 BaoStock `query_all_stock(day=2026-09-30)` 证明；当日股本使用截至 9 月 30 日可得的 PIT shares，动态涨跌停价清空。
- ST v3：`data/research/a_share_universe/20260930T-oos-st-history-v3/`；5,436 只全部 loaded、0 error，5,224 只当前上市 SH/SZ raw 均覆盖至 9 月 30 日，新增 4 个状态变化。
- 退市覆盖 v2：`data/research/a_share_universe/20260930T-oos-delisted-coverage-v2/`；212 只集合及关键字段零漂移，继续复用 adjusted-v2 bars。
- 首次信号运行发现 `301139.SZ 元道退`：BaoStock 在进入退市整理期时把 `isST` 从 1 清为 0，仅依靠 ST/正式退市日会错误放行。该包已归档到 `_superseded/as_of=2026-09-30-missing-delisting-period-v1/`。
- 新增显式 PIT exclusion history 支持与回归测试；事件包 `20260930T-oos-exclusion-history-v1/` 证明 `301139.SZ` 名称从 9 月 29 日 `*ST元道` 变为 9 月 30 日 `元道退`，从 9 月 30 日起退出 universe，不改写历史 ST 状态。
- 正式 observation：`data/research/a_share_oos/as_of=2026-09-30/`。raw/size-neutral 各 20 只、权重各 100%，18 只重合，Jaccard 0.8182，执行日固定为 2026-10-08；当前四账户仍为现金，等待执行日行情。
- 审计：信号中无 ST/*ST/退名称，`301139.SZ` 为 0 行，strict PIT fallback=0，输入 9 个/输出 7 个 SHA 全通过；相关回归 58 passed、Ruff 和 `git diff --check` 通过。

### Open Gates

1. 退市独立复权因子仍缺；未统一复权前不得把退市日 K 接入正式收益结论。
2. `ResearchUniverse.price_limit_pct` 尚未接入研究成交锁定判断；三策略正式入口也尚未接入 universe、delistings 和恢复率场景。
3. 北交所 347 只股票的 PIT ST 缺失；当前已明确 fail-closed，不会被当成非 ST。
4. E/P 与股息率继续等待可信 PIT 数据契约。
5. 月频反转与异常换手继续保持 `research_only`；五切片反转继续暂停。
