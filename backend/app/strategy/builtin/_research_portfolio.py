"""研究组合构建。

这些函数只生成目标权重，不经过生产撮合器。生产回测对同一标的只保留一个仓位，
不能表达「五份资金、同一股票可叠加」或「月末信号、次日才换仓」。
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def select_low_bucket(
    values: np.ndarray,
    *,
    top_n: int | None = None,
    quantile: float | None = None,
) -> np.ndarray:
    """每个截面保留最低的有限值。

    ``top_n`` 与 ``quantile`` 必须且只能给一个。分位比例向下取整，至少保留 1 只。
    相同数值按资产轴原顺序保留靠前的标的。
    """
    data = np.asarray(values, dtype=np.float64)
    if data.ndim != 2:
        raise ValueError("values must be a time x asset matrix")
    if (top_n is None) == (quantile is None):
        raise ValueError("select_low_bucket requires exactly one of top_n or quantile")
    if top_n is not None and int(top_n) < 1:
        raise ValueError("top_n must be a positive integer")
    if quantile is not None and not 0.0 < float(quantile) <= 1.0:
        raise ValueError("quantile must be in (0, 1]")

    selected = np.zeros(data.shape, dtype=bool)
    for time_id in range(data.shape[0]):
        row = data[time_id]
        finite = np.flatnonzero(np.isfinite(row))
        n_finite = int(finite.size)
        if n_finite == 0:
            continue
        if top_n is not None:
            count = min(int(top_n), n_finite)
        else:
            count = int(np.floor(n_finite * float(quantile)))
            count = min(n_finite, max(count, 1))
        order = np.argsort(row[finite], kind="stable")[:count]
        selected[time_id, finite[order]] = True
    return selected


def month_end_mask(labels: Sequence[str]) -> np.ndarray:
    """样本内每个自然月的最后一个已出现交易日。

    序列末端如果还没看到下一个月，不把最后一天当成月末，避免用未完成月份提前换仓。
    """
    months = [str(label)[:7] for label in labels]
    mask = np.zeros(len(months), dtype=bool)
    for index in range(len(months) - 1):
        if months[index] != months[index + 1]:
            mask[index] = True
    return mask


def month_end_holding_weights(
    selected: np.ndarray,
    month_end: np.ndarray,
) -> np.ndarray:
    """月末信号从下一个交易日起等权持有，直到再下一个月末信号生效前。"""
    chosen = np.asarray(selected, dtype=bool)
    rebalance = np.asarray(month_end, dtype=bool)
    if chosen.ndim != 2:
        raise ValueError("selected must be a time x asset matrix")
    if rebalance.shape != (chosen.shape[0],):
        raise ValueError("month_end length does not match selected")
    weights = np.zeros(chosen.shape, dtype=np.float64)
    active: np.ndarray | None = None
    for time_id in range(chosen.shape[0]):
        if active is not None:
            weights[time_id] = active
        if not rebalance[time_id]:
            continue
        count = int(chosen[time_id].sum())
        active = np.zeros(chosen.shape[1], dtype=np.float64)
        if count:
            active[chosen[time_id]] = 1.0 / count
    return weights


def overlapping_sleeve_weights(
    selected: np.ndarray,
    *,
    n_sleeves: int = 5,
    execution_lag: int = 1,
) -> np.ndarray:
    """每日替换一份资金，每份持有 ``n_sleeves`` 个交易日。

    ``execution_lag=1`` 表示收盘信号在下一交易日才进入持仓，对应 ``open_t+1``。
    尚未形成的切片保持现金，不把已形成切片重新放大到满仓。
    同一标的出现在多份切片时，权重相加。
    """
    chosen = np.asarray(selected, dtype=bool)
    if chosen.ndim != 2:
        raise ValueError("selected must be a time x asset matrix")
    sleeves = int(n_sleeves)
    lag = int(execution_lag)
    if sleeves < 1:
        raise ValueError("n_sleeves must be positive")
    if lag < 0:
        raise ValueError("execution_lag must be non-negative")
    weights = np.zeros(chosen.shape, dtype=np.float64)
    sleeve_budget = 1.0 / sleeves
    for time_id in range(chosen.shape[0]):
        total = np.zeros(chosen.shape[1], dtype=np.float64)
        for age in range(sleeves):
            signal_id = time_id - lag - age
            if signal_id < 0:
                continue
            members = chosen[signal_id]
            count = int(members.sum())
            if count == 0:
                continue
            total[members] += sleeve_budget / count
        weights[time_id] = total
    return weights


def cross_sectional_residual(values: np.ndarray, control: np.ndarray) -> np.ndarray:
    """逐日对控制变量做一元线性回归，返回残差。有效配对少于 3 个时整行留空。"""
    left = np.asarray(values, dtype=np.float64)
    right = np.asarray(control, dtype=np.float64)
    if left.shape != right.shape or left.ndim != 2:
        raise ValueError("values and control must share a time x asset shape")
    residual = np.full(left.shape, np.nan, dtype=np.float64)
    for time_id in range(left.shape[0]):
        mask = np.isfinite(left[time_id]) & np.isfinite(right[time_id])
        if int(mask.sum()) < 3:
            continue
        y = left[time_id, mask]
        x = right[time_id, mask]
        x_center = x - x.mean()
        variance = float(np.dot(x_center, x_center))
        if variance <= 0.0:
            continue
        beta = float(np.dot(x_center, y - y.mean()) / variance)
        alpha = float(y.mean() - beta * x.mean())
        residual[time_id, mask] = left[time_id, mask] - (alpha + beta * right[time_id, mask])
    return residual
