# Task Plan: A 股报告与 TSP 因子/策略对照

## Goal

截至 2026-09-28，逐项判断报告策略是否已在 TSP 中同口径实现，并为缺失项形成可执行、无未来函数的研究规格；本轮不新增生产策略或数据源。

## Next Step

读取完整历史验证产物 `data/research/a_share_strategy_validation/20260928T-full-history-v1/`，优先推进异常换手原始月频与月频反转的无偏股票池/历史 ST/基准对照；五切片反转暂停扩展。E/P、股息率继续等待数据契约。

## Current Phase

Phase 7

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
| 月频反转与异常换手仅判 internally stable | 成本/子时期/相邻参数方向稳定，但股票池、历史 ST、PIT 股本与基准比较尚未闭环 |

## Errors Encountered

| Error | Resolution |
|---|---|
| 原始 Documents 路径受 macOS 隐私权限限制 | 只读 Desktop 副本，不修改原文件 |
| `bs4` 未安装 | 使用标准库 `html.parser` |
| 系统 Python 无 `polars` | 使用 `uv run --project backend python` |
| agent-reach 文档中的 Exa server 不可用：`Unknown MCP server 'exa'` | 使用 agent-reach 可用网页/Jina 后端抓取论文和 MSCI 原文 |
