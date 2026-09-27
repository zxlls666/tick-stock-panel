---
title: DaoShare / Tushare Pro 数据接口使用指南
source: https://teajoin.com/guide
captured_at: 2026-09-27T13:31:00+08:00
credential_saved: false
---

> **⚠️ 免责声明：本文档仅供学习研究，数据不构成任何投资建议，请勿据此作出投资决策。**

# Tushare Pro 数据接口使用指南

## 使用前必读

- 本服务兼容 Tushare SDK 与 HTTP API；将请求地址改为 `https://teajoin.com` 后即可使用。
- 15000 积分通用套餐覆盖常用股票、ETF、指数、基金、期货、期权、财务、资金流及特色数据接口；具体以已开通套餐和卖家提供的接口清单为准。
- 历史分钟与实时行情属于可单独开通的权限，通用 15000 积分套餐默认不包含。
- 最高频率为 **450 次/分钟**。批量请求建议间隔至少 **0.2 秒**，不要用无限并发方式下载全市场数据。
- 返回格式与 Tushare Pro 保持一致：`data.fields` 为字段名，`data.items` 为数据行。无数据时会正常返回空列表，不代表程序出错。

## 1. 获取 API Key

打开卖家发给你的兑换页面：

```
https://teajoin.com/redeem
```

输入兑换码，即可获得你的专属 API Key 和到期时间。**兑换码可重复使用**，随时回来输入同一兑换码即可重新查看 Key。

返回示例：
```json
{"api_key": "YOUR_API_KEY", "expires_at": "2026-06-08 17:47:34"}
```

请妥善保存 API Key。如忘记，重新输入兑换码即可找回。

## 2. 安装依赖

```bash
pip install tushare pandas
```

## 3. 调用方式

### 方式一：使用 Ts SDK（推荐，最简单）

如果你已经在用 `tushare` Python 包，只需改两行代码。**如果你之前设置过 token 环境变量，记得要删除。**

```python
import tushare as ts

# 把 token 设为兑换获得的 API Key
token = "YOUR_API_KEY"
ts.set_token(token)
pro = ts.pro_api()
pro._DataApi_token = token
pro._DataApi__http_url = "https://teajoin.com"

# 然后正常使用
df = pro.daily(ts_code='000001.SZ', start_date='20260101', end_date='20260110')
print(df)
print(f'[SDK] 行数: {len(df)}')
```

**特殊情形：`ts.pro_bar()` 等模块级函数，必须手动传 `api=pro`**

```python
import tushare as ts

token = "YOUR_API_KEY"
ts.set_token(token)
pro = ts.pro_api()
pro._DataApi_token = token
pro._DataApi__http_url = "https://teajoin.com"

df = ts.pro_bar(ts_code='002594.SZ', api=pro, start_date='20180101', end_date='20181011', adj='qfq')
print(df)
```

### 方式二：直接 HTTP 请求

如果不方便用 ts SDK，可以直接 POST 到根路径 `/`：

```python
import requests

resp = requests.post("https://teajoin.com", json={
    "api_name": "daily",
    "token": "YOUR_API_KEY",
    "params": {
        "ts_code": "000001.SZ",
        "start_date": "20260101",
        "end_date": "20260110"
    }
})

data = resp.json()
print(data)
```

### 批量下载建议

请尽量按日期或代码分批请求，不要瞬间并发提交大量任务。下面是一个带间隔、重试和断点落盘的日线下载示例：

```python
import time
from pathlib import Path

import pandas as pd
import tushare as ts

API_KEY = "YOUR_API_KEY"
OUTPUT = Path("daily_prices.csv")
CODES = ["000001.SZ", "000002.SZ", "600519.SH"]

ts.set_token(API_KEY)
pro = ts.pro_api()
pro._DataApi_token = API_KEY
pro._DataApi__http_url = "https://teajoin.com"

frames = []
for index, ts_code in enumerate(CODES, start=1):
    for attempt in range(3):
        try:
            df = pro.daily(ts_code=ts_code, start_date="20260101", end_date="20260131")
            frames.append(df)
            print(f"[{index}/{len(CODES)}] {ts_code}: {len(df)} 行")
            break
        except Exception as exc:
            if attempt == 2:
                print(f"跳过 {ts_code}: {exc}")
            else:
                time.sleep(2 * (attempt + 1))
    time.sleep(0.2)  # 建议的最小请求间隔

result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
result.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
print(f"已保存 {len(result)} 行到 {OUTPUT.resolve()}")
```

