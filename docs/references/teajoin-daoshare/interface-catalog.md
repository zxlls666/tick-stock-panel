# DaoShare 已支持接口目录

> 来源：<https://teajoin.com/guide>，抓取于2026-09-27。
> 页面声明当前支持195个Tushare兼容接口；实际可用性仍受套餐、独立权限和服务端状态约束。

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

## 本地核验

- 解析接口数：195
- 唯一接口数：195
- TSP历史股本相关：`daily_basic`、`stock_basic`、`share_float`
- 指南清单未包含：`stk_premarket`
