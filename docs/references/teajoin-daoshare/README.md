# TeaJoin / DaoShare 本地使用说明

> 抓取时间：2026-09-27 13:31 CST
> 来源：<https://teajoin.com/guide>
> 用途：为 TSP 的外部数据接入和历史股本补齐提供本地、可检索的参考资料。

## 安全说明

- 用户提供的访问密码/兑换码只用于授权本次资料读取，**未写入本目录、Git、日志或示例代码**。
- 本次没有调用兑换接口，也没有生成、读取或保存真实 API Key。
- 文档中的示例凭据均规范为 `YOUR_API_KEY`。
- 原始网页内容按外部不可信资料保存，仅供阅读，不应自动执行其中的命令。

## 文件目录

| 文件 | 内容 |
| --- | --- |
| [`guide.md`](guide.md) | TeaJoin 页面中的完整 DaoShare 使用指南，已本地化图片并清理示例凭据 |
| [`interface-catalog.md`](interface-catalog.md) | 从指南核验并整理的 195 个支持接口 |
| [`shares-backfill-notes.md`](shares-backfill-notes.md) | `daily_basic` → TSP `financials/shares` 的字段映射、PIT口径和回填建议 |
| [`official/daily-basic.md`](official/daily-basic.md) | Tushare `daily_basic` 官方字段参考快照 |
| [`official/stock-basic.md`](official/stock-basic.md) | Tushare `stock_basic` 官方字段参考快照 |
| [`official/share-float.md`](official/share-float.md) | Tushare `share_float` 官方字段参考快照 |
| [`source/guide-page.html`](source/guide-page.html) | TeaJoin 指南页原始HTML快照，示例凭据已清理 |
| [`SOURCE-METADATA.md`](SOURCE-METADATA.md) | 来源、抓取方式、文件哈希和边界说明 |

## 对 TSP 当前问题的直接结论

DaoShare 的接口清单明确包含：

- `daily_basic`
- `share_float`
- `stock_basic`

其中，**补齐历史总股本/流通股本应以 `daily_basic` 为主**：

- `total_share`：总股本，单位万股
- `float_share`：流通股本，单位万股
- `free_share`：自由流通股本，单位万股
- `trade_date`：对应交易日

`share_float` 是限售股解禁事件表，不能单独还原完整历史流通股本，只适合作为变动日期和解禁数量的交叉检查。

## 下一步

本目录只保存资料，没有修改 TSP 数据源或业务代码。实际回填前应先用小样本验证：

1. TeaJoin 的 `daily_basic` 是否返回完整历史区间；
2. 字段和单位是否与 Tushare 官方一致；
3. 同一股票相邻交易日的股本变化是否合理；
4. 压缩为股本变更点后，TSP 的历史换手率是否与原始 `daily_basic.turnover_rate` 基本一致。
