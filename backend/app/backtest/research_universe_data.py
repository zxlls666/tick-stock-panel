"""Phase 2 研究股票池数据标准化。"""
from __future__ import annotations

import polars as pl

_RESEARCH_INSTRUMENT_SCHEMA = {
    "symbol": pl.String,
    "name": pl.String,
    "exchange": pl.String,
    "list_date": pl.Date,
    "delist_date": pl.Date,
    "instrument_source": pl.String,
}

_INSTRUMENT_SCHEMA = {
    "symbol": pl.String,
    "name": pl.String,
    "exchange": pl.String,
    "list_date": pl.Date,
    "delist_date": pl.Date,
    "delist_reason": pl.String,
    "load_status": pl.String,
    "bar_count": pl.Int64,
    "error": pl.String,
    "source": pl.String,
}


def merge_research_instruments(
    current: pl.DataFrame,
    delisted: pl.DataFrame,
) -> pl.DataFrame:
    """Overlay delisting metadata onto current instruments without touching production data."""
    _require_columns(
        current,
        {"symbol", "name", "exchange", "listing_date"},
        "current instruments",
    )
    _require_columns(
        delisted,
        {"symbol", "name", "exchange", "list_date", "delist_date", "source"},
        "delisted instruments",
    )
    if current["symbol"].n_unique() != current.height:
        raise ValueError("current instruments requires unique symbols")
    if delisted["symbol"].n_unique() != delisted.height:
        raise ValueError("delisted instruments requires unique symbols")

    normalized_current = current.select(
        "symbol",
        "name",
        "exchange",
        pl.col("listing_date").cast(pl.String).str.to_date(strict=False).alias("list_date"),
        pl.lit(None, dtype=pl.Date).alias("delist_date"),
        pl.lit("current_instruments").alias("instrument_source"),
    )
    overlay = delisted.select(
        "symbol",
        "name",
        "exchange",
        "list_date",
        "delist_date",
        pl.col("source").alias("instrument_source"),
    )
    result = pl.concat([
        normalized_current.join(overlay.select("symbol"), on="symbol", how="anti"),
        overlay,
    ]).sort("symbol")
    if result["symbol"].n_unique() != result.height:
        raise ValueError("research instruments requires unique symbols")
    if result["list_date"].null_count():
        symbols = result.filter(pl.col("list_date").is_null())["symbol"].to_list()
        raise ValueError(f"research instruments missing list_date: {symbols}")
    if result.filter(
        pl.col("delist_date").is_not_null()
        & (pl.col("delist_date") < pl.col("list_date"))
    ).height:
        raise ValueError("research instruments contain delist_date before list_date")
    return result.cast(_RESEARCH_INSTRUMENT_SCHEMA)


def normalize_baostock_instruments(raw: pl.DataFrame) -> pl.DataFrame:
    """Normalize delisted Shanghai/Shenzhen A-share metadata."""
    required = {"code", "code_name", "ipoDate", "outDate", "type", "status"}
    _require_columns(raw, required, "BaoStock instruments")
    result = (
        raw.filter(
            (pl.col("type") == "1")
            & (
                pl.col("code").str.starts_with("sh.")
                | pl.col("code").str.starts_with("sz.")
            )
            & (pl.col("outDate") != "")
        )
        .with_columns(
            _symbol_expr("code").alias("symbol"),
            pl.when(pl.col("code").str.starts_with("sh.")).then(pl.lit("SH"))
            .otherwise(pl.lit("SZ")).alias("exchange"),
            pl.col("ipoDate").str.to_date(strict=False).alias("list_date"),
            pl.col("outDate").str.to_date(strict=False).alias("delist_date"),
        )
        .select(
            "symbol",
            pl.col("code_name").alias("name"),
            "exchange",
            "list_date",
            "delist_date",
            pl.lit(None, dtype=pl.String).alias("delist_reason"),
            pl.lit("pending").alias("load_status"),
            pl.lit(0, dtype=pl.Int64).alias("bar_count"),
            pl.lit(None, dtype=pl.String).alias("error"),
            pl.lit("baostock:query_stock_basic").alias("source"),
        )
        .unique("symbol", keep="last")
        .sort("symbol")
    )
    if result["list_date"].null_count() or result["delist_date"].null_count():
        raise ValueError("BaoStock instruments contain invalid dates")
    if result.filter(pl.col("delist_date") < pl.col("list_date")).height:
        raise ValueError("BaoStock instruments contain delist_date before list_date")
    return result.cast(_INSTRUMENT_SCHEMA)