常用技巧：

- 使用 `fields="ts_code,trade_date,close,vol"` 只取需要的列，可减少传输和处理量。
- 需要大量历史数据时，优先按交易日、月份或股票列表分批保存，程序中断后可从已保存文件继续。
- 部分接口支持 `limit` 与 `offset`；需要分页时请以该接口的 Tushare 文档参数为准。

### 方式三：MCP 协议（接入 AI 大模型）

将 ts 数据接入 Claude、Cursor、Codex 等支持 MCP 的 AI 工具，让大模型直接调用数据。

在你的 AI 工具 MCP 配置文件中添加：

```json
{
  "mcpServers": {
    "tushare": {
      "url": "https://teajoin.com/mcp/?api_key=YOUR_API_KEY"
    }
  }
}
```

配好后在 AI 对话里说"查一下000001.SZ最近一周的日线行情"，AI 自动调接口返回数据。

### 方式四：让 AI 帮你写量化代码（推荐）

将下面整段提示词连同你的数据需求一起发给 Codex、Claude、Cursor 等 AI。AI 会按本服务的 SDK 配置、权限范围与调用频率生成代码。**不要把真实 API Key 发给陌生人或公开贴出**；AI 生成的代码中请自行填入你的 Key。

```text
# 角色设定
你是一名精通 Tushare Pro 数据接口的 Python 量化开发工程师。请使用 Tushare SDK 编写可直接运行的数据获取、清洗与分析代码。

# 我的数据权限与调用规则
- Tushare Pro：15000 积分通用数据权限
- 调用频率：最高 450 次/分钟
- 建议请求间隔：每次至少 0.2 秒；批量任务请加入 sleep、重试和进度保存
- 可访问：常规积分接口与已开通的特色数据权限，例如日/周/月线、财务数据、资金流、龙虎榜、涨跌停、同花顺热点、筹码分布、量化因子、券商金股等
- 不包含：实时行情、新闻公告、研报、港美股等独立权限接口
- 数据请求应优先按接口文档传参；不要假设未开通的权限可用

# 代码要求
1. 必须使用 tushare SDK，不要改为其他数据源。
2. 所有代码均需兼容本代理服务：设置 API Key 后，将 SDK 请求地址指向 `https://teajoin.com`。
3. 代码应包含依赖安装说明、合理的 0.2 秒请求间隔、批量请求的异常处理/重试/进度保存，以及必要的数据清洗说明。
4. 返回完整、可复制运行的 Python 代码，并说明如何修改股票代码、日期和输出文件路径。
5. 若调用 `ts.pro_bar()` 等模块级函数，必须传入 `api=pro`。
6. 不要输出、记录或要求我公开真实 API Key；直接使用代码中已填入的 Key。

# Tushare SDK 接入模板
import tushare as ts

API_KEY = "YOUR_API_KEY"
ts.set_token(API_KEY)
pro = ts.pro_api()
pro._DataApi_token = API_KEY
pro._DataApi__http_url = "https://teajoin.com"

df = pro.daily(ts_code="000001.SZ", start_date="20260101", end_date="20260110")
print(df)

# 特殊情形：ts.pro_bar 等模块级函数
df = ts.pro_bar(
    ts_code="002594.SZ", api=pro,
    start_date="20180101", end_date="20181011", adj="qfq"
)
print(df)

# 记忆要求
请在本次对话中记住：我使用的是 15000 积分通用权限、最高 450 次/分钟、建议间隔 0.2 秒，并通过 `https://teajoin.com` 使用 Tushare SDK 代理。后续我直接提出数据需求时，默认按以上配置生成代码。

