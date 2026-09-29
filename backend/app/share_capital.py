"""Resolve historical share capital with an optional strict research mode."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

SHARE_SOURCE_HISTORICAL_PIT = "historical_pit"
SHARE_SOURCE_CURRENT_DAY = "current_day"
SHARE_SOURCE_CURRENT_FALLBACK = "current_fallback"
SHARE_SOURCE_MISSING = "missing"


def load_share_history(data_dir: Path) -> pl.DataFrame:
    """读取本地财务股本表, 未同步或损坏时返回空表。"""
    path = data_dir / "financials" / "shares" / "part.parquet"
    if not path.exists():
        return pl.DataFrame()
    try:
        shares = pl.read_parquet(path)
        if not {"symbol", "period_end", "float_shares"} <= set(shares.columns):
            return pl.DataFrame()
        return shares
    except Exception:
        return pl.DataFrame()


def apply_historical_float_shares(
    rows: pl.DataFrame,
    shares: pl.DataFrame | None,
    *,
    today: date,
    strict: bool = False,
) -> pl.DataFrame:
    """Resolve effective float shares and record the selected source.

    Today's rows keep their input value. Historical rows use the latest PIT
    observation; strict mode leaves missing history null instead of falling
    back to the current instrument value.
    """
    required = {"symbol", "date", "float_shares"}
    if rows.is_empty() or not required <= set(rows.columns):
        return rows

    if (
        shares is None
        or shares.is_empty()
        or not {"symbol", "period_end", "float_shares"} <= set(shares.columns)
    ):
        return _tag_without_history(rows, today=today, strict=strict)

    def as_date_expr(column: str) -> pl.Expr:
        dtype = shares.schema[column]
        if dtype == pl.Utf8:
            return pl.col(column).str.to_date(strict=False)
        return pl.col(column).cast(pl.Date, strict=False)

    available_date = as_date_expr("period_end")
    if "announce_date" in shares.columns:
        available_date = as_date_expr("announce_date").fill_null(available_date)

    history = (
        shares
        .select(
            pl.col("symbol").cast(pl.Utf8),
            available_date.alias("_share_available_date"),
            pl.col("period_end").cast(pl.Utf8).alias("_share_period_end"),
            pl.col("float_shares").cast(pl.Float64, strict=False).alias("_historical_float_shares"),
        )
        .filter(
            pl.col("symbol").is_not_null()
            & pl.col("_share_available_date").is_not_null()
            & (pl.col("_historical_float_shares") > 0)
        )
        .sort(["symbol", "_share_available_date", "_share_period_end"])
        .unique(subset=["symbol", "_share_available_date"], keep="last")
        .sort(["symbol", "_share_available_date"])
    )
    if history.is_empty():
        return _tag_without_history(rows, today=today, strict=strict)

    trade_date = (
        pl.col("date").str.to_date(strict=False)
        if rows.schema["date"] == pl.Utf8
        else pl.col("date").cast(pl.Date, strict=False)
    )
    resolved = (
        rows
        .with_row_index("_share_row_order")
        .with_columns(
            pl.col("symbol").cast(pl.Utf8),
            trade_date.alias("_share_trade_date"),
            pl.col("float_shares").cast(pl.Float64, strict=False).alias("_input_float_shares"),
        )
        .sort(["symbol", "_share_trade_date"])
        .join_asof(
            history,
            left_on="_share_trade_date",
            right_on="_share_available_date",
            by="symbol",
            strategy="backward",
            check_sortedness=False,
        )
    )

    is_today = pl.col("_share_trade_date") == pl.lit(today)
    has_history = pl.col("_historical_float_shares").is_not_null()
    source = (
        pl.when(is_today)
        .then(pl.lit(SHARE_SOURCE_CURRENT_DAY))
        .when(has_history)
        .then(pl.lit(SHARE_SOURCE_HISTORICAL_PIT))
        .otherwise(
            pl.lit(SHARE_SOURCE_MISSING if strict else SHARE_SOURCE_CURRENT_FALLBACK)
        )
        .alias("float_shares_source")
    )
    if strict:
        float_shares = (
            pl.when(is_today)
            .then(pl.col("_input_float_shares"))
            .when(has_history)
            .then(pl.col("_historical_float_shares"))
            .otherwise(None)
            .alias("float_shares")
        )
    else:
        float_shares = (
            pl.when(is_today)
            .then(pl.col("_input_float_shares"))
            .otherwise(
                pl.coalesce("_historical_float_shares", "_input_float_shares")
            )
            .alias("float_shares")
        )

    return (
        resolved
        .with_columns(float_shares, source)
        .sort("_share_row_order")
        .drop(
            "_share_row_order",
            "_share_trade_date",
            "_share_available_date",
            "_share_period_end",
            "_historical_float_shares",
            "_input_float_shares",
        )
    )


def _tag_without_history(
    rows: pl.DataFrame,
    *,
    today: date,
    strict: bool,
) -> pl.DataFrame:
    trade_date = (
        pl.col("date").str.to_date(strict=False)
        if rows.schema["date"] == pl.Utf8
        else pl.col("date").cast(pl.Date, strict=False)
    )
    is_today = trade_date == pl.lit(today)
    source = (
        pl.when(is_today)
        .then(pl.lit(SHARE_SOURCE_CURRENT_DAY))
        .otherwise(
            pl.lit(SHARE_SOURCE_MISSING if strict else SHARE_SOURCE_CURRENT_FALLBACK)
        )
        .alias("float_shares_source")
    )
    float_shares = (
        pl.when(is_today)
        .then(pl.col("float_shares"))
        .otherwise(None if strict else pl.col("float_shares"))
        .alias("float_shares")
    )
    return rows.with_columns(float_shares, source)
