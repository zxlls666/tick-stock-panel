# 基本面基线因子 Phase 2（2026-09-27）

> 只覆盖 Phase 1 选定的 8 个因子。未发布策略，未采购分钟或 Level-2 数据。
> 行情世代：`78ff1e8ffb6c48ccb0ebacd0ac780718`。区间：`2021-01-04 .. 2026-09-24`。
> 退市、历史 ST、行业 PIT、财报修订版本仍未关闭。下面的数字不能写成无偏 Alpha。

## 1. 登记

7 个财务因子原本就在目录里。本次只追加一个，并给规模因子补上股本 PIT 来源。目录顺序的前 48 项没有改，挖掘调度前缀不变。内置因子从 77 变为 78。

| 因子 | 公式 | 依赖 | PIT | 预热 | 目录方向 |
| --- | --- | --- | --- | --- | --- |
| `log_float_mv` | `ln(收盘价 × 成交量 / 换手率)`。换手率缺失或非正时为空 | `close`, `volume`, `turnover_rate` | `share_capital_announce` | 1 | 不预填。挖掘按训练折重估 |
| `roe_latest` | `metrics.roe`，百分数 | 财务快照 | `financial_announce` | 1 | 同上 |
| `debt_ratio_latest` | `metrics.debt_to_asset_ratio`，百分数 | 财务快照 | `financial_announce` | 1 | 同上 |
| `revenue_yoy_latest` | `metrics.revenue_yoy`，百分数 | 财务快照 | `financial_announce` | 1 | 同上 |
| `net_income_yoy_latest` | `metrics.net_income_yoy`，百分数 | 财务快照 | `financial_announce` | 1 | 同上 |
| `gross_margin_latest` | `metrics.gross_margin`，百分数 | 财务快照 | `financial_announce` | 1 | 同上 |
| `net_margin_latest` | `metrics.net_margin`，百分数 | 财务快照 | `financial_announce` | 1 | 同上 |
| `ocf_to_ni_latest` | `metrics.net_profit_cash_content`，百分数，不除以 100 | 财务快照 | `financial_announce` | 1 | 同上 |

财务因子只在严格晚于生效公告日的交易日有值。空值保持空，不填 0。真实 0（例如资产负债率 0）会保留。

最小测试在 `backend/tests/test_fundamental_factors.py`。相关注册表、因子库和矩阵一致性测试共 82 项通过。

## 2. 月频检验

调用 `FactorBacktestService.run`。月频调仓，5 组，等权。Rank IC 对的是下一持有期收益，所以每年大约 12 个 IC 点，不是 240 个日 IC。衰减列另算 1/3/5 个交易日的日频 Rank IC。环境用 T-1 三档。分组收益含往返成本：佣金万二双边、滑点 5bp 双边、印花税 0，合计约 14bp。多空是理论价差，`executable_short=false`，不能当成可卖空收益。

Q1 是因子值最低的一组，Q5 是最高的一组。

L1 门槛来自 `auto_mining.SCREEN_GATES`。q 值只在这 8 个因子内部做 BH-FDR，不是全目录。

| 因子 | 月频 IC | IR | NW t | q | 覆盖率 | 均衡档 | 严格档 |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `log_float_mv` | -0.0428 | -0.2608 | -2.25 | 0.097 | 1.00 | 稳定度低（\|IR\|<0.30） | 稳定度低（\|IR\|<0.50） |
| `ocf_to_ni_latest` | 0.0133 | 0.2971 | 2.53 | 0.091 | 0.83 | 预测力弱（\|IC\|<0.02） | 预测力弱（\|IC\|<0.03） |
| `roe_latest` | -0.0144 | -0.1021 | -0.75 | 0.90 | 0.83 | 预测力弱 | 预测力弱 |
| `gross_margin_latest` | -0.0112 | -0.1243 | -1.09 | 0.74 | 0.81 | 预测力弱 | 预测力弱 |
| `net_margin_latest` | -0.0084 | -0.0733 | -0.55 | 0.94 | 0.83 | 预测力弱 | 预测力弱 |
| `revenue_yoy_latest` | -0.0010 | -0.0104 | -0.08 | 0.99 | 0.83 | 预测力弱 | 预测力弱 |
| `debt_ratio_latest` | -0.0003 | -0.0032 | -0.03 | 0.99 | 0.83 | 预测力弱 | 预测力弱 |
| `net_income_yoy_latest` | 0.0002 | 0.0019 | 0.01 | 0.99 | 0.83 | 预测力弱 | 预测力弱 |

