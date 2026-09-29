"""研究专用逐日可投资股票池。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

import numpy as np
import polars as pl

from app.backtest.matrix import MarketDataMatrix
from app.price_limits import (
    MAIN_BOARD_LIMIT,
    MAIN_BOARD_ST_LIMIT_CHANGE_DATE,
    board_limit_pct,
    numpy_limit_price,
)


@dataclass(frozen=True)
class ResearchUniverse:
    eligible: np.ndarray
    st_known: np.ndarray
    st_flag: np.ndarray
    price_limit_pct: np.ndarray
    funnel: pl.DataFrame


def append_research_bars(
    market: MarketDataMatrix,
    bars: pl.DataFrame,
    instruments: pl.DataFrame,
) -> MarketDataMatrix:
    """Append disjoint research assets on the existing production date axis."""
    required = {"symbol", "date", "open", "high", "low", "close", "volume"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"research bars missing columns: {sorted(missing)}")
    if bars.select("symbol", "date").is_duplicated().any():
        raise ValueError("research bars require unique symbol/date rows")
    new_symbols = tuple(sorted(str(value) for value in bars["symbol"].unique().to_list()))
    overlap = sorted(set(new_symbols) & set(market.symbols))
    if overlap:
        raise ValueError(f"research bars overlap production assets: {overlap[:10]}")
    date_ids = {label[:10]: index for index, label in enumerate(market.timestamp_labels)}
    outside_dates = sorted(
        value.isoformat()
        for value in bars["date"].unique().to_list()
        if value.isoformat() not in date_ids
    )
    if outside_dates:
        raise ValueError(f"research bars outside market date axis: {outside_dates[:10]}")
    if instruments["symbol"].n_unique() != instruments.height:
        raise ValueError("research instruments require unique symbols")
    names = {
        str(row["symbol"]): str(row.get("name") or "")
        for row in instruments.iter_rows(named=True)
    }
    missing_names = sorted(set(new_symbols) - set(names))
    if missing_names:
        raise ValueError(f"research bars missing instrument names: {missing_names[:10]}")

    n_times, old_assets = market.shape
    total_assets = old_assets + len(new_symbols)
    symbol_ids = {symbol: old_assets + index for index, symbol in enumerate(new_symbols)}
    shape = (n_times, total_assets)

    def extend(source: np.ndarray, *, fill: float | bool = np.nan) -> np.ndarray:
        result = np.full(shape, fill, dtype=source.dtype)
        result[:, :old_assets] = source
        return result

    arrays = {
        "open": extend(market.open),
        "high": extend(market.high),
        "low": extend(market.low),
        "close": extend(market.close),
        "volume": extend(market.volume, fill=0.0),
    }
    numeric_bar_fields = {
        name
        for name, dtype in bars.schema.items()
        if dtype.is_numeric() and name not in arrays
    }
    field_names = set(market.fields) | numeric_bar_fields
    fields = {
        name: extend(values) if (values := market.fields.get(name)) is not None
        else np.full(shape, np.nan, dtype=np.float64)
        for name in field_names
    }
    tradable = extend(market.tradable, fill=False)
    limit_up = extend(market.limit_up_locked, fill=False)
    limit_down = extend(market.limit_down_locked, fill=False)

    rows = bars.with_columns(
        pl.col("date").dt.to_string("%Y-%m-%d").replace_strict(date_ids).alias("_time_id"),
        pl.col("symbol").replace_strict(symbol_ids).alias("_asset_id"),
    )
    time_ids = rows["_time_id"].to_numpy()
    asset_ids = rows["_asset_id"].to_numpy()
    for name, target in arrays.items():
        target[time_ids, asset_ids] = rows[name].cast(pl.Float64).to_numpy()
    for name in numeric_bar_fields:
        fields[name][time_ids, asset_ids] = rows[name].cast(pl.Float64).to_numpy()
    present = (
        np.isfinite(arrays["open"][:, old_assets:])
        & np.isfinite(arrays["high"][:, old_assets:])
        & np.isfinite(arrays["low"][:, old_assets:])
        & np.isfinite(arrays["close"][:, old_assets:])
        & (arrays["volume"][:, old_assets:] > 0)
    )
    tradable[:, old_assets:] = present
    for values in (*arrays.values(), tradable, limit_up, limit_down, *fields.values()):
        values.flags.writeable = False
    return MarketDataMatrix(
        timestamps=market.timestamps,
        timestamp_labels=market.timestamp_labels,
        session_ids=market.session_ids,
        symbols=market.symbols + new_symbols,
        names=market.names + tuple(names[symbol] for symbol in new_symbols),
        open=arrays["open"],
        high=arrays["high"],
        low=arrays["low"],
        close=arrays["close"],
        volume=arrays["volume"],
        tradable=tradable,
        limit_up_locked=limit_up,
        limit_down_locked=limit_down,
        fields=MappingProxyType(fields),
        cache_status="research_overlay",
        vector_fields=market.vector_fields,
    )


def build_delisting_mask(
    trading_dates: tuple[date, ...],
    symbols: tuple[str, ...],
    instruments: pl.DataFrame,
) -> np.ndarray:
    """Return a mask that stays true from each instrument's delist date onward."""
    required = {"symbol", "delist_date"}
    missing = required - set(instruments.columns)
    if missing:
        raise ValueError(f"instruments missing columns: {sorted(missing)}")
    if instruments["symbol"].n_unique() != instruments.height:
        raise ValueError("instruments requires unique symbols")
    delistings = {
        str(row["symbol"]): row["delist_date"]
        for row in instruments.select("symbol", "delist_date").iter_rows(named=True)
        if row["delist_date"] is not None
    }
    dates = np.asarray([value.toordinal() for value in trading_dates], dtype=np.int64)
    result = np.zeros((len(trading_dates), len(symbols)), dtype=bool)
    for asset_id, symbol in enumerate(symbols):
        delist_date = delistings.get(symbol)
        if delist_date is not None:
            result[:, asset_id] = dates >= delist_date.toordinal()
    return result