# 配置完成提醒
完成上述配置并生成可运行代码后，请提醒我：**重启当前 Agent（Codex / Claude / Cursor 等），让记忆设置生效**。重启后我再提出数据需求时，你会默认按本配置生成代码。
```


## 4. 常用接口速查

以下是最常用的接口和参数。日期统一使用 `YYYYMMDD`，股票代码使用 `000001.SZ`、`600519.SH` 这类 Tushare 格式。

| 接口名 | 用途 | 常用参数 |
|--------|------|---------|
| `daily` | 日线行情 | ts_code, trade_date, start_date, end_date |
| `weekly` | 周线行情 | ts_code, start_date, end_date |
| `monthly` | 月线行情 | ts_code, start_date, end_date |
| `daily_basic` | 每日指标 | ts_code, trade_date |
| `stock_basic` | 股票列表 | exchange, list_status |
| `trade_cal` | 交易日历 | exchange, start_date, end_date |
| `us_tradecal` | 美股交易日历 | start_date, end_date |
| `income` | 利润表 | ts_code, start_date, end_date |
| `balancesheet` | 资产负债表 | ts_code, start_date, end_date |
| `cashflow` | 现金流量表 | ts_code, start_date, end_date |
| `index_daily` | 指数日线 | ts_code, start_date, end_date |
| `limit_list_d` | 涨跌停列表 | trade_date, ts_code |
| `moneyflow` | 个股资金流向 | ts_code, trade_date |
| `stk_limit` | 涨跌停价格 | ts_code, trade_date |
| `ths_hot` | 同花顺热点 | trade_date, ts_code |
| `broker_recommend` | 券商金股 | month |
| `cyq_chips` | 筹码分布（**数据截至 2026-06-25，已停更**，历史仍可查） | ts_code, trade_date |
| `stk_factor_pro` | 专业量化因子 | ts_code 或 trade_date |

### 已支持接口目录

以下为当前服务已支持的 **195 个**接口名称，可直接用 `pro.接口名(...)` 调用；例如 `pro.moneyflow(...)`。其中带 `_vip` 的接口是对应财务接口的 VIP 版本。不同接口的必填参数不同，使用前可让 AI 或售后按接口名称确认参数。

**股票基础与行情**

`stock_basic`、`trade_cal`、`hk_tradecal`、`us_tradecal`、`namechange`、`stk_rewards`、`bak_basic`、`stock_st`、`st`、`stock_hsgt`、`bse_mapping`、`daily`、`weekly`、`monthly`、`stk_weekly_monthly`、`stk_week_month_adj`、`adj_factor`、`suspend_d`、`daily_basic`、`stk_limit`、`moneyflow`、`moneyflow_hsgt`、`hsgt_top10`、`ggt_top10`、`hk_hold`、`bak_daily`

**财务、公司与市场参考**

`income`、`income_vip`、`balancesheet`、`balancesheet_vip`、`cashflow`、`cashflow_vip`、`forecast`、`forecast_vip`、`express`、`express_vip`、`dividend`、`fina_indicator`、`fina_indicator_vip`、`fina_audit`、`fina_mainbz`、`fina_mainbz_vip`、`disclosure_date`、`top10_holders`、`top10_floatholders`、`top_inst`、`pledge_stat`、`pledge_detail`、`repurchase`、`share_float`、`block_trade`、`stk_holdernumber`、`stk_holdertrade`、`broker_recommend`

**资金流、打板与题材数据**

`margin`、`margin_detail`、`moneyflow_ths`、`moneyflow_dc`、`moneyflow_ind_ths`、`moneyflow_ind_dc`、`moneyflow_cnt_ths`、`moneyflow_mkt_dc`、`limit_list_d`、`limit_list_ths`、`limit_cpt_list`、`limit_step`、`top_list`、`kpl_list`、`kpl_concept_cons`、`ths_hot`、`dc_hot`、`dc_index`、`dc_member`、`dc_daily`、`dc_concept`、`dc_concept_cons`、`tdx_index`、`tdx_member`、`tdx_daily`

**同花顺、游资、筹码与量化特色数据**

`ths_daily`、`ths_index`、`ths_member`、`hm_list`、`hm_detail`、`cyq_perf`、`cyq_chips`、`stk_factor`、`stk_factor_pro`、`stk_surv`、`stk_nineturn`、`stk_auction`、`stk_auction_o`、`stk_auction_c`、`report_rc`
> ⚠ **`cyq_chips`（每日筹码分布）数据源已停更，数据截至 2026-06-25**；历史数据仍可正常查询。`cyq_perf`（每日筹码及胜率）不受影响，仍在每日更新。


**指数、基金与 ETF**

`index_basic`、`index_daily`、`index_weekly`、`index_monthly`、`index_weight`、`index_dailybasic`、`index_classify`、`index_member_all`、`sw_daily`、`ci_index_member`、`fund_basic`、`fund_company`、`fund_manager`、`fund_nav`、`fund_daily`、`fund_adj`、`fund_div`、`fund_portfolio`、`fund_share`、`etf_basic`、`etf_index`、`etf_share_size`

**期货、期权、可转债、债券与宏观**

`fut_basic`、`fut_daily`、`fut_holding`、`fut_wsr`、`fut_settle`、`fut_mapping`、`fut_index_daily`、`fut_trade_cal`、`fut_weekly_monthly`、`fut_weekly_detail`、`opt_basic`、`ft_limit`、`opt_daily`、`cb_basic`、`cb_daily`、`cb_call`、`cb_issue`、`cb_rate`、`cb_rating`、`cb_share`、`cb_factor_pro`、`top10_cb_holders`、`bond_blk`、`bond_blk_detail`、`bc_otcqt`、`bc_bestotcqt`、`hk_basic`、`us_basic`、`us_tbr`、`us_tltr`、`us_trltr`、`us_tycr`、`hk_basic`、`cn_gdp`、`cn_cpi`、`cn_ppi`、`cn_pmi`、`cn_m`、`cn_schedule`、`shibor`、`shibor_quote`、`shibor_lpr`、`hibor`、`libor`、`eco_cal`、`repo_daily`、`sge_basic`、`sge_daily`、`mkt_idx_bmk`、`gz_index`、`wz_index`、`new_share`、`stk_account`、`slb_len`、`slb_len_mm`、`slb_sec`、`slb_sec_detail`、`ci_daily`、`idx_anns`、`idx_factor_pro`、`fund_factor_pro`

**已支持的完整 catalog 接口名称（195 个）**

`adj_factor`、`bak_basic`、`bak_daily`、`balancesheet`、`balancesheet_vip`、`bc_bestotcqt`、`bc_otcqt`、`block_trade`、`bond_blk`、`bond_blk_detail`、`broker_recommend`、`bse_mapping`、`cashflow`、`cashflow_vip`、`cb_basic`、`cb_call`、`cb_daily`、`cb_factor_pro`、`cb_issue`、`cb_rate`、`cb_rating`、`cb_share`、`ci_daily`、`ci_index_member`、`cn_cpi`、`cn_gdp`、`cn_m`、`cn_pmi`、`cn_ppi`、`cn_schedule`、`cyq_chips`、`cyq_perf`、`daily`、`daily_basic`、`daily_info`、`dc_concept`、`dc_concept_cons`、`dc_daily`、`dc_hot`、`dc_index`、`dc_member`、`disclosure_date`、`dividend`、`eco_cal`、`etf_basic`、`etf_index`、`etf_mins`、`etf_sh_cons`、`etf_share_size`、`etf_sz_cons`、`express`、`express_vip`、`fina_audit`、`fina_indicator`、`fina_indicator_vip`、`fina_mainbz`、`fina_mainbz_vip`、`forecast`、`forecast_vip`、`ft_limit`、`fund_adj`、`fund_basic`、`fund_company`、`fund_daily`、`fund_div`、`fund_factor_pro`、`fund_manager`、`fund_nav`、`fund_portfolio`、`fund_share`、`fut_basic`、`fut_daily`、`fut_holding`、`fut_index_daily`、`fut_mapping`、`fut_settle`、`fut_trade_cal`、`fut_weekly_detail`、`fut_weekly_monthly`、`fut_wsr`、`fx_daily`、`fx_obasic`、`ggt_daily`、`ggt_top10`、`gz_index`、`hibor`、`hk_basic`、`hk_hold`、`hk_tradecal`、`hm_detail`、`hm_list`、`hsgt_top10`、`idx_anns`、`idx_factor_pro`、`income`、`income_vip`、`index_basic`、`index_classify`、`index_daily`、`index_dailybasic`、`index_global`、`index_member_all`、`index_monthly`、`index_weekly`、`index_weight`、`kpl_concept_cons`、`kpl_list`、`libor`、`limit_cpt_list`、`limit_list_d`、`limit_list_ths`、`limit_step`、`margin`、`margin_detail`、`margin_secs`、`mkt_idx_bmk`、`moneyflow`、`moneyflow_cnt_ths`、`moneyflow_dc`、`moneyflow_hsgt`、`moneyflow_ind_dc`、`moneyflow_ind_ths`、`moneyflow_mkt_dc`、`moneyflow_ths`、`monthly`、`namechange`、`new_share`、`opt_basic`、`opt_daily`、`pledge_detail`、`pledge_stat`、`repo_daily`、`report_rc`、`repurchase`、`sf_month`、`sge_basic`、`sge_daily`、`share_float`、`shibor`、`shibor_lpr`、`shibor_quote`、`slb_len`、`slb_len_mm`、`slb_sec`、`slb_sec_detail`、`st`、`stk_account`、`stk_ah_comparison`、`stk_alert`、`stk_auction`、`stk_auction_c`、`stk_auction_o`、`stk_factor`、`stk_factor_pro`、`stk_high_shock`、`stk_holdernumber`、`stk_holdertrade`、`stk_limit`、`stk_managers`、`stk_mins`、`stk_nineturn`、`stk_rewards`、`stk_shock`、`stk_surv`、`stk_week_month_adj`、`stk_weekly_monthly`、`stock_basic`、`stock_company`、`stock_hsgt`、`stock_st`、`suspend_d`、`sw_daily`、`sz_daily_info`、`tdx_daily`、`tdx_index`、`tdx_member`、`ths_daily`、`ths_hot`、`ths_index`、`ths_member`、`top10_cb_holders`、`top10_floatholders`、`top10_holders`、`top_inst`、`top_list`、`trade_cal`、`us_basic`、`us_tbr`、`us_tltr`、`us_tradecal`、`us_trltr`、`us_trycr`、`us_tycr`、`weekly`、`wz_index`

**已支持的独立权限接口**
（历史分钟行情）需下单历史分钟权限
`stk_mins`、`etf_mins`；

**（集合竞价）所有套餐均已包含无需额外开通。**

**早盘请求这个，9点26分后有数据，如果没拿到数据间隔20秒再试**

`stk_auction`、

**盘后请求这两个：**

`stk_auction_c`、`stk_auction_o`

**需要注意**：实时行情、新闻公告、研报、港美股等独立权限接口，不在通用 15000 积分套餐默认范围内；如需使用，请先咨询售后开通情况。


## 5. 常见问题

**Q: 数据格式和官方 Tushare 一样吗？**
A: 一致。响应使用 Tushare 标准 JSON 结构，SDK 会自动转换为 DataFrame，字段名和数据行格式保持兼容。

**Q: 支持哪些接口？**
A: 支持已开通套餐范围内的 Tushare Pro 接口。15000 积分常规接口与部分特色数据可用；独立付费接口、历史分钟和实时行情需要单独开通。具体接口可咨询售后确认。

**Q: 和直接用 Ts 有什么区别？**
A: 返回数据格式完全一致。你只需把 `_DataApi__http_url` 指向本服务地址，Token 设为你的 API Key 即可，代码几乎不用改。

**Q: 到期怎么办？**
A: 访问 /redeem 输入兑换码即可查看到期时间。如已到期，联系卖家购买新兑换码续费。

**Q: 怎么知道自己的到期时间？**
A: 访问 /redeem 输入兑换码即可查看。

**Q: 为什么返回空数据？**
A: 先检查股票代码、日期、接口名和必填参数是否正确；该日期没有数据时会返回空列表。若该接口不在当前套餐范围内，也不会返回可用数据。

**Q: 批量下载时变慢或超时怎么办？**
A: 请将间隔设为至少 0.2 秒，降低并发并分批保存。遇到网络波动时采用重试机制，不要持续重复提交同一批请求。

**Q: 不传某个参数时，返回全部数据还是默认值？**
A: 与官方文档一致——**按该参数的文档默认值过滤**。此前有一批默认值漏实现（不传即全量返回），现已补齐；如果你原先依赖“不传即全量”，请改为显式传值：

| 接口 | 参数 | 不传时的默认值 |
|---|---|---|
| `limit_list_ths` | limit_type | 涨停池 |
| `kpl_list` | tag | 涨停 |
| `stock_basic`、`hk_basic` | list_status | L（上市） |
| `index_basic` | market | SSE（上交所指数） |
| `index_member_all`、`ci_index_member` | is_new | Y（最新） |
| `libor` | curr_type | USD |

例如 `index_basic` 想查中证指数要显式传 `market='CSI'`；`limit_list_ths` 想查跌停池要显式传 `limit_type='跌停池'`。

**Q: `fund_basic` 为什么只返回几千条？官方说有两万多个基金。**
A: 这里**不要按“默认 E（场内）”去理解**，我们与上游保持一致：**不传 `market` 时返回全部（场内 E + 场外 O）**。以当前快照为例：

| 用法 | 返回 | 说明 |
|---|---|---|
| 不传 `market` | 32,922 条 | 场内 2,958 + 场外 29,964，全量 |
| `market='E'` | 2,958 条 | 仅场内基金，**看不到场外那两万多个** |
| `market='O'` | 29,964 条 | 场外基金，其中 `status='L'`（存续中）25,104 条 |

所以“要找两万多个基金”应该用 `market='O'`（或干脆不传 `market`），不要用 `market='E'`。另外注意单页上限 10,000 条，取全量需要翻页：响应里 `has_more` 为 `true` 时带上 `offset` 继续取下一页。

**Q: `fund_basic` 里为什么有 600 行 `status` 是空的？**
A: 那是新发行、上游尚未赋状态的基金（当前快照：场外 557 + 场内 43 = 600 行），**上游就是这么返回的，我们原样保留，不做填充**。如果你用 `status='L'` 之类的条件过滤，这 600 行会被静默丢掉——需要覆盖新基金时请把 `status` 为空的情况一并考虑。

**Q: API Key 可以给别人使用吗？**
A: 不建议。请妥善保管 API Key，不要提交到 Git 仓库、截图或公开聊天记录中；泄露后请及时联系售后处理。

**Q: 我的 Key 过期了怎么办？**
A: 重新找卖家购买即可获得新的 Key。如需延长原 Key，请联系卖家后台操作。

## 6. 联系我们

使用中遇到任何问题，欢迎加客服 QQ **2419798599**。

闲鱼平台限制较多，消息回复慢，也不方便传文件；**加了客服 QQ 后可以随时发代码、报错截图、数据文件**，排查问题更高效。

- **续费 / 延期**：到期前联系客服，购买新兑换码或延长原 Key
- **不会用 / 报错**：把代码、接口名、请求参数直接发给客服，快速定位
- **售后保障**：任何使用问题都有专人跟进，不用在闲鱼干等

添加时备注**来意 + API Key**，回复更快。

<img src="assets/support-qq.jpg" alt="客服QQ二维码" width="200" height="200">

- 请勿在公开渠道（群、贴吧、博客）暴露你的 API Key