def normalize_baostock_daily(raw: pl.DataFrame) -> pl.DataFrame:
    """Normalize unadjusted traded bars and discard BaoStock suspension placeholders."""
    required = {
        "date", "code", "open", "high", "low", "close",
        "volume", "amount", "tradestatus", "isST",
    }
    _require_columns(raw, required, "BaoStock daily")
    result = (
        raw.filter(
            (pl.col("tradestatus") == "1")
            & (pl.col("volume") != "")
            & (pl.col("amount") != "")
        )
        .with_columns(
            _symbol_expr("code").alias("symbol"),
            pl.col("date").str.to_date(strict=False),
            *[
                pl.col(column).cast(pl.Float64, strict=False)
                for column in ("open", "high", "low", "close", "volume", "amount")
            ],
            (pl.col("isST") == "1").alias("is_st"),
        )
        .select(
            "symbol", "date", "open", "high", "low", "close", "volume", "amount",
            pl.lit(1.0).alias("adj_factor"),
            "is_st",
            pl.lit("baostock:query_history_k_data_plus:unadjusted").alias("source"),
        )
        .sort("symbol", "date")
    )
    if result.select("symbol", "date").is_duplicated().any():
        raise ValueError("BaoStock daily requires unique symbol/date rows")
    required_values = result.select("date", "open", "high", "low", "close", "volume", "amount")
    if required_values.null_count().row(0) != (0,) * required_values.width:
        raise ValueError("BaoStock daily contains invalid numeric values")
    invalid = result.filter(
        (pl.col("open") <= 0)
        | (pl.col("high") <= 0)
        | (pl.col("low") <= 0)
        | (pl.col("close") <= 0)
        | (pl.col("volume") < 0)
        | (pl.col("amount") < 0)
        | (pl.col("high") < pl.max_horizontal("open", "close", "low"))
        | (pl.col("low") > pl.min_horizontal("open", "close", "high"))
    )
    if invalid.height:
        raise ValueError("BaoStock daily contains invalid OHLC or volume/amount")
    return result


def apply_baostock_forward_adjustment(
    raw_bars: pl.DataFrame,
    adjusted_close: pl.DataFrame,
) -> pl.DataFrame:
    """Apply BaoStock's forward-adjustment ratio to isolated raw delisted bars."""
    _require_columns(
        raw_bars,
        {"symbol", "date", "open", "high", "low", "close", "volume", "amount"},
        "raw delisted bars",
    )
    _require_columns(
        adjusted_close,
        {"date", "code", "close", "tradestatus"},
        "BaoStock forward-adjusted close",
    )
    if raw_bars["symbol"].n_unique() != 1:
        raise ValueError("raw delisted bars must contain exactly one symbol")
    symbol = str(raw_bars["symbol"][0])
    expected_code = _baostock_code(symbol)
    unexpected = adjusted_close.filter(pl.col("code") != expected_code).select("code").unique()
    if unexpected.height:
        raise ValueError(
            f"forward-adjusted data has unexpected BaoStock codes: {unexpected['code'].to_list()}"
        )
    adjusted = (
        adjusted_close.filter(pl.col("tradestatus") == "1")
        .with_columns(
            pl.col("date").cast(pl.String).str.to_date(strict=False),
            pl.col("close").cast(pl.Float64, strict=False).alias("_adjusted_close"),
        )
        .select("date", "_adjusted_close")
    )
    if adjusted["date"].null_count() or adjusted["_adjusted_close"].null_count():
        raise ValueError("BaoStock forward-adjusted data contains invalid values")
    if adjusted["date"].n_unique() != adjusted.height:
        raise ValueError("BaoStock forward-adjusted data requires unique dates")
    joined = raw_bars.join(adjusted, on="date", how="left")
    missing = joined.filter(pl.col("_adjusted_close").is_null())
    if missing.height:
        raise ValueError(
            f"missing forward-adjusted dates for {symbol}: {missing['date'].to_list()[:10]}"
        )
    result = (
        joined.with_columns(
            (pl.col("_adjusted_close") / pl.col("close")).alias("adj_factor"),
            pl.col("close").alias("raw_close"),
            pl.col("high").alias("raw_high"),
            pl.col("low").alias("raw_low"),
        )
        .with_columns(
            *[
                (pl.col(column) * pl.col("adj_factor")).alias(column)
                for column in ("open", "high", "low")
            ],
            pl.col("_adjusted_close").alias("close"),
            (pl.col("volume") / 100.0).floor().alias("volume"),
            pl.lit("baostock:query_history_k_data_plus:forward_adjusted").alias("source"),
        )
        .drop("_adjusted_close")
    )
    if result.filter(
        ~pl.col("adj_factor").is_finite() | (pl.col("adj_factor") <= 0)
    ).height:
        raise ValueError("BaoStock forward-adjusted data produced invalid factors")
    return result


def st_changes_from_daily(bars: pl.DataFrame) -> pl.DataFrame:
    """Compress daily BaoStock ST flags into point-in-time state changes."""
    _require_columns(bars, {"symbol", "date", "is_st"}, "ST daily bars")
    return (
        bars.sort("symbol", "date")
        .with_columns(pl.col("is_st").shift(1).over("symbol").alias("_previous"))
        .filter(pl.col("_previous").is_null() | (pl.col("is_st") != pl.col("_previous")))
        .select(
            "symbol",
            pl.col("date").alias("effective_date"),
            pl.col("is_st").alias("st_flag"),
            pl.when(pl.col("is_st")).then(pl.lit("ST")).otherwise(pl.lit("normal")).alias("st_type"),
            pl.lit(None, dtype=pl.String).alias("name"),
            pl.lit("baostock:daily_isST").alias("source"),
            pl.col("date").alias("source_date"),
        )
    )


def _baostock_code(symbol: str) -> str:
    code, exchange = symbol.split(".")
    return f"{exchange.lower()}.{code}"


def _symbol_expr(column: str) -> pl.Expr:
    return pl.col(column).str.slice(3, 6) + pl.lit(".") + pl.col(column).str.slice(0, 2).str.to_uppercase()


def _require_columns(frame: pl.DataFrame, required: set[str], label: str) -> None:
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{label} missing columns: {sorted(missing)}")
