"""关键价位 (levels) 纯函数测试 —— 钉死 fib 口径 (levels_v1) 与版本常量。

对应 CONTRIBUTING §9「指标...固定样本数值断言」; 本模块新增测试前无任何覆盖。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.indicators.levels import (
    LEVEL_TYPES,
    LEVELS_METHODOLOGY_VERSION,
    _fibonacci_levels,
    compute_levels,
)

RATIOS = [0.236, 0.382, 0.5, 0.618, 0.786]
FIB_LABELS = ["Fib 23.6%", "Fib 38.2%", "Fib 50.0%", "Fib 61.8%", "Fib 78.6%"]


def _fib_frame(highs: list[float], lows: list[float], close: float) -> pl.DataFrame:
    # 显式 Float64: 入参可同时含整数与小数, 与 enriched 帧同 dtype, 避免 Polars 由首元素
    # 推断 Int64 后拒绝后续小数。
    return pl.DataFrame(
        {"high": highs, "low": lows, "close": [close] * len(highs)},
        schema={"high": pl.Float64, "low": pl.Float64, "close": pl.Float64},
    )


def test_fibonacci_uptrend_values() -> None:
    """低点在前、高点在后 → 上涨波段, 回撤 = high - range*r。"""
    highs = [11, 10, 9, 13, 14, 15, 16, 17, 20, 16]
    lows = [10, 9, 8, 9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5]
    out = _fibonacci_levels(_fib_frame(highs, lows, 13.0))

    assert [p["label"] for p in out] == FIB_LABELS
    assert [p["type"] for p in out] == ["fib"] * 5
    assert [p["strength"] for p in out] == ["medium"] * 5
    assert [p["value"] for p in out] == pytest.approx([round(20 - 12 * r, 2) for r in RATIOS])
    # close=13: 17.17/15.42/14.0 在其上(压力), 12.58/10.57 在其下(支撑)
    assert [p["side"] for p in out] == [
        "resistance", "resistance", "resistance", "support", "support",
    ]


def test_fibonacci_downtrend_values() -> None:
    """高点在前、低点在后 → 下跌波段, 回撤 = low + range*r。"""
    highs = [16, 20, 17, 15, 14, 13, 12, 11, 10, 11]
    lows = [15, 13, 11, 10, 9.5, 9, 8.5, 8.2, 8.0, 8.5]
    out = _fibonacci_levels(_fib_frame(highs, lows, 12.0))

    assert [p["value"] for p in out] == pytest.approx([round(8 + 12 * r, 2) for r in RATIOS])
    assert [p["side"] for p in out] == [
        "support", "resistance", "resistance", "resistance", "resistance",
    ]


def test_fibonacci_uses_tail_window() -> None:
    """>120 行时只用 tail(120): 尾部之外的极端值不参与。"""
    highs = [1000.0] + [11.0] * 9 + [11.0 + (i % 5) for i in range(120)]
    lows = [999.0] + [10.0] * 9 + [10.0 + (i % 5) for i in range(120)]
    highs[-1] = 25.0  # 尾部最高
    lows[10] = 5.0    # 尾部最低(后 120 行首行)

    out = _fibonacci_levels(_fib_frame(highs, lows, 12.0))
    assert len(out) == 5
    # 若误用全局极值(1000), 值会接近 750+; 正确口径下 range=20, 值均 < 25
    assert max(p["value"] for p in out) < 30


def test_fibonacci_insufficient_or_flat_returns_empty() -> None:
    nine_rows = _fib_frame([1, 2, 3, 4, 4, 4, 4, 4, 4], [1, 1, 1, 1, 1, 1, 1, 1, 1], 3.0)
    assert _fibonacci_levels(nine_rows) == []

    flat = _fib_frame([10.0] * 12, [10.0] * 12, 10.0)
    assert _fibonacci_levels(flat) == []


def test_fibonacci_tie_break_uses_first_occurrence() -> None:
    """并列最高价取首次出现位置 (list.index 语义), 决定波段方向。"""
    highs = [10, 20, 12, 13, 14, 15, 16, 17, 20, 11]  # 最高 20 在 idx1 与 idx8
    lows = [9, 9.5, 9, 9.5, 9, 8, 9, 9, 9.5, 9]        # 最低 8 在 idx5
    out = _fibonacci_levels(_fib_frame(highs, lows, 12.0))
    # 首次出现 idx1 < idx5 → 判为下跌波段 (若用末次 idx8 则为上涨波段)
    assert [p["value"] for p in out] == pytest.approx([round(8 + 12 * r, 2) for r in RATIOS])


def test_compute_levels_shape_and_empty() -> None:
    assert set(LEVEL_TYPES) == {
        "sr", "pivot", "extreme", "boll", "keltner_s", "keltner_m", "keltner_l",
        "atr_stop", "gap", "fib", "round",
    }

    empty = compute_levels(pl.DataFrame())
    assert set(empty) == set(LEVEL_TYPES)
    assert all(v == [] for v in empty.values())

    frame = pl.DataFrame({
        "symbol": ["X"] * 20,
        "date": [f"2026-01-{i + 1:02d}" for i in range(20)],
        "open": [10.0] * 20, "high": [10.5] * 20, "low": [9.5] * 20,
        "close": [10.0] * 20, "volume": [1000.0] * 20, "amount": [1e6] * 20,
    })
    assert set(compute_levels(frame)) == set(LEVEL_TYPES)


def test_levels_methodology_version_pinned() -> None:
    assert LEVELS_METHODOLOGY_VERSION == "levels_v1"
