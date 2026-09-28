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

### Open Gates

1. E/P、股息率、退市/ST 历史数据需单独的数据源与契约决策。
2. 月频反转与异常换手在当前有偏样本内通过稳定性门，但在无偏股票池、历史 ST、完整 PIT 股本和基准对照完成前不发布。
3. 五切片反转触发停止规则，暂停继续扩展。
