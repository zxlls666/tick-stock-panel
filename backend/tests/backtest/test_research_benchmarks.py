"""Tests for research benchmark active-return statistics."""
from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from app.backtest.research_benchmarks import (
    active_performance_stats,
    align_active_returns,
    benchmark_close_to_close_returns,
)


def test_benchmark_and_active_stats_match_fixed_formulas() -> None:
    closes = pl.DataFrame({
        "date": [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        "close": [100.0, 110.0, 99.0, 108.9],
    })
    benchmark = benchmark_close_to_close_returns(closes)
    portfolio = pl.DataFrame({
        "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
        "daily_return": [0.05, -0.02, 0.04],
    })
    aligned = align_active_returns(portfolio, benchmark)
    stats = active_performance_stats(aligned)

    expected_bench = np.asarray([0.10, -0.10, 0.10])
    expected_active = np.asarray([0.05, -0.02, 0.04]) - expected_bench
    portfolio_nav = float(np.prod(1.0 + np.asarray([0.05, -0.02, 0.04])))
    benchmark_nav = float(np.prod(1.0 + expected_bench))
    relative_nav = portfolio_nav / benchmark_nav

    assert aligned["benchmark_return"].to_list() == pytest.approx(expected_bench.tolist())
    assert stats["excess_total_return"] == pytest.approx(relative_nav - 1.0)
    assert stats["tracking_error"] == pytest.approx(
        float(np.std(expected_active, ddof=0) * np.sqrt(252.0))
    )
    assert stats["information_ratio"] == pytest.approx(
        float(np.mean(expected_active) / np.std(expected_active, ddof=0) * np.sqrt(252.0))
    )


def test_align_active_returns_fails_closed_on_missing_benchmark_day() -> None:
    portfolio = pl.DataFrame({
        "date": ["2024-01-02", "2024-01-03"],
        "daily_return": [0.01, 0.02],
    })
    benchmark = pl.DataFrame({
        "date": ["2024-01-02"],
        "benchmark_return": [0.0],
    })
    with pytest.raises(ValueError, match="benchmark_missing_strategy_days"):
        align_active_returns(portfolio, benchmark)


def test_information_ratio_is_null_when_active_vol_is_zero() -> None:
    aligned = pl.DataFrame({
        "date": ["2024-01-02", "2024-01-03"],
        "daily_return": [0.01, 0.01],
        "benchmark_return": [0.01, 0.01],
        "portfolio_nav": [1.01, 1.0201],
        "benchmark_nav": [1.01, 1.0201],
        "relative_nav": [1.0, 1.0],
        "active_return": [0.0, 0.0],
    })
    stats = active_performance_stats(aligned)
    assert stats["information_ratio"] is None
    assert stats["tracking_error"] == pytest.approx(0.0)