8 个都没有过 L1。`log_float_mv` 的 \|IC\| 过了 0.02，但 IR 不够，而且 2024 年 IC 转正（0.0186），不是年年同号。财务因子里 `ocf_to_ni_latest` 的 t 最大，IC 仍低于 0.02。2021 年财务因子只有 2 个 IC 月，不能单独解释。

`log_float_mv` 分组：Q1 累计收益 1.62、Q5 0.06。低市值组更高，和负 IC 一致。财务因子的五组差距小，`debt_ratio_latest` 五组累计收益都在 0.46～0.55。`ocf_to_ni_latest` 的 Q5（高现金含量）累计 0.70，Q1 0.58，中间组更低，不是单调阶梯。

日频衰减的绝对值和月频 IC 同量级或更小。环境分组的 IC 点数很少（震荡档只有 5～6 个月），不能拿来做环境切换。

## 3. 嵌套样本外

用现有挖掘运行时，只放这 8 个因子，`max_combination_factors=1`，所以候选是单因子而不是新组合。费用是挖掘请求口径：佣金万二、卖出印花税万五、滑点 5bp。前瞻由引擎按 5 日处理。训练折自己决定方向，和月频 IC 的符号不必相同。

| 档 | run id | 状态 | 有效 outer 折 | 跳过 | 候选 |
| --- | --- | --- | --- | ---: | ---: |
| balanced | `df78d23d836c466291a0d7b974c246c8` | `succeeded_with_budget_exhausted` | 12 | 0 个选中折 | 3 |
| strict | `b8f31fc809f94542b5d27579b4424d76` | `succeeded` | 4 | 0 | 3 |

结果在 `data` 下对应挖掘 run 目录。没有调用发布。

均衡档真实试验预算用尽，跨折补评有 22 行原因是 `real trial budget exhausted before cross-fold evaluation`。这一档的样本外均值不完整。

严格档四个 outer 折都评完了。按 `evaluate_candidate_gate`：

| 档 | 候选 | 样本外夏普 | 正收益折 | 最大回撤 | 交易数 | 发布门槛 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| balanced | `net_income_yoy_latest` 方向 -1 | 0.05 | 0.50 | -40.9% | 177 | 未过：夏普、正收益折、回撤。且预算未评完 |
| balanced | `log_float_mv` 方向 -1 | -0.03 | 0.57 | -40.9% | 435 | 未过：夏普、正收益折、回撤 |
| balanced | `gross_margin_latest` 方向 -1 | -0.52 | 0.00 | -19.5% | 145 | 未过：夏普、正收益折 |
| strict | `ocf_to_ni_latest` 方向 +1 | 1.45 | 1.00 | -12.8% | 106 | 数字上达到门槛 |
| strict | `net_income_yoy_latest` 方向 -1 | 0.46 | 0.75 | -22.2% | 147 | 未过：夏普 0.46 < 0.50 |
| strict | `log_float_mv` 方向 -1 | -0.08 | 0.50 | -32.0% | 254 | 未过：夏普、正收益折、回撤 |

其余因子没有在任何 outer 折胜出。严格档里 `net_margin_latest` 的记录原因是 `not_selected`。

`ocf_to_ni_latest` 是这 8 个里唯一走完「定义 → PIT 计算 → 月频检验 → 严格档嵌套样本外」的因子。严格档合计夏普 1.45 来自 4 个 outer 折、合计 106 笔交易。同一因子的月频 Rank IC 只有 0.013，L1 判定预测力弱。两套口径不一致，不能把发布门槛数字当成已经证实的基本面 Alpha。

## 4. 停止点

- 不发布、不把 `ocf_to_ni_latest` 放进自动挖掘的默认前 48 个因子。
- 不据此做质量 × 估值或质量 × 动量。`pb_latest` 仍没有历史 `bps`。
- Phase 3 日频资金异动雷达还没开始。
