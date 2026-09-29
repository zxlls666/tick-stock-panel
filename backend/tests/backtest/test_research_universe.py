"""研究逐日股票池。"""
from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix
from app.backtest.research_universe import (
    ResearchUniverse,
    append_research_bars,
    build_delisting_mask,
    build_observation_mask,
    build_pit_float_share_mask,
    build_research_universe,
    mask_research_values,
    with_research_price_limits,
)


def test_append_research_bars_adds_disjoint_assets_without_mutating_market():
    market = build_market_data_matrix(pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "name": ["当前", "当前"],
        "date": [date(2024, 1, 2), date(2024, 1, 3)],
        "open": [10.0, 10.1],
        "high": [10.2, 10.3],
        "low": [9.9, 10.0],
        "close": [10.1, 10.2],
        "raw_close": [10.1, 10.2],
        "volume": [1000.0, 1100.0],
        "turnover_rate": [1.0, 1.1],
    }), field_columns={"raw_close", "turnover_rate"})
    bars = pl.DataFrame({
        "symbol": ["600001.SH", "600001.SH"],
        "date": [date(2024, 1, 2), date(2024, 1, 3)],
        "open": [5.0, 5.1],
        "high": [5.2, 5.3],
        "low": [4.9, 5.0],
        "close": [5.1, 5.2],
        "raw_close": [10.2, 10.4],
        "volume": [2000.0, 2100.0],
    })
    instruments = pl.DataFrame({
        "symbol": ["600001.SH"],
        "name": ["退市甲"],
    })

    combined = append_research_bars(market, bars, instruments)

    assert market.shape == (2, 1)
    assert combined.shape == (2, 2)
    assert combined.symbols == ("000001.SZ", "600001.SH")
    assert combined.names == ("当前", "退市甲")
    np.testing.assert_allclose(combined.close[:, 1], [5.1, 5.2])
    np.testing.assert_allclose(combined.field("raw_close")[:, 1], [10.2, 10.4])
    assert np.isnan(combined.field("turnover_rate")[:, 1]).all()

    with pytest.raises(ValueError, match="overlap production assets"):
        append_research_bars(market, bars.with_columns(pl.lit("000001.SZ").alias("symbol")), instruments)
    with pytest.raises(ValueError, match="outside market date axis"):
        append_research_bars(
            market,
            bars.with_columns(
                pl.when(pl.int_range(pl.len()) == 0).then(date(2024, 1, 4))
                .otherwise(pl.col("date")).alias("date")
            ),
            instruments,
        )


def test_delisting_mask_stays_true_from_delist_date_onward():
    mask = build_delisting_mask(
        (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)),
        ("600001.SH", "000001.SZ"),
        pl.DataFrame({
            "symbol": ["600001.SH", "000001.SZ"],
            "delist_date": [date(2024, 1, 3), None],
        }),
    )

    assert mask.tolist() == [
        [False, False],
        [True, False],
        [True, False],
    ]


def test_pit_float_share_mask_starts_on_announcement_date_and_ignores_invalid_rows():
    dates = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    shares = pl.DataFrame({
        "symbol": ["600001.SH", "600001.SH", "000001.SZ"],
        "period_end": ["2023-12-31", "2024-01-04", "2023-12-31"],
        "announce_date": ["2024-01-03", "2024-01-04", "2024-01-02"],
        "float_shares": [100.0, 0.0, None],
    })

    result = build_pit_float_share_mask(
        dates,
        ("600001.SH", "000001.SZ"),
        shares,
    )

    assert result.tolist() == [
        [False, False],
        [True, False],
        [True, False],
    ]


def test_mask_research_values_nulls_ineligible_cells_without_mutating_input():
    values = np.array([[1.0, 2.0], [3.0, 4.0]])
    eligible = np.array([[True, False], [False, True]])

    masked = mask_research_values(values, eligible)

    np.testing.assert_allclose(masked, [[1.0, np.nan], [np.nan, 4.0]], equal_nan=True)
    np.testing.assert_allclose(values, [[1.0, 2.0], [3.0, 4.0]])


def test_research_price_limits_recompute_locks_without_mutating_market():
    market = build_market_data_matrix(pl.DataFrame({
        "symbol": ["600001.SH", "600001.SH"],
        "name": ["正常名称", "正常名称"],
        "date": [date(2024, 1, 2), date(2024, 1, 3)],
        "open": [10.0, 10.5],
        "high": [10.0, 10.5],
        "low": [10.0, 10.5],
        "close": [10.0, 10.5],
        "raw_close": [10.0, 10.5],
        "volume": [1000.0, 1000.0],
    }), field_columns={"raw_close"})
    universe = ResearchUniverse(
        eligible=np.ones((2, 1), dtype=bool),
        st_known=np.ones((2, 1), dtype=bool),
        st_flag=np.ones((2, 1), dtype=bool),
        price_limit_pct=np.full((2, 1), 0.05),
        funnel=pl.DataFrame({"date": [date(2024, 1, 2), date(2024, 1, 3)]}),
    )

    rewritten = with_research_price_limits(market, universe)

    assert market.limit_up_locked[:, 0].tolist() == [0, 0]
    assert rewritten.limit_up_locked[:, 0].tolist() == [0, 1]
    np.testing.assert_allclose(rewritten.field("price_limit_pct"), [[0.05], [0.05]])


