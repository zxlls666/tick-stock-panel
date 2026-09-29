"""Strict PIT turnover overlay for research MarketDataMatrix runs."""
from __future__ import annotations

from datetime import date
from types import MappingProxyType
from typing import Any

import numpy as np
import polars as pl

from app.backtest.matrix import MarketDataMatrix
from app.share_capital import (
    SHARE_SOURCE_CURRENT_FALLBACK,
    apply_historical_float_shares,
)


def with_strict_pit_turnover(
    market: MarketDataMatrix,
    *,
    historical_shares: pl.DataFrame,
    instrument_float_shares: np.ndarray,
    today: date,
) -> tuple[MarketDataMatrix, dict[str, Any]]:
    """Return a market copy whose turnover_rate uses strict PIT float shares.

    Selected/research consumers must see current_fallback_count == 0.
    """
    if "turnover_rate" not in market.fields:
        raise ValueError("market matrix missing turnover_rate field")
    if instrument_float_shares.shape != (market.shape[1],):
        raise ValueError("instrument_float_shares must be one value per asset")

    n_times, n_assets = market.shape
    dates = np.repeat(
        np.asarray([label[:10] for label in market.timestamp_labels], dtype=object),
        n_assets,
    )
    symbols = np.tile(np.asarray(market.symbols, dtype=object), n_times)
    volumes = np.asarray(market.volume, dtype=np.float64).reshape(-1)
    current_shares = np.tile(
        np.asarray(instrument_float_shares, dtype=np.float64),
        n_times,
    )

    panel = pl.DataFrame({
        "symbol": symbols,
        "date": dates,
        "volume": volumes,
        "float_shares": current_shares,
    })
    resolved = apply_historical_float_shares(
        panel,
        historical_shares,
        today=today,
        strict=True,
    )
    turnover = (
        pl.when(pl.col("float_shares").is_not_null() & (pl.col("float_shares") > 0))
        .then(pl.col("volume") * 10_000.0 / pl.col("float_shares"))
        .otherwise(None)
        .alias("turnover_rate")
    )
    with_turnover = resolved.with_columns(turnover)
    source_counts = {
        str(key): int(value)
        for key, value in with_turnover["float_shares_source"].value_counts().iter_rows()
    }
    source_counts.setdefault(SHARE_SOURCE_CURRENT_FALLBACK, 0)

    rewritten = (
        with_turnover["turnover_rate"]
        .to_numpy()
        .astype(np.float64, copy=False)
        .reshape(n_times, n_assets)
    )
    float_shares = (
        with_turnover["float_shares"]
        .to_numpy()
        .astype(np.float64, copy=False)
        .reshape(n_times, n_assets)
    )
    raw_close = np.asarray(market.fields.get("raw_close", market.close), dtype=np.float64)
    log_float_mv = np.full(market.shape, np.nan, dtype=np.float64)
    valid_mv = (
        np.isfinite(float_shares)
        & (float_shares > 0)
        & np.isfinite(raw_close)
        & (raw_close > 0)
    )
    np.log(
        float_shares * raw_close,
        out=log_float_mv,
        where=valid_mv,
    )
    fields = {
        **market.fields,
        "turnover_rate": np.array(rewritten, copy=True),
        "log_float_mv": log_float_mv,
    }
    strict_market = MarketDataMatrix(
        timestamps=market.timestamps,
        timestamp_labels=market.timestamp_labels,
        session_ids=market.session_ids,
        symbols=market.symbols,
        names=market.names,
        open=market.open,
        high=market.high,
        low=market.low,
        close=market.close,
        volume=market.volume,
        tradable=market.tradable,
        limit_up_locked=market.limit_up_locked,
        limit_down_locked=market.limit_down_locked,
        fields=MappingProxyType(fields),
        cache_status=market.cache_status,
        cache_path=market.cache_path,
        vector_fields=market.vector_fields,
        cache_timing_ms=market.cache_timing_ms,
    )
    stats = {
        "source_counts": source_counts,
        "current_fallback_count": int(source_counts.get(SHARE_SOURCE_CURRENT_FALLBACK, 0)),
        "missing_count": int(source_counts.get("missing", 0)),
        "historical_pit_count": int(source_counts.get("historical_pit", 0)),
        "current_day_count": int(source_counts.get("current_day", 0)),
    }
    return strict_market, stats


# Backwards-compatible name used by early Phase 1 tests.
def overlay_strict_pit_turnover(
    market: MarketDataMatrix,
    *,
    historical_shares: pl.DataFrame,
    instrument_float_shares: np.ndarray,
    today: date,
) -> dict[str, Any]:
    """Mutate a writable turnover field in place; prefer with_strict_pit_turnover."""
    if "turnover_rate" not in market.fields:
        raise ValueError("market matrix missing turnover_rate field")
    target = np.asarray(market.fields["turnover_rate"])
    if not target.flags.writeable:
        raise ValueError(
            "turnover_rate is read-only; use with_strict_pit_turnover for cached matrices"
        )
    strict_market, stats = with_strict_pit_turnover(
        market,
        historical_shares=historical_shares,
        instrument_float_shares=instrument_float_shares,
        today=today,
    )
    np.copyto(target, strict_market.field("turnover_rate"))
    return stats


def instrument_float_share_vector(
    symbols: tuple[str, ...],
    instruments: pl.DataFrame,
) -> np.ndarray:
    """Align instruments.float_shares to the market asset axis."""
    if instruments.is_empty() or "symbol" not in instruments.columns:
        return np.full(len(symbols), np.nan, dtype=np.float64)
    if "float_shares" not in instruments.columns:
        return np.full(len(symbols), np.nan, dtype=np.float64)
    lookup = {
        str(row["symbol"]): float(row["float_shares"])
        for row in instruments.select(["symbol", "float_shares"]).iter_rows(named=True)
        if row["float_shares"] is not None and np.isfinite(float(row["float_shares"]))
    }
    return np.asarray([lookup.get(symbol, np.nan) for symbol in symbols], dtype=np.float64)
