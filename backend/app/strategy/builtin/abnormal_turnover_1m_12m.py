"""1 个月异常换手研究策略。

因子是过去 20 日平均换手率 / 过去 250 日平均换手率，低值优先。
它与 ``turnover_z_60d`` 不是同一个信号。主调仓是月末形成、次日持有到下一次月末；
日频入选只作为敏感性信号。规模中性是对流通市值对数的截面回归残差，默认关闭。

换手率沿用 enriched 口径：有历史股本时用公告日不晚于交易日的流通股本，
没有历史股本时回退当前流通股本。因此本因子不能标成完整 PIT。
"""
from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.strategy.builtin._research_portfolio import (
    cross_sectional_residual,
    month_end_mask,
    select_low_bucket,
)

META = {
    "id": "abnormal_turnover_1m_12m",
    "name": "1个月异常换手",
    "description": (
        "研究策略：20日均换手/250日均换手，低值优先，至少250个有效交易日。"
        "主版本月末形成并持有一个月；日频入选是敏感性版本。"
        "与turnover_z_60d分开。历史股本缺失时换手率会回退当前流通股本。"
    ),
    "tags": ["研究", "换手", "流动性"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "research_only": True,
    "params": [
        {
            "id": "top_n",
            "label": "最低异常换手入选数",
            "type": "int",
            "default": 20,
            "min": 1,
            "max": 500,
            "step": 1,
        },
        {
            "id": "quantile",
            "label": "最低异常换手分位(0表示用入选数)",
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
            "default": True,
        },
        {
            "id": "neutralize_size",
            "label": "对流通市值对数取残差",
            "type": "bool",
            "default": False,
        },
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_abnormal_turnover_1m_12m"]
EXIT_SIGNALS: list[str] = []
STOP_LOSS = None
MAX_HOLD_DAYS = None


class AbnormalTurnover1m12mMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 251

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        raw = matrix_feature(market, "abnormal_turnover_1m_12m")
        values = raw
        if bool(params.get("neutralize_size", False)):
            values = cross_sectional_residual(raw, matrix_feature(market, "log_float_mv"))
        selected = _select(values, params)
        if bool(params.get("month_end", True)):
            selected = selected & month_end_mask(market.timestamp_labels)[:, None]
        score = np.zeros(market.shape, dtype=np.float32)
        finite = np.isfinite(values)
        score[finite] = -np.asarray(values, dtype=np.float32)[finite]
        return make_signal_matrix(
            market.shape,
            entry=selected.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(selected, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_abnormal_turnover_1m_12m",),
        )


def _select(values: np.ndarray, params: dict) -> np.ndarray:
    quantile = float(params.get("quantile", 0.0))
    if quantile > 0.0:
        return select_low_bucket(values, quantile=quantile)
    top_n = int(params.get("top_n", 20))
    if top_n < 1:
        raise ValueError("top_n must be a positive integer")
    return select_low_bucket(values, top_n=top_n)


MATRIX_STRATEGY = AbnormalTurnover1m12mMatrixStrategy()
