# 使用 DaoShare 补齐 TSP 历史股本

## 结论

**可以优先尝试使用 DaoShare 的 `daily_basic` 补齐。**

DaoShare 指南声明兼容 Tushare SDK/HTTP，并将 `daily_basic` 列入195个已支持接口。Tushare官方契约中，`daily_basic`按交易日提供：

| DaoShare/Tushare字段 | 含义 | 单位 | TSP目标字段 |
| --- | --- | --- | --- |
| `ts_code` | 股票代码 | — | `symbol` |
| `trade_date` | 交易日 | `YYYYMMDD` | `period_end`或新增`effective_date` |
| `total_share` | 总股本 | 万股 | `total_shares`，乘以10,000 |
| `float_share` | 流通股本 | 万股 | `float_shares`，乘以10,000 |
| `free_share` | 自由流通股本 | 万股 | 可选`free_shares`，乘以10,000 |
| `turnover_rate` | 换手率 | % | 仅用于验算，不作为股本事实源 |

## 为什么不是 `share_float`

`share_float`描述的是限售股解禁事件：

- `ann_date`：公告日期
- `float_date`：解禁日期
- `float_share`：该事件涉及的流通股份，单位股
- `holder_name`：股东名称
- `share_type`：股份类型

它不提供每个交易日的完整流通股本余额。直接累计事件容易遗漏IPO初始股本、送转、增发、回购、注销及供应商修订，因此不能替代`daily_basic.float_share`。

适合用途：

- 解释`daily_basic.float_share`的跳变；
- 对重要解禁日期做交叉检查；
- 研究限售解禁事件因子。

## TSP当前契约

TSP读取：

```text
data/financials/shares/part.parquet
```

当前最小必需字段为：

```text
symbol, period_end, float_shares
```

可选字段：

```text
announce_date, total_shares, free_shares, source
```

运行时逻辑使用`announce_date`（如有），否则回退到`period_end`作为可用日期，然后对行情按symbol做backward as-of join。

## 推荐落库模型

### 方案A：最小改动，沿用现有TSP契约

将每天的`daily_basic`转换为：

```text
symbol       = ts_code
period_end   = trade_date
announce_date= trade_date
float_shares = float_share * 10000
```

同时保存：

```text
total_shares = total_share * 10000
free_shares  = free_share * 10000
source       = "teajoin:daily_basic"
```

优点：无需修改读取逻辑。
缺点：`period_end`语义原本更像财务报告期，用于每日生效日并不理想。

### 方案B：长期更清晰

未来将股本表契约明确为：

```text
symbol, effective_date, announce_date,
total_shares, float_shares, free_shares, source
```

并让`share_capital.py`优先使用`effective_date`，兼容旧`period_end`。

这更符合“股本状态变更历史”，但属于业务契约修改，不应在第一次回填时顺手扩建。

**当前建议先采用方案A。**

## 是否保存每日记录

`daily_basic`会产生约“交易日 × 股票数”的数百万行。TSP只需要股本状态，因此可压缩为变更点：

1. 按`symbol, trade_date`排序；
2. 将`total_share/float_share/free_share`转为股数；
3. 与该股票上一交易日比较；
4. 只保留首条及任一股本字段发生变化的行；
5. 写入`financials/shares/part.parquet`。

注意：比较浮点数前应先四舍五入到整数股，避免供应商浮点噪声制造伪变更。

## Point-in-time口径

`daily_basic`是对应交易日的日终快照。为了避免未来数据：

- 交易日T的快照最早应在T日收盘后用于研究；
- 如果因子在T日收盘生成、T+1开盘执行，可将`announce_date/effective_date`设为T；
- 不要用最新股本倒填历史；
- 不要根据后续`share_float`公告反推公告前的股本状态；
- 缺失日期使用最近一个不晚于目标日期的历史状态，而不是向前填入未来记录。

## 获取策略

### 优先按交易日拉全市场

Tushare官方说明`daily_basic`单次最多返回约6000行，当前A股规模在该上限附近。建议：

```python
df = pro.daily_basic(
    trade_date="20260924",
    fields="ts_code,trade_date,total_share,float_share,free_share,turnover_rate",
)
```

优点：一次请求覆盖一个交易日，比逐股票请求更省额度。

### 请求节流

TeaJoin指南给出的边界：

- 最高450次/分钟；
- 批量请求建议至少间隔0.2秒；
- 使用重试、进度保存和断点续传；
- 不使用无限并发。

1466个交易日按每次0.2秒理论请求时间约5分钟，但实际应考虑网络、限流、空返回和重试，建议按月/年分段保存原始分区，再统一转换。

## 必做验证

### 字段与单位

抽查至少三只股票：

- 大盘股
- 次新股
- 历史送转/增发较多的股票

验证`float_share × 10000`与当前TSP instruments的`float_shares`数量级一致。

### 覆盖率

统计：

- 日期最小/最大值
- 唯一交易日数
- 唯一股票数
- 每日返回股票数分布
- `total_share/float_share/free_share`空值率
- 重复`(ts_code, trade_date)`键

### 业务约束

正常情况下：

```text
0 < free_shares <= float_shares <= total_shares
```

允许供应商个别异常，但必须输出异常清单，不应静默修正。

### 换手率交叉验证

TSP公式：

```text
turnover_rate(%) = volume(手) × 10000 / float_shares(股)
```

将回填股本计算出的换手率与`daily_basic.turnover_rate`比较。若出现系统性100倍误差，通常是“万股→股”转换遗漏或成交量单位误判。

## 与退市股覆盖的关系

要减少幸存者偏差，还需：

```python
pro.stock_basic(list_status="D")
```

获取退市股票及`list_date/delist_date`，然后确认`daily_basic`是否能返回其历史记录。只用默认`stock_basic()`会按`list_status='L'`返回当前上市股票，不能覆盖退市宇宙。

## 本次未执行

- 未兑换或读取真实API Key；
- 未对TeaJoin数据接口发起带token请求；
- 未创建`financials/shares/part.parquet`；
- 未修改TSP数据源配置或代码；
- 未重算enriched/换手率。

实际回填应先做3只股票或5个交易日的小样本dry-run，再决定是否全量拉取。
