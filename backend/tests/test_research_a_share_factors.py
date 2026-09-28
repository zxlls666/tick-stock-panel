"""20日反转与1个月异常换手的研究规格测试。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix, matrix_feature, validate_signal_matrix
from app.strategy.builtin._research_portfolio import (
    cross_sectional_residual,
    month_end_holding_weights,
    month_end_mask,
    overlapping_sleeve_weights,
    select_low_bucket,
)
from app.strategy.builtin.abnormal_turnover_1m_12m import MATRIX_STRATEGY as TURNOVER_STRATEGY
from app.strategy.builtin.short_term_reversal_20d import MATRIX_STRATEGY as REVERSAL_STRATEGY
from app.strategy.engine import StrategyEngine
from app.strategy.scoring import materialize_scoring_columns
from scripts.run_a_share_strategy_validation import (
    _cost_scenarios,
    _parameter_scenarios,
)

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"


def test_low_bucket_prefers_smaller_values_and_breaks_ties_by_asset_order():
    values = np.array([
        [0.2, np.nan, -0.1, -0.1],
        [np.nan, np.nan, np.nan, np.nan],
    ])

    selected = select_low_bucket(values, top_n=1)

    assert selected.tolist() == [[False, False, True, False], [False, False, False, False]]
    assert select_low_bucket(values, quantile=0.7)[0].tolist() == [False, False, True, True]


def test_five_sleeves_stack_and_keep_unformed_capital_in_cash():
    selected = np.array([
        [True, False],
        [False, True],
        [True, True],
    ])

    weights = overlapping_sleeve_weights(selected, n_sleeves=2, execution_lag=0)

    assert weights[0].tolist() == pytest.approx([0.5, 0.0])
    assert weights[1].tolist() == pytest.approx([0.5, 0.5])
    assert weights[2].tolist() == pytest.approx([0.25, 0.75])
    assert overlapping_sleeve_weights(selected, n_sleeves=5, execution_lag=1)[0].sum() == 0.0


def test_month_end_book_starts_the_next_trading_day_and_ignores_incomplete_tail():
    labels = ["2024-01-30", "2024-01-31", "2024-02-03", "2024-02-04"]
    selected = np.array([
        [True, False],
        [False, True],
        [True, False],
        [True, True],
    ])

    mask = month_end_mask(labels)
    weights = month_end_holding_weights(selected, mask)

    assert mask.tolist() == [False, True, False, False]
    assert weights[1].tolist() == pytest.approx([0.0, 0.0])
    assert weights[2].tolist() == pytest.approx([0.0, 1.0])
    assert weights[3].tolist() == pytest.approx([0.0, 1.0])


def test_size_residual_is_zero_on_an_exact_line_and_flags_the_outlier():
    control = np.array([[1.0, 2.0, 3.0, 4.0]])
    exact = np.array([[3.0, 5.0, 7.0, 9.0]])
    outlier = np.array([[3.0, 5.0, 7.0, 20.0]])

    assert cross_sectional_residual(exact, control) == pytest.approx(0.0, abs=1e-9)
    residual = cross_sectional_residual(outlier, control)
    assert residual[0, 3] > 1.0
    assert np.isnan(cross_sectional_residual(exact[:, :2], control[:, :2])).all()


def _panel(rows: list[dict]) -> object:
    return build_market_data_matrix(
        pl.DataFrame(rows),
        field_columns={"turnover_rate", "volume"},
    )


def test_reversal_signal_selects_the_lowest_20d_return():
    start = date(2023, 12, 1)
    symbols = ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"]
    terminal = [8.0, 9.0, 12.0, 15.0]
    rows = []
    for offset in range(70):
        current = start + timedelta(days=offset)
        for index, symbol in enumerate(symbols):
            close = terminal[index] if offset == 30 else 10.0
            rows.append({
                "symbol": symbol,
                "date": current,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000.0,
                "amount": 10000.0,
                "turnover_rate": 1.0,
            })
    market = _panel(rows)

    daily = REVERSAL_STRATEGY.compute_signals(market, {"top_n": 1, "month_end": False})
    monthly = REVERSAL_STRATEGY.compute_signals(market, {"top_n": 1, "month_end": True})

    validate_signal_matrix(daily, market.shape)
    assert daily.entry[30].tolist() == [1, 0, 0, 0]
    assert monthly.entry[30].tolist() == [1, 0, 0, 0]
    assert monthly.entry[31].tolist() == [0, 0, 0, 0]
    assert float(daily.score[30, 0]) > float(daily.score[30, 3])


def test_abnormal_turnover_matches_20_over_250_and_stays_null_until_warmup():
    turnover = np.linspace(1.0, 3.0, 260)
    rows = []
    start = date(2024, 1, 2)
    for offset, value in enumerate(turnover):
        close = 10.0
        rows.append({
            "symbol": "000001.SZ",
            "date": start + timedelta(days=offset),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1_000.0,
            "amount": 10_000.0,
            "turnover_rate": float(value),
        })
    frame = pl.DataFrame(rows)
    scored = materialize_scoring_columns(frame, ["abnormal_turnover_1m_12m"])
    market = _panel(rows)
    matrix_values = matrix_feature(market, "abnormal_turnover_1m_12m")[:, 0]
    column = scored["abnormal_turnover_1m_12m"].to_numpy()

    assert np.isnan(column[:249]).all()
    short = turnover[230:250].mean()
    long = turnover[:250].mean()
    assert column[249] == pytest.approx(short / long, rel=1e-6)
    assert matrix_values[249] == pytest.approx(column[249], rel=1e-5)
    assert np.isnan(matrix_values[:249]).all()

    flat = frame.with_columns(pl.lit(0.0).alias("turnover_rate"))
    zero = materialize_scoring_columns(flat, ["abnormal_turnover_1m_12m"])
    assert zero["abnormal_turnover_1m_12m"].is_null().all()


def test_turnover_signal_is_month_end_low_bucket_and_distinct_from_daily():
    start = date(2024, 1, 2)
    # 前 230 日用 base, 其后用 recent, 使第 250 个交易日起的 20/250 比值可区分。
    paths = ((4.0, 1.0), (1.0, 1.0), (1.0, 2.0), (1.0, 4.0))
    rows = []
    for offset in range(320):
        for path_index, (base, recent) in enumerate(paths):
            close = 10.0
            rows.append({
                "symbol": f"00000{path_index + 1}.SZ",
                "date": start + timedelta(days=offset),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000.0 * (path_index + 1),
                "amount": 10000.0,
                "turnover_rate": recent if offset >= 230 else base,
            })
    market = _panel(rows)
    monthly = TURNOVER_STRATEGY.compute_signals(market, {"top_n": 1, "month_end": True})
    daily = TURNOVER_STRATEGY.compute_signals(market, {"top_n": 1, "month_end": False})

    validate_signal_matrix(monthly, market.shape)
    ends = np.flatnonzero(month_end_mask(market.timestamp_labels))
    mature_ends = ends[ends >= 249]
    assert mature_ends.size > 0
    assert monthly.entry[mature_ends[0]].tolist() == [1, 0, 0, 0]
    assert int(monthly.entry.sum()) < int(daily.entry.sum())
    neutral = TURNOVER_STRATEGY.compute_signals(
        market, {"top_n": 1, "month_end": True, "neutralize_size": True}
    )
    validate_signal_matrix(neutral, market.shape)


def test_full_study_scenarios_change_one_assumption_at_a_time():
    costs = _cost_scenarios()
    assert [item.name for item in costs] == ["gross", "base", "10bps", "20bps", "40bps"]
    assert costs[1].commission_pct == pytest.approx(0.0002)
    assert costs[1].stamp_tax_pct == pytest.approx(0.0005)
    assert costs[1].slippage_bps == pytest.approx(5.0)

    reversal = _parameter_scenarios("reversal_five_sleeves")
    assert [(item.name, item.top_n, item.reversal_lookback) for item in reversal] == [
        ("top_n_10", 10, 20),
        ("top_n_30", 30, 20),
        ("lookback_10", 20, 10),
        ("lookback_30", 20, 30),
    ]
    turnover = _parameter_scenarios("abnormal_turnover_monthly_raw")
    assert [(item.name, item.turnover_short, item.turnover_long) for item in turnover] == [
        ("top_n_10", 20, 250),
        ("top_n_30", 20, 250),
        ("windows_15_200", 15, 200),
        ("windows_30_300", 30, 300),
    ]


def test_research_strategies_stay_out_of_the_public_catalog():
    engine = StrategyEngine(strategy_dirs=[BUILTIN_DIR])

    public = {item["id"] for item in engine.list_strategies()}
    research = {item["id"] for item in engine.list_strategies(include_research=True)}

    assert "short_term_reversal_20d" not in public
    assert "abnormal_turnover_1m_12m" not in public
    assert {"short_term_reversal_20d", "abnormal_turnover_1m_12m"} <= research
    assert engine.get("short_term_reversal_20d").meta["research_only"] is True
    assert engine.get("abnormal_turnover_1m_12m").meta["research_only"] is True
    assert engine.get("short_term_reversal_20d").max_hold_days is None
    assert engine.get("abnormal_turnover_1m_12m").matrix_strategy.required_warmup_bars({}) == 251