def mask_research_values(values: np.ndarray, eligible: np.ndarray) -> np.ndarray:
    """Return a float copy with ineligible cross-sectional cells set to NaN."""
    source = np.asarray(values, dtype=np.float64)
    mask = np.asarray(eligible, dtype=bool)
    if source.shape != mask.shape:
        raise ValueError("eligible shape must match values")
    return np.where(mask, source, np.nan)


def with_research_price_limits(
    market: MarketDataMatrix,
    universe: ResearchUniverse,
) -> MarketDataMatrix:
    """Clone a research matrix with PIT limit percentages and lock flags."""
    if universe.price_limit_pct.shape != market.shape:
        raise ValueError("research price_limit_pct shape must match market")
    percentages = np.asarray(universe.price_limit_pct, dtype=np.float64)
    up_locked, down_locked = _research_limit_locks(market, percentages)
    fields = {
        **market.fields,
        "price_limit_pct": np.array(percentages, dtype=np.float64, copy=True),
    }
    return MarketDataMatrix(
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
        limit_up_locked=up_locked,
        limit_down_locked=down_locked,
        fields=MappingProxyType(fields),
        cache_status=market.cache_status,
        cache_path=market.cache_path,
        vector_fields=market.vector_fields,
        cache_timing_ms=market.cache_timing_ms,
    )


