"""Phase 2 研究股票池数据标准化。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.backtest.research_universe_data import (
    apply_baostock_forward_adjustment,
    merge_research_instruments,
    normalize_baostock_daily,
    normalize_baostock_instruments,
    st_changes_from_daily,
)


def test_merge_research_instruments_overlays_delisting_dates_and_appends_missing():
    current = pl.DataFrame({
        "symbol": ["000001.SZ", "000002.SZ"],
        "name": ["当前甲", "当前乙"],
        "exchange": ["SZ", "SZ"],
        "listing_date": ["1991-04-03", "1991-01-29"],
    })
    delisted = pl.DataFrame({
        "symbol": ["000002.SZ", "600001.SH"],
        "name": ["退市乙", "退市丙"],
        "exchange": ["SZ", "SH"],
        "list_date": [date(1991, 1, 30), date(2000, 1, 1)],
        "delist_date": [date(2024, 1, 5), date(2024, 2, 1)],
        "source": ["baostock:query_stock_basic"] * 2,
    })

    result = merge_research_instruments(current, delisted)

    assert result.to_dicts() == [
        {
            "symbol": "000001.SZ",
            "name": "当前甲",
            "exchange": "SZ",
            "list_date": date(1991, 4, 3),
            "delist_date": None,
            "instrument_source": "current_instruments",
        },
        {
            "symbol": "000002.SZ",
            "name": "退市乙",
            "exchange": "SZ",
            "list_date": date(1991, 1, 30),
            "delist_date": date(2024, 1, 5),
            "instrument_source": "baostock:query_stock_basic",
        },
        {
            "symbol": "600001.SH",
            "name": "退市丙",
            "exchange": "SH",
            "list_date": date(2000, 1, 1),
            "delist_date": date(2024, 2, 1),
            "instrument_source": "baostock:query_stock_basic",
        },
    ]


def test_merge_research_instruments_rejects_duplicate_symbols():
    current = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "name": ["甲", "甲"],
        "exchange": ["SZ", "SZ"],
        "listing_date": ["1991-04-03", "1991-04-03"],
    })
    delisted = pl.DataFrame(schema={
        "symbol": pl.String,
        "name": pl.String,
        "exchange": pl.String,
        "list_date": pl.Date,
        "delist_date": pl.Date,
        "source": pl.String,
    })

    with pytest.raises(ValueError, match="current instruments requires unique symbols"):
        merge_research_instruments(current, delisted)


def test_normalize_instruments_filters_a_shares_and_sets_load_status():
    raw = pl.DataFrame({
        "code": ["sh.600001", "sz.000002", "sh.000001"],
        "code_name": ["退市甲", "乙", "上证指数"],
        "ipoDate": ["2000-01-01", "2010-01-01", "1991-07-15"],
        "outDate": ["2024-01-05", "", ""],
        "type": ["1", "1", "2"],
        "status": ["0", "1", "1"],
    })

    result = normalize_baostock_instruments(raw)

    assert result.to_dicts() == [{
        "symbol": "600001.SH",
        "name": "退市甲",
        "exchange": "SH",
        "list_date": date(2000, 1, 1),
        "delist_date": date(2024, 1, 5),
        "delist_reason": None,
        "load_status": "pending",
        "bar_count": 0,
        "error": None,
        "source": "baostock:query_stock_basic",
    }]


def test_normalize_daily_drops_synthetic_suspensions_and_validates_ohlc():
    raw = pl.DataFrame({
        "date": ["2024-01-02", "2024-01-03"],
        "code": ["sh.600001", "sh.600001"],
        "open": ["10.0", "10.0"],
        "high": ["10.5", "10.0"],
        "low": ["9.8", "10.0"],
        "close": ["10.2", "10.0"],
        "volume": ["1000", ""],
        "amount": ["10200", ""],
        "tradestatus": ["1", "0"],
        "isST": ["1", "1"],
    })

    bars = normalize_baostock_daily(raw)

    assert bars.to_dicts() == [{
        "symbol": "600001.SH",
        "date": date(2024, 1, 2),
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "volume": 1000.0,
        "amount": 10200.0,
        "adj_factor": 1.0,
        "is_st": True,
        "source": "baostock:query_history_k_data_plus:unadjusted",
    }]

    bad = raw.head(1).with_columns(pl.lit("9.0").alias("high"))
    with pytest.raises(ValueError, match="OHLC"):
        normalize_baostock_daily(bad)


def test_apply_forward_adjustment_preserves_raw_prices_and_rejects_missing_dates():
    raw = pl.DataFrame({
        "symbol": ["600001.SH", "600001.SH"],
        "date": [date(2024, 1, 2), date(2024, 1, 3)],
        "open": [10.0, 11.0],
        "high": [10.5, 11.5],
        "low": [9.5, 10.5],
        "close": [10.0, 11.0],
        "volume": [1000.0, 2000.0],
        "amount": [10_000.0, 22_000.0],
        "adj_factor": [1.0, 1.0],
        "source": ["raw", "raw"],
    })
    adjusted = pl.DataFrame({
        "date": ["2024-01-02", "2024-01-03"],
        "code": ["sh.600001", "sh.600001"],
        "close": ["5.0", "11.0"],
        "tradestatus": ["1", "1"],
    })

    result = apply_baostock_forward_adjustment(raw, adjusted)

    assert result.select(
        "date", "open", "high", "low", "close", "volume",
        "raw_close", "raw_high", "raw_low", "adj_factor",
    ).rows() == [
        (date(2024, 1, 2), 5.0, 5.25, 4.75, 5.0, 10.0, 10.0, 10.5, 9.5, 0.5),
        (date(2024, 1, 3), 11.0, 11.5, 10.5, 11.0, 20.0, 11.0, 11.5, 10.5, 1.0),
    ]
    assert result["source"].unique().to_list() == [
        "baostock:query_history_k_data_plus:forward_adjusted"
    ]

    with pytest.raises(ValueError, match="missing forward-adjusted dates"):
        apply_baostock_forward_adjustment(raw, adjusted.head(1))


def test_st_changes_compress_adjacent_states_and_add_known_non_st_start():
    bars = pl.DataFrame({
        "symbol": ["600001.SH"] * 4,
        "date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)],
        "is_st": [False, False, True, False],
    })

    result = st_changes_from_daily(bars)

    assert result.select("effective_date", "st_flag", "st_type").rows() == [
        (date(2024, 1, 2), False, "normal"),
        (date(2024, 1, 4), True, "ST"),
        (date(2024, 1, 5), False, "normal"),
    ]
    assert result["source"].to_list() == ["baostock:daily_isST"] * 3
