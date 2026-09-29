# Task Plan: A 股报告与 TSP 因子/策略对照

## Goal

> 后续偏差修复与执行现实性升级的权威执行文档：[`research-data-bias-and-execution-realism-plan.md`](research-data-bias-and-execution-realism-plan.md)。该文档覆盖退市样本、历史 ST、严格 PIT 股本、指数基准、容量/冲击和排队代理的阶段计划及验收门槛。

截至 2026-09-28，逐项判断报告策略是否已在 TSP 中同口径实现，并为缺失项形成可执行、无未来函数的研究规格；本轮不新增生产策略或数据源。

## Next Step

Phase 0–4 已完成。两条异常换手升级为 `bias_reduced_candidate`，但继续 `research_only`，下一步仅进入人工评审或 paper-only 样本外观察；月频反转已暂停。Phase 5 继续受分钟/Level-2 数据门控。E/P、股息率继续等待可信 PIT 数据契约。

## Current Phase

Phase 10 in progress: paper-only OOS observer CLI; Phase 5 remains blocked by minute/Level-2 data

## Phases

### Phase 1: Requirements & Discovery
- [x] 阅读报告副本、项目贡献规范和二次开发文档
- [x] 明确同口径判定标准及研究边界
- [x] 盘点因子、策略、财务 PIT、股票池和挖掘结果
- **Status:** completed

### Phase 2: Evidence Research
- [x] 核对 E/P、壳股清洗、短期反转和异常换手的一手研究定义
- [x] 核对 MSCI 低波组合的历史暴露解释
- [x] 区分论文多空结果与个人可实现多头结果
- **Status:** completed

### Phase 3: Comparison & Research Specifications
- [x] 形成四级对照矩阵
- [x] 记录可复用能力与未解决偏差
- [x] 为五条缺失主线写出公式、频率、方向、宇宙和 PIT 要求
- **Status:** completed

### Phase 4: Verification
- [x] 复核关键代码定义与本地 parquet schema
- [x] 复核本地挖掘 IC/IR 和数据时间
- [x] 复核 git 状态，确保未覆盖用户已有修改
- **Status:** completed

### Phase 5: Delivery
- [x] 将结论写入 `findings.md`
- [x] 更新进度与限制
- [x] 向用户交付摘要和建议优先级
- **Status:** completed

### Phase 6: Research implementation
- [x] 实现 `short_term_reversal_20d` 的低收益分组、五切片权重和月末持有
- [x] 实现 `abnormal_turnover_1m_12m`，并与 `turnover_z_60d` 分开
- [x] 保持研究策略不进公开选股目录
- **Status:** completed

### Phase 7: Research target-weight backtester
- [x] 将完整目标、边界、阶段、测试和停止条件落盘
- [x] RED：恒定价格零成本账户测试
- [x] GREEN：最小目标权重撮合内核
- [x] 补齐成本、整手、停牌、一字板、统计测试
- [x] 接研究脚本并运行真实数据 smoke
- [x] 完整回归与文档收口
- **Status:** completed

### Phase 8: Full-history robustness validation
- [x] 扩展研究脚本为完整历史、五档成本、三个子时期和相邻参数敏感性
- [x] 运行 52 条独立账户路径并输出可复现产物
- [x] 校验 summary/daily/yearly 无重复且净值与收益均为有限值
- [x] 形成稳定性判断与停止规则结论
- **Status:** completed

### Phase 9: Bias repair and execution realism
- [x] 冻结快照、补齐基准并关闭 strict PIT 当前股本回退
- [x] 建立前复权退市行情、SH/SZ PIT ST、逐日 universe 和历史涨跌停
- [x] 重跑退市 100%/70%/0% 恢复率瀑布并暂停月频反转
- [x] 增加 t-1 ADV20 容量、平方根冲击、四档资金和三档 queue proxy
- [x] 完成完整 universe 的成本、子时期、参数、年度贡献和最终发布门
- **Status:** completed

### Phase 10: Paper-only OOS observer
- [x] 新增手动/外部调度共用的单次 CLI，不实现常驻调度器
- [x] 冻结 OOS 起点、20/250 窗口、Top 20、raw/size-neutral 和 primary/stress 四账户
- [x] 使用交易日历识别已完成月末，避免依赖未来一根行情
- [x] 每次从冻结点重放到 `--as-of`，输出不可覆盖 observation、manifest、信号、目标权重、账户、月度指标、组合重合度和质量审计
- [x] strict PIT 当前股本回退非零、数据 generation 漂移、输入覆盖不足或 observation 已存在时 fail-closed
- [x] 完成 RED/GREEN、历史边界 dry-run、Ruff 和回归验证
- [x] 完整日行情、同日 instruments、ST/退市覆盖更新后生成首个成功 observation
- [ ] 9 月 30 日收盘后生成首个月末信号，并在下一交易日开盘影子执行
- **Status:** implemented; first cash-only observation completed, first rebalance pending month-end

## Decisions Made

| Decision | Rationale |
|---|---|
| 不按策略名称判定已实现 | 必须同时核对公式、方向、频率、宇宙和成交假设 |
| 本轮不新增生产代码 | 用户目标是对照和研究，且 E/P、股息和无偏宇宙仍受数据阻塞 |
| `momentum_20d` 归为“因子存在、策略缺失” | 数值窗口相符，但现有 `n_day_low_reversal` 是另一类事件策略 |
| `turnover_z_60d` 仅归代理 | z-score 与 20/250 日均换手比值不是同一信号 |
| E/P 不用 `eps_basic` 临时代替 | 本地字段未证明是 PIT TTM EPS，直接使用会混淆报告期与未来函数 |
| 低波组合不等同 `low_volatility_leader` | 现有策略核心是正动量和均线过滤，且缺 E/P/股息/质量合成 |
| 小市值不直接发布 | 当前缺退市样本与历史 ST，且报告要求先剔除最小 30% 壳股 |
| 五切片反转暂停扩展 | 40 bps 后总收益转负、早期子时期为负、10 日参数方向翻转 |
| 月频反转停止 | 加入 PIT ST/退市后 base 为 -64.23%，70%/0% 恢复方向翻转，74 个退市持仓事件 |
| 两条异常换手升级为 bias_reduced_candidate | 通过退市/ST、40 bps、三个子时期、相邻参数、年度贡献和 1000 万最保守容量/冲击门，但仍保持 research_only |
| 日线执行只称 queue proxy | optimistic/base/conservative 是确定性日线边界，不是 Level-2 队列概率 |
| OOS observer 先做单次 CLI | 手动验证与未来外部调度复用同一条执行路径；脚本内部不常驻、不写 cron、不发真实订单 |
| OOS 每次从冻结点重放 | 当前日线规模下更简单、可复算，避免引入持久订单簿和增量状态迁移 |

## Errors Encountered

| Error | Resolution |
|---|---|
| 原始 Documents 路径受 macOS 隐私权限限制 | 只读 Desktop 副本，不修改原文件 |
| `bs4` 未安装 | 使用标准库 `html.parser` |
| 系统 Python 无 `polars` | 使用 `uv run --project backend python` |
| agent-reach 文档中的 Exa server 不可用：`Unknown MCP server 'exa'` | 使用 agent-reach 可用网页/Jina 后端抓取论文和 MSCI 原文 |
