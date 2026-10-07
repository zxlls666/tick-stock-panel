# ADR-004: paper-only 样本外观察与外部调度架构

- **状态：** 已采纳
- **日期：** 2026-10 (v0.2.3)
- **决策者：** Serge

## 上下文

- 背景：ADR-003 的两条月频异常换手候选（`abnormal_turnover_monthly_raw` / `size_neutral`）通过了偏差修复瀑布，但**样本内通过不等于可持续**；需要一套**前瞻（forward）纸面观察**路径，并为将来挂到外部调度（launchd/cron）预留唯一入口。
- 约束：
  - 不常驻、不下真实订单、不安装调度到脚本内部；
  - **手动运行与外部调度必须走完全相同的命令路径**（否则两套逻辑会漂移）；
  - 上游数据（日线/instruments/ST/退市/PIT 股本/交易日历）可能不同步或缺失，观察不得基于半成品；
  - 同一天重复调度必须安全（幂等），且不得覆盖历史观察。
- 可选方案：
  1. 常驻服务/内部常开调度器；
  2. 脚本内部直接写 cron 触发自己；
  3. **单次一次性 CLI + 外部调度只编排**；
  4. 每次增量持久状态，避免全量重放。

## 决策

- **Phase 10 —— 研究候选的 paper-only OOS observer**（`backend/scripts/run_a_share_oos_observer.py`）：
  - 单次 CLI，手动与外部调度共用同一路径；冻结起点 `2026-09-25`，**每次从冻结点重放**到 `--as-of`；20/250 异常换手、Top 20、退市 70% 恢复、四个独立影子账户（raw/size-neutral × primary/stress）。
  - 输出**不可覆盖**的 `as_of=` observation：`signals / target_weights / daily_accounts / summary / monthly_metrics / signal_overlap / data_quality / manifest.json / report.md`；输入输出全部 SHA-256。
  - fail-closed 门：数据 generation 漂移、行情末日 ≠ `as-of`、末日覆盖低于近 20 日中位数、instruments 快照日期不一致、ST/退市/PIT 股本 coverage 不足、strict PIT 当前股本回退非零、交易日历不完整；**尚无目标前不把现金相对基准的收益报告为 alpha**。
- **内置策略 `n_day_low_reversal` 进入 paper**（`backend/scripts/run_nday_low_paper.py`）：冻结 `2026-10-01`、**固定默认参数**（禁止逐折调参）、`open_t+1` + 佣金/印花税/滑点 + 涨跌停阻塞；逐自然月落脚 + regime 归因；**预注册停止条件**（连续 2 个自然月负收益 / 滚动 126 日回撤 ≤ 历史 OOS 上限×1.5 / 实际胜率 < 40%（需 ≥30 笔）/ 累计收益 ≤ -20%）。
- **Phase 11 —— 唯一外部调度入口**（`backend/scripts/run_research_scheduler.py`）：只编排既有命令
  `sync 日线 → sync instruments → 构建 ST → 退市覆盖证明 → preflight → observer`；同 `as-of` **严格串行**，任一步失败立即非零退出、observer 不运行、不留半成品；`as_of=` 已存在视为**幂等成功**；逐 run 记录 `scheduler.jsonl` 与各步日志；preflight 由 `app/backtest/research_preflight.py` 纯校验函数承担。
- 外部调度以 **launchd 模板** 形式提供（`packaging/launchd/`），**复核通过前不安装**。

## 理由

- 单次 CLI 让手动验证与自动调度零漂移，且不引入常驻进程与第二套事实源。
- 从冻结点重放在当前日线规模下最简单、可复算，避免增量状态的迁移与漂移。
- fail-closed 门 + 不可覆盖 observation 保证观察不被半成品或重复运行污染。
- 幂等短路使同日重复调度安全，符合"失败不覆盖、成功不重复记账"。

## 后果

- 正面：观察路径可复现、可审计（manifest 哈希）、可挂外部调度；研究候选与内置策略共用同一套 paper 纪律。
- 负面 / 边界：
  - 回放是"影子成交"，不含排队/冲击的真实微观过程（日线 proxy）；
  - `_superseded` 归档依赖人工纪律；调度只备了 macOS launchd 模板，尚未安装；
  - 上游未原子对齐时 observer 会拒绝运行（这是有意为之，但需要运维按序执行）。
- 需要后续验证：2026-10-08 首次影子成交人工复核 → 手动执行调度入口 ≥2 个交易日（成功/失败/重复）→ 才 `launchctl bootstrap`。

## 替代方案否决理由

- **常驻服务 / 内部常开调度器**：不需要，且引入长驻状态与故障面；paper 阶段用一次性 CLI 足够。
- **脚本内部写 cron**：造成调度逻辑的第二处真相；外部调度应只调用同一 CLI。
- **每次增量持久状态**：在当前规模下复杂度高、易漂移；全量重放更可复算。
- **直接进入实盘/生产**：抗不过 ADR-003 的偏差与失效条件，且未经过前瞻验证。

## 相关记录

- 调度入口与 preflight：`backend/scripts/run_research_scheduler.py`、`app/backtest/research_preflight.py`
- 观察器：`backend/scripts/run_a_share_oos_observer.py`、`backend/scripts/run_nday_low_paper.py`
- launchd 模板与装卸说明：`packaging/launchd/README.md`
- 首次月末信号取证与 Phase 11 计划：`.planning/2026-09-28-a/{findings,progress,task_plan}.md`
