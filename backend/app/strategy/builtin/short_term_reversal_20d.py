"""20 日短期反转研究策略。

信号是过去 20 个交易日收益，低值优先。主复刻的五切片持仓由
``overlapping_sleeve_weights`` 生成；月末持有一个月由 ``month_end_holding_weights`` 生成。
二者都不走生产撮合器。本模块的信号矩阵只标出当日低收益分组，供研究检查。

未实现上市满 6 个月、历史 ST 和退市整理期过滤；这些数据当前不在 PIT 契约内。
"""
from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.strategy.builtin._research_portfolio import month_end_mask, select_low_bucket

META = {
    "id": "short_term_reversal_20d",
    "name": "20日短期反转",
    "description": (
        "研究策略：过去20日收益低值优先。"
        "主复刻是每日五份资金、每份持有5日；月末排序持有一个月是稳健性版本。"
        "权重函数与生产单仓位回测分开，不能把槽位回测称为五切片复刻。"
        "尚未过滤历史ST、退市样本和上市不满6个月。"
    ),
    "tags": ["研究", "反转", "动量"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "research_only": True,
    "params": [
        {
            "id": "top_n",
            "label": "每日最低收益入选数",
            "type": "int",
            "default": 20,
            "min": 1,
            "max": 500,
            "step": 1,
        },
        {
            "id": "quantile",
            "label": "最低收益分位(0表示用入选数)",
            "type": "float",
            "default": 0.0,
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
        },
        {
            "id": "month_end",
            "label": "仅月末形成信号",
            "type": "bool",
            "default": False,
        },
    ],
    # 分数在策略内写成 -momentum_20d。META 评分留空，避免流水线按高动量覆盖。
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_short_term_reversal_20d"]
EXIT_SIGNALS: list[str] = []
STOP_LOSS = None
MAX_HOLD_DAYS = None


class ShortTermReversal20dMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 21

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        past_return = matrix_feature(market, "momentum_20d")
        selected = _select(past_return, params)
        if bool(params.get("month_end", False)):
            selected = selected & month_end_mask(market.timestamp_labels)[:, None]
        score = np.zeros(market.shape, dtype=np.float32)
        finite = np.isfinite(past_return)
        score[finite] = -past_return[finite]
        return make_signal_matrix(
            market.shape,
            entry=selected.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(selected, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_short_term_reversal_20d",),
        )


def _select(values: np.ndarray, params: dict) -> np.ndarray:
    quantile = float(params.get("quantile", 0.0))
    if quantile > 0.0:
        return select_low_bucket(values, quantile=quantile)
    top_n = int(params.get("top_n", 20))
    if top_n < 1:
        raise ValueError("top_n must be a positive integer")
    return select_low_bucket(values, top_n=top_n)


MATRIX_STRATEGY = ShortTermReversal20dMatrixStrategy()
