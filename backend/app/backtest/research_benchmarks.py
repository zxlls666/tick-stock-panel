"""Research benchmark alignment and active-return statistics."""
from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl


def benchmark_close_to_close_returns(
    closes: pl.DataFrame,
    *,
    date_col: str = "date",
    close_col: str = "close",
) -> pl.DataFrame:
    """Build close-to-close benchmark returns; first row is dropped as undefined."""
    required = {date_col, close_col}
    if not required <= set(closes.columns):
        raise ValueError(f"benchmark frame missing columns: {sorted(required - set(closes.columns))}")
    ordered = closes.sort(date_col)
    previous = ordered[close_col].shift(1)
    returns = ordered[close_col] / previous - 1.0
    return (
        ordered
        .with_columns(returns.alias("benchmark_return"))
        .filter(previous.is_not_null())
        .select(
            pl.col(date_col).cast(pl.Utf8).str.slice(0, 10).alias("date"),
            pl.col("benchmark_return").cast(pl.Float64),
        )
    )


def align_active_returns(
    portfolio_daily: pl.DataFrame,
    benchmark_returns: pl.DataFrame,
) -> pl.DataFrame:
    """Align portfolio and benchmark on shared dates; fail closed on missing benchmark days."""
    if "date" not in portfolio_daily.columns or "daily_return" not in portfolio_daily.columns:
        raise ValueError("portfolio_daily requires date and daily_return")
    if "date" not in benchmark_returns.columns or "benchmark_return" not in benchmark_returns.columns:
        raise ValueError("benchmark_returns requires date and benchmark_return")

    portfolio = portfolio_daily.with_columns(
        pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date")
    )
    benchmark = benchmark_returns.with_columns(
        pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date")
    )
    joined = portfolio.join(benchmark, on="date", how="left")
    missing = int(joined["benchmark_return"].null_count())
    if missing:
        raise ValueError(f"benchmark_missing_strategy_days == {missing}")

    portfolio_nav = (1.0 + joined["daily_return"]).cum_prod()
    benchmark_nav = (1.0 + joined["benchmark_return"]).cum_prod()
    active = joined["daily_return"] - joined["benchmark_return"]
    return joined.with_columns(
        portfolio_nav.alias("portfolio_nav"),
        benchmark_nav.alias("benchmark_nav"),
        (portfolio_nav / benchmark_nav).alias("relative_nav"),
        active.alias("active_return"),
    )


def active_performance_stats(aligned: pl.DataFrame) -> dict[str, float | None]:
    """Compute excess return, tracking error and information ratio (ddof=0)."""
    if aligned.is_empty():
        raise ValueError("aligned frame is empty")
    active = aligned["active_return"].to_numpy()
    relative_nav = float(aligned["relative_nav"][-1])
    excess_total_return = relative_nav - 1.0
    periods = max(len(active), 1)
    annual_excess = (1.0 + excess_total_return) ** (252.0 / periods) - 1.0
    tracking_error = float(np.std(active, ddof=0) * np.sqrt(252.0))
    mean_active = float(np.mean(active))
    std_active = float(np.std(active, ddof=0))
    information_ratio = (
        None
        if std_active == 0.0
        else float(mean_active / std_active * np.sqrt(252.0))
    )
    relative_drawdown = float((aligned["relative_nav"] / aligned["relative_nav"].cum_max() - 1.0).min())
    return {
        "benchmark_total_return": float(aligned["benchmark_nav"][-1] - 1.0),
        "excess_total_return": excess_total_return,
        "annual_excess_return": annual_excess,
        "tracking_error": tracking_error,
        "information_ratio": information_ratio,
        "relative_drawdown": relative_drawdown,
        "relative_nav": relative_nav,
    }


def load_benchmark_returns(
    repo,
    symbol: str,
    *,
    start: date,
    end: date,
) -> pl.DataFrame:
    """Load index closes with one pre-day warmup, then emit close-to-close returns."""
    frame = repo.get_index_daily(
        symbol,
        start,
        end,
        columns=["date", "close"],
    )
    if frame.is_empty():
        raise ValueError(f"benchmark history empty: {symbol}")
    return benchmark_close_to_close_returns(frame)