def test_observation_mask_aligns_sparse_rows_to_fixed_axes():
    result = build_observation_mask(
        (date(2024, 1, 2), date(2024, 1, 3)),
        ("000001.SZ", "600001.SH"),
        pl.DataFrame({
            "date": [date(2024, 1, 3), date(2024, 1, 2)],
            "symbol": ["000001.SZ", "600001.SH"],
        }),
    )

    assert result.tolist() == [[False, True], [True, False]]


def test_observation_mask_rejects_duplicate_or_off_axis_rows():
    duplicate = pl.DataFrame({
        "date": [date(2024, 1, 2), date(2024, 1, 2)],
        "symbol": ["000001.SZ", "000001.SZ"],
    })
    with np.testing.assert_raises_regex(ValueError, "unique symbol/date"):
        build_observation_mask(
            (date(2024, 1, 2),),
            ("000001.SZ",),
            duplicate,
        )

    with np.testing.assert_raises_regex(ValueError, "outside the fixed axes"):
        build_observation_mask(
            (date(2024, 1, 2),),
            ("000001.SZ",),
            pl.DataFrame({
                "date": [date(2024, 1, 2)],
                "symbol": ["600001.SH"],
            }),
        )


def test_universe_filters_in_fixed_order_and_fails_closed_on_unknown_st():
    dates = (date(2024, 1, 2), date(2024, 7, 2), date(2025, 1, 2))
    symbols = ("000001.SZ", "000002.SZ", "000003.SZ")
    instruments = pl.DataFrame({
        "symbol": symbols,
        "list_date": [date(2023, 1, 1), date(2024, 4, 1), date(2023, 1, 1)],
        "delist_date": [None, None, date(2024, 12, 31)],
    })
    st_history = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ", "000002.SZ", "000003.SZ"],
        "effective_date": [date(2024, 1, 1), date(2024, 8, 1), date(2024, 4, 1), date(2023, 1, 1)],
        "st_flag": [True, False, False, False],
        "st_type": ["ST", "normal", "normal", "normal"],
        "name": ["ST甲", "甲", "乙", "丙"],
        "source": ["direct_st_history"] * 4,
        "source_date": [date(2024, 1, 1), date(2024, 8, 1), date(2024, 4, 1), date(2023, 1, 1)],
    })
    pit_valid = np.array([
        [True, True, True],
        [True, True, True],
        [False, True, True],
    ])

    result = build_research_universe(
        dates,
        symbols,
        instruments,
        st_history,
        pit_valid=pit_valid,
        seasoning_months=6,
    )

    assert result.eligible.tolist() == [
        [False, False, True],
        [False, False, True],
        [False, True, False],
    ]
    assert result.st_known.tolist() == [
        [True, False, True],
        [True, True, True],
        [True, True, True],
    ]
    assert result.st_flag.tolist() == [
        [True, False, False],
        [True, False, False],
        [False, False, False],
    ]
    assert result.funnel.select(
        "listed",
        "not_delisted",
        "st_known",
        "not_st",
        "seasoned",
        "pit_valid",
        "eligible",
    ).rows() == [
        (2, 2, 2, 1, 1, 1, 1),
        (3, 3, 3, 2, 1, 1, 1),
        (3, 2, 2, 2, 2, 1, 1),
    ]


def test_unknown_st_state_is_excluded_and_counted():
    result = build_research_universe(
        (date(2024, 7, 1),),
        ("000001.SZ", "000002.SZ"),
        pl.DataFrame({
            "symbol": ["000001.SZ", "000002.SZ"],
            "list_date": [date(2020, 1, 1), date(2020, 1, 1)],
            "delist_date": [None, None],
        }),
        pl.DataFrame({
            "symbol": ["000001.SZ"],
            "effective_date": [date(2020, 1, 1)],
            "st_flag": [False],
        }),
        pit_valid=np.ones((1, 2), dtype=bool),
    )

    assert result.eligible.tolist() == [[True, False]]
    assert result.st_known.tolist() == [[True, False]]
    assert result.funnel["st_unknown"][0] == 1


def test_st_limit_pct_uses_pit_status_not_current_name():
    result = build_research_universe(
        (date(2024, 7, 1), date(2025, 7, 1)),
        ("600001.SH",),
        pl.DataFrame({
            "symbol": ["600001.SH"],
            "list_date": [date(2000, 1, 1)],
            "delist_date": [None],
        }),
        pl.DataFrame({
            "symbol": ["600001.SH", "600001.SH"],
            "effective_date": [date(2024, 1, 1), date(2025, 1, 1)],
            "st_flag": [True, False],
        }),
        pit_valid=np.ones((2, 1), dtype=bool),
    )

    assert result.price_limit_pct[:, 0].tolist() == [0.05, 0.10]
