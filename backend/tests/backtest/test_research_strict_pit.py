"""Tests for strict PIT turnover overlay."""
from __future__ import annotations

from datetime import date
from types import MappingProxyType, SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import MarketDataMatrix
from app.backtest.research_strict_pit import (
    instrument_float_share_vector,
    overlay_strict_pit_turnover,
    with_strict_pit_turnover,
)


def _tiny_market(
    turnover: np.ndarray,
    volume: np.ndarray,
    *,
    raw_close: np.ndarray | None = None,
) -> MarketDataMatrix:
    n_times, n_assets = turnover.shape
    blank = np.zeros((n_times, n_assets), dtype=np.float64)
    return MarketDataMatrix(
        timestamps=np.arange(n_times, dtype=np.int64),
        timestamp_labels=("2024-03-31", "2024-04-15", "2024-06-30")[:n_times],
        session_ids=np.arange(n_times, dtype=np.int32),
        symbols=("600000.SH",)[:n_assets],
        names=("测试",)[:n_assets],
        open=blank.copy(),
        high=blank.copy(),
        low=blank.copy(),
        close=blank.copy(),
        volume=volume,
        tradable=np.ones((n_times, n_assets), dtype=bool),
        limit_up_locked=np.zeros((n_times, n_assets), dtype=bool),
        limit_down_locked=np.zeros((n_times, n_assets), dtype=bool),
        fields=MappingProxyType({
            "turnover_rate": turnover,
            **({"raw_close": raw_close} if raw_close is not None else {}),
        }),
    )


def test_with_strict_pit_turnover_nulls_missing_and_counts_sources() -> None:
    turnover = np.full((3, 1), 9.0, dtype=np.float64)
    volume = np.full((3, 1), 10_000.0, dtype=np.float64)
    market = _tiny_market(turnover, volume)
    shares = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "period_end": ["2023-12-31", "2024-06-30"],
        "announce_date": ["2024-04-15", None],
        "float_shares": [100_000_000.0, 50_000_000.0],
    })

    strict_market, stats = with_strict_pit_turnover(
        market,
        historical_shares=shares,
        instrument_float_shares=np.asarray([200_000_000.0]),
        today=date(2026, 7, 18),
    )

    assert stats["current_fallback_count"] == 0
    assert stats["missing_count"] == 1
    assert stats["historical_pit_count"] == 2
    assert turnover[0, 0] == pytest.approx(9.0)  # original untouched
    rewritten = strict_market.field("turnover_rate")
    assert np.isnan(rewritten[0, 0])
    assert rewritten[1, 0] == pytest.approx(1.0)
    assert rewritten[2, 0] == pytest.approx(2.0)


def test_strict_pit_exposes_raw_price_float_market_cap():
    turnover = np.full((3, 1), 9.0, dtype=np.float64)
    volume = np.full((3, 1), 10_000.0, dtype=np.float64)
    raw_close = np.full((3, 1), 20.0, dtype=np.float64)
    market = _tiny_market(turnover, volume, raw_close=raw_close)
    shares = pl.DataFrame({
        "symbol": ["600000.SH"],
        "period_end": ["2023-12-31"],
        "announce_date": ["2024-03-31"],
        "float_shares": [100_000_000.0],
    })

    strict_market, _ = with_strict_pit_turnover(
        market,
        historical_shares=shares,
        instrument_float_shares=np.asarray([200_000_000.0]),
        today=date(2026, 7, 18),
    )

    np.testing.assert_allclose(
        strict_market.field("log_float_mv")[:, 0],
        np.log(20.0 * 100_000_000.0),
    )


def test_overlay_strict_pit_turnover_requires_writable_field() -> None:
    turnover = np.full((3, 1), 9.0, dtype=np.float64)
    turnover.flags.writeable = False
    market = SimpleNamespace(
        shape=turnover.shape,
        timestamp_labels=("2024-03-31", "2024-04-15", "2024-06-30"),
        symbols=("600000.SH",),
        volume=np.full((3, 1), 10_000.0),
        fields={"turnover_rate": turnover},
    )
    with pytest.raises(ValueError, match="read-only"):
        overlay_strict_pit_turnover(
            market,  # type: ignore[arg-type]
            historical_shares=pl.DataFrame(),
            instrument_float_shares=np.asarray([1.0]),
            today=date(2026, 7, 18),
        )


def test_instrument_float_share_vector_aligns_to_symbols() -> None:
    instruments = pl.DataFrame({
        "symbol": ["000001.SZ", "600000.SH"],
        "float_shares": [1.0, 2.0],
    })
    values = instrument_float_share_vector(("600000.SH", "999999.SH"), instruments)
    assert values[0] == pytest.approx(2.0)
    assert np.isnan(values[1])
