"""图表指标参数化 (custom.py) 契约测试。

核心断言: **默认参数产出的别名列与规范列数值逐位一致** —— 自定义周期走的是与
`pipeline.compute_indicators` 相同的口径, 只改周期/别名列, 不改数值语义。
另覆盖 spec 解析的 fail-closed 行为。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.indicators.custom import (
    IndicatorSpec,
    compute_custom_indicators,
    max_period,
    parse_indicator_specs,
)
from app.indicators.pipeline import compute_indicators


def _bars(n: int = 90) -> pl.DataFrame:
    rows = []
    for symbol, offset in (("600000", 0.0), ("300001", 2.0)):
        for i in range(n):
            close = 10.0 + offset + i * 0.03 + ((i % 7) - 3) * 0.04
            rows.append({
                "symbol": symbol,
                "date": date(2024, 1, 1) + timedelta(days=i),
                "open": close - 0.02,
                "high": close + 0.10,
                "low": close - 0.10,
                "close": close,
                "volume": 1000 + i * 10,
                "amount": close * (1000 + i * 10),
                "raw_close": close,
                "raw_high": close + 0.10,
                "raw_low": close - 0.10,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


DEFAULT_SPECS = (
    "ma:5:10:20:30:60,ema:5:10:20:30:60,rsi:6:14:24,macd:12:26:9,boll:20:2,kdj:9"
)

# (别名列, 规范列)
PARITY_PAIRS = [
    ("ma_5_c", "ma5"),
    ("ma_20_c", "ma20"),
    ("ma_60_c", "ma60"),
    ("ema_5_c", "ema5"),
    ("ema_20_c", "ema20"),
    ("rsi_6_c", "rsi_6"),
    ("rsi_14_c", "rsi_14"),
    ("rsi_24_c", "rsi_24"),
    ("macd_12_26_9_dif_c", "macd_dif"),
    ("macd_12_26_9_dea_c", "macd_dea"),
    ("macd_12_26_9_hist_c", "macd_hist"),
    ("boll_20_2_upper_c", "boll_upper"),
    ("boll_20_2_lower_c", "boll_lower"),
    ("kdj_9_k_c", "kdj_k"),
    ("kdj_9_d_c", "kdj_d"),
    ("kdj_9_j_c", "kdj_j"),
]


def test_default_params_match_canonical_values() -> None:
    bars = _bars()
    canonical = compute_indicators(bars)
    custom = compute_custom_indicators(bars, parse_indicator_specs(DEFAULT_SPECS))

    for alias, canon in PARITY_PAIRS:
        assert alias in custom.columns, f"缺少别名列 {alias}"
        assert custom[alias].equals(canonical[canon]), f"{alias} != {canon}"


def test_custom_period_values_are_finite_and_differ_from_canonical() -> None:
    bars = _bars()
    canonical = compute_indicators(bars)
    custom = compute_custom_indicators(bars, parse_indicator_specs("ma:13"))

    assert "ma_13_c" in custom.columns
    # 预热后的行应有非空值
    assert custom["ma_13_c"].null_count() < custom.height
    # 13 日均线不应与 10 日/20 日规范列重合 (存在真实差异)
    tail = custom["ma_13_c"].to_list()[-1]
    assert tail != canonical["ma10"].to_list()[-1]
    assert tail != canonical["ma20"].to_list()[-1]


def test_compute_empty_specs_returns_frame_unchanged() -> None:
    bars = _bars()
    out = compute_custom_indicators(bars, [])
    assert out.equals(bars)


def test_compute_sorts_input_for_window_correctness() -> None:
    bars = _bars().sort(["symbol", "date"], descending=True)
    custom = compute_custom_indicators(bars, [IndicatorSpec("ma", (5,))])
    assert custom["ma_5_c"].equals(
        compute_custom_indicators(_bars(), [IndicatorSpec("ma", (5,))])["ma_5_c"]
    )


# ── spec 解析 (fail-closed) ─────────────────────────────


def test_parse_blank_returns_empty() -> None:
    assert parse_indicator_specs(None) == []
    assert parse_indicator_specs("") == []
    assert parse_indicator_specs("  ") == []


def test_parse_accepts_all_kinds_and_dedupes() -> None:
    specs = parse_indicator_specs("ma:5:13,ma:5:13,rsi:7")
    assert specs == [IndicatorSpec("ma", (5, 13)), IndicatorSpec("rsi", (7,))]


def test_parse_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        parse_indicator_specs("foo:5")


def test_parse_rejects_out_of_range_period() -> None:
    with pytest.raises(ValueError):
        parse_indicator_specs("ma:1")
    with pytest.raises(ValueError):
        parse_indicator_specs(f"ma:{10000}")


def test_parse_rejects_macd_fast_not_below_slow() -> None:
    with pytest.raises(ValueError):
        parse_indicator_specs("macd:26:12:9")
    with pytest.raises(ValueError):
        parse_indicator_specs("macd:12:12:9")


def test_parse_rejects_non_integer_params() -> None:
    with pytest.raises(ValueError):
        parse_indicator_specs("ma:5.5")


def test_max_period() -> None:
    assert max_period([]) == 0
    assert max_period(parse_indicator_specs("ma:5:60,rsi:14")) == 60