def _research_limit_locks(
    market: MarketDataMatrix,
    percentages: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    raw_close = np.asarray(market.fields.get("raw_close", market.close), dtype=np.float64)
    close = np.asarray(market.close, dtype=np.float64)
    present = np.isfinite(close) & np.isfinite(raw_close) & (raw_close > 0)
    up_locked = np.zeros(market.shape, dtype=bool)
    down_locked = np.zeros(market.shape, dtype=bool)
    previous_close = np.full(market.shape[1], np.nan, dtype=np.float64)
    previous_raw = np.full(market.shape[1], np.nan, dtype=np.float64)
    previous_adjustment = np.full(market.shape[1], np.nan, dtype=np.float64)
    for time_id in range(market.shape[0]):
        current_close = close[time_id]
        current_raw = raw_close[time_id]
        current_adjustment = np.full(market.shape[1], np.nan, dtype=np.float64)
        np.divide(
            current_close,
            current_raw,
            out=current_adjustment,
            where=np.isfinite(current_raw) & (current_raw != 0),
        )
        adjustment_changed = (
            np.isfinite(current_adjustment)
            & np.isfinite(previous_adjustment)
            & (np.abs(current_adjustment - previous_adjustment) > 1e-6)
        )
        reference = np.where(adjustment_changed, previous_close, previous_raw)
        valid = (
            present[time_id]
            & np.isfinite(reference)
            & (reference > 0)
            & np.isfinite(percentages[time_id])
        )
        if valid.any():
            up_price = numpy_limit_price(reference, percentages[time_id], up=True)
            down_price = numpy_limit_price(reference, percentages[time_id], up=False)
            up_locked[time_id, valid] = current_raw[valid] >= up_price[valid] - 0.005
            down_locked[time_id, valid] = current_raw[valid] <= down_price[valid] + 0.005
        previous_close[present[time_id]] = current_close[present[time_id]]
        previous_raw[present[time_id]] = current_raw[present[time_id]]
        previous_adjustment[present[time_id]] = current_adjustment[present[time_id]]
    return up_locked, down_locked


def build_observation_mask(
    trading_dates: tuple[date, ...],
    symbols: tuple[str, ...],
    observations: pl.DataFrame,
) -> np.ndarray:
    """Align unique sparse symbol/date observations to fixed research axes."""
    required = {"symbol", "date"}
    missing = required - set(observations.columns)
    if missing:
        raise ValueError(f"observations missing columns: {sorted(missing)}")
    if observations.select("symbol", "date").is_duplicated().any():
        raise ValueError("observations requires unique symbol/date rows")

    date_axis = pl.DataFrame({
        "date": trading_dates,
        "_time_id": pl.Series(range(len(trading_dates)), dtype=pl.UInt32),
    })
    symbol_axis = pl.DataFrame({
        "symbol": symbols,
        "_asset_id": pl.Series(range(len(symbols)), dtype=pl.UInt32),
    })
    aligned = (
        observations.select("symbol", "date")
        .join(date_axis, on="date", how="left")
        .join(symbol_axis, on="symbol", how="left")
    )
    outside = aligned.filter(
        pl.col("_time_id").is_null() | pl.col("_asset_id").is_null()
    )
    if outside.height:
        raise ValueError(
            f"observations outside the fixed axes: "
            f"{outside.select('symbol', 'date').head(5).rows()}"
        )
    result = np.zeros((len(trading_dates), len(symbols)), dtype=bool)
    result[
        aligned["_time_id"].to_numpy(),
        aligned["_asset_id"].to_numpy(),
    ] = True
    return result


def build_pit_float_share_mask(
    trading_dates: tuple[date, ...],
    symbols: tuple[str, ...],
    shares: pl.DataFrame,
) -> np.ndarray:
    """Mark dates where a positive PIT float-share observation is available."""
    shape = (len(trading_dates), len(symbols))
    required = {"symbol", "period_end", "float_shares"}
    missing = required - set(shares.columns)
    if missing:
        raise ValueError(f"share history missing columns: {sorted(missing)}")
    if shares.is_empty():
        return np.zeros(shape, dtype=bool)

    def date_expr(column: str) -> pl.Expr:
        return (
            pl.col(column).str.to_date(strict=False)
            if shares.schema[column] == pl.String
            else pl.col(column).cast(pl.Date, strict=False)
        )

    available = date_expr("period_end")
    if "announce_date" in shares.columns:
        available = date_expr("announce_date").fill_null(available)
    valid = (
        shares.select(
            pl.col("symbol").cast(pl.String),
            available.alias("available_date"),
            pl.col("float_shares").cast(pl.Float64, strict=False),
        )
        .filter(
            pl.col("available_date").is_not_null()
            & pl.col("float_shares").is_not_null()
            & (pl.col("float_shares") > 0)
        )
        .group_by("symbol")
        .agg(pl.col("available_date").min().alias("first_available_date"))
    )
    first_available = {
        str(row["symbol"]): row["first_available_date"]
        for row in valid.iter_rows(named=True)
    }
    date_values = np.asarray([value.toordinal() for value in trading_dates], dtype=np.int64)
    result = np.zeros(shape, dtype=bool)
    for asset_id, symbol in enumerate(symbols):
        first = first_available.get(symbol)
        if first is not None:
            result[:, asset_id] = date_values >= first.toordinal()
    return result


def build_research_universe(
    trading_dates: tuple[date, ...],
    symbols: tuple[str, ...],
    instruments: pl.DataFrame,
    st_history: pl.DataFrame,
    *,
    pit_valid: np.ndarray,
    seasoning_months: int = 6,
) -> ResearchUniverse:
    """Apply listing, delisting, PIT ST, seasoning, and PIT-data gates in order."""
    shape = (len(trading_dates), len(symbols))
    pit = np.asarray(pit_valid, dtype=bool)
    if pit.shape != shape:
        raise ValueError("pit_valid shape must match dates and symbols")
    if seasoning_months < 0:
        raise ValueError("seasoning_months must be non-negative")

    instrument_rows = _instrument_rows(instruments)
    date_values = np.asarray([value.toordinal() for value in trading_dates], dtype=np.int64)
    list_values = np.empty(len(symbols), dtype=np.int64)
    delist_values = np.empty(len(symbols), dtype=np.int64)
    seasoned_values = np.empty(len(symbols), dtype=np.int64)
    for asset_id, symbol in enumerate(symbols):
        try:
            list_date, delist_date = instrument_rows[symbol]
        except KeyError as exc:
            raise ValueError(f"missing instrument dates for {symbol}") from exc
        list_values[asset_id] = list_date.toordinal()
        delist_values[asset_id] = delist_date.toordinal() if delist_date else np.iinfo(np.int64).max
        seasoned_values[asset_id] = _add_months(list_date, seasoning_months).toordinal()

    listed = date_values[:, None] >= list_values[None, :]
    not_delisted = listed & (date_values[:, None] < delist_values[None, :])
    st_known, st_flag = _st_matrices(trading_dates, symbols, st_history)
    known = not_delisted & st_known
    not_st = known & ~st_flag
    seasoned = not_st & (date_values[:, None] >= seasoned_values[None, :])
    pit_gate = seasoned & pit
    eligible = pit_gate

    price_limit_pct = np.empty(shape, dtype=np.float64)
    for asset_id, symbol in enumerate(symbols):
        price_limit_pct[:, asset_id] = board_limit_pct(symbol)
    legacy_st = (
        st_known
        & st_flag
        & (price_limit_pct == MAIN_BOARD_LIMIT)
        & (date_values[:, None] < MAIN_BOARD_ST_LIMIT_CHANGE_DATE.toordinal())
    )
    price_limit_pct[legacy_st] = 0.05

    funnel = pl.DataFrame({
        "date": trading_dates,
        "listed": listed.sum(axis=1),
        "not_delisted": not_delisted.sum(axis=1),
        "st_known": known.sum(axis=1),
        "st_unknown": (not_delisted & ~st_known).sum(axis=1),
        "not_st": not_st.sum(axis=1),
        "seasoned": seasoned.sum(axis=1),
        "pit_valid": pit_gate.sum(axis=1),
        "eligible": eligible.sum(axis=1),
    })
    return ResearchUniverse(
        eligible=eligible,
        st_known=st_known,
        st_flag=st_flag,
        price_limit_pct=price_limit_pct,
        funnel=funnel,
    )


def _instrument_rows(instruments: pl.DataFrame) -> dict[str, tuple[date, date | None]]:
    required = {"symbol", "list_date", "delist_date"}
    missing = required - set(instruments.columns)
    if missing:
        raise ValueError(f"instruments missing columns: {sorted(missing)}")
    if instruments["symbol"].n_unique() != instruments.height:
        raise ValueError("instruments requires unique symbols")
    result: dict[str, tuple[date, date | None]] = {}
    for row in instruments.select(required).iter_rows(named=True):
        list_date = row["list_date"]
        delist_date = row["delist_date"]
        if list_date is None:
            raise ValueError(f"missing list_date for {row['symbol']}")
        if delist_date is not None and delist_date < list_date:
            raise ValueError(f"delist_date precedes list_date for {row['symbol']}")
        result[str(row["symbol"])] = (list_date, delist_date)
    return result


def _st_matrices(
    trading_dates: tuple[date, ...],
    symbols: tuple[str, ...],
    st_history: pl.DataFrame,
) -> tuple[np.ndarray, np.ndarray]:
    required = {"symbol", "effective_date", "st_flag"}
    missing = required - set(st_history.columns)
    if missing:
        raise ValueError(f"st_history missing columns: {sorted(missing)}")
    if st_history.select("symbol", "effective_date").is_duplicated().any():
        raise ValueError("st_history requires unique symbol/effective_date rows")

    date_values = np.asarray([value.toordinal() for value in trading_dates], dtype=np.int64)
    known = np.zeros((len(trading_dates), len(symbols)), dtype=bool)
    flags = np.zeros_like(known)
    grouped = st_history.sort("effective_date").partition_by("symbol", as_dict=True)
    for asset_id, symbol in enumerate(symbols):
        history = grouped.get((symbol,))
        if history is None or history.is_empty():
            continue
        effective = np.asarray(
            [value.toordinal() for value in history["effective_date"].to_list()],
            dtype=np.int64,
        )
        values = history["st_flag"].cast(pl.Boolean).to_numpy()
        indices = np.searchsorted(effective, date_values, side="right") - 1
        valid = indices >= 0
        known[valid, asset_id] = True
        flags[valid, asset_id] = values[indices[valid]]
    return known, flags


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    month_lengths = (31, 29 if _is_leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return date(year, month, min(value.day, month_lengths[month - 1]))


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
