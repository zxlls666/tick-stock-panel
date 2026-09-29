from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from scripts.run_a_share_oos_observer import (
    account_scenarios,
    completed_month_end_mask,
    freeze_oos_month_ends,
    manifest_covered_through,
    monthly_account_metrics,
    observation_path,
    oos_account_stats,
    require_calendar_horizon,
    require_instrument_snapshot,
    require_market_completeness,
    require_source_coverage,
    restrict_bars_to_market_axis,
    signal_overlap,
    signal_rows,
    started_account_window,
    target_rows_with_pending,
    warmup_start_index,
)


def test_completed_month_end_mask_uses_calendar_without_future_market_bar() -> None:
    labels = ("2026-09-28", "2026-09-29", "2026-09-30")
    calendar = (
        date(2026, 9, 28),
        date(2026, 9, 29),
        date(2026, 9, 30),
        date(2026, 10, 9),
    )

    assert np.array_equal(
        completed_month_end_mask(labels, calendar, as_of=date(2026, 9, 30)),
        np.array([False, False, True]),
    )
    assert np.array_equal(
        completed_month_end_mask(labels, calendar, as_of=date(2026, 9, 29)),
        np.array([False, False, False]),
    )


def test_calendar_must_reach_the_next_month() -> None:
    with pytest.raises(RuntimeError, match="calendar does not reach beyond 2026-09"):
        require_calendar_horizon(
            (date(2026, 9, 29), date(2026, 9, 30)),
            date(2026, 9, 29),
        )

    require_calendar_horizon(
        (date(2026, 9, 30), date(2026, 10, 9)),
        date(2026, 9, 29),
    )


def test_oos_month_ends_do_not_inherit_pre_freeze_positions() -> None:
    labels = ("2026-08-31", "2026-09-24", "2026-09-30")
    month_ends = np.array([True, False, True])

    assert np.array_equal(
        freeze_oos_month_ends(labels, month_ends, oos_start=date(2026, 9, 25)),
        np.array([False, False, True]),
    )


def test_manifest_coverage_uses_declared_end_not_last_change_row(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"end": "2026-09-30"}), encoding="utf-8")

    assert manifest_covered_through(manifest) == date(2026, 9, 30)


def test_source_coverage_fails_closed_before_as_of() -> None:
    with pytest.raises(RuntimeError, match="ST history only covers through 2026-09-24"):
        require_source_coverage("ST history", date(2026, 9, 24), date(2026, 9, 28))

    require_source_coverage("ST history", date(2026, 9, 28), date(2026, 9, 28))


def test_instrument_snapshot_must_match_as_of() -> None:
    with pytest.raises(RuntimeError, match="instrument snapshot is 2026-09-29"):
        require_instrument_snapshot(date(2026, 9, 29), date(2026, 9, 28))

    require_instrument_snapshot(date(2026, 9, 28), date(2026, 9, 28))


def test_market_completeness_rejects_partial_latest_partition() -> None:
    close = np.ones((21, 100), dtype=float)
    close[-1, 40:] = np.nan

    with pytest.raises(RuntimeError, match="latest market coverage is incomplete"):
        require_market_completeness(close)

    close[-1, 40:] = 1.0
    require_market_completeness(close)


def test_restrict_bars_to_market_axis_discards_older_delisted_history() -> None:
    bars = pl.DataFrame({
        "date": [date(2020, 9, 9), date(2025, 5, 13), date(2026, 9, 29)],
        "symbol": ["A", "A", "A"],
    })

    restricted = restrict_bars_to_market_axis(
        bars,
        ("2025-05-13", "2026-09-29"),
    )

    assert restricted["date"].to_list() == [date(2025, 5, 13), date(2026, 9, 29)]


def test_account_scenarios_freeze_primary_and_stress_parameters() -> None:
    scenarios = account_scenarios()

    assert [(item.name, item.strategy) for item in scenarios] == [
        ("raw_primary", "abnormal_turnover_monthly_raw"),
        ("raw_stress", "abnormal_turnover_monthly_raw"),
        ("size_neutral_primary", "abnormal_turnover_monthly_size_neutral"),
        ("size_neutral_stress", "abnormal_turnover_monthly_size_neutral"),
    ]
    assert scenarios[0].initial_capital == 1_000_000.0
    assert scenarios[0].max_adv_participation is None
    assert scenarios[0].impact_eta == 0.0
    assert scenarios[0].queue_proxy == "base"
    assert scenarios[1].initial_capital == 10_000_000.0
    assert scenarios[1].max_adv_participation == 0.01
    assert scenarios[1].impact_eta == 0.50
    assert scenarios[1].queue_proxy == "conservative"


def test_warmup_start_keeps_capacity_history_before_cash_only_oos() -> None:
    labels = tuple(f"2026-09-{day:02d}" for day in range(1, 31))

    assert warmup_start_index(labels, date(2026, 9, 25), bars=20) == 4


def test_oos_stats_exclude_cash_only_warmup_days() -> None:
    daily = pl.DataFrame({
        "date": ["2026-09-24", "2026-09-28", "2026-09-29"],
        "equity": [100.0, 100.0, 110.0],
        "daily_return": [0.0, 0.0, 0.1],
        "drawdown": [0.0, 0.0, 0.0],
        "one_way_turnover": [0.0, 0.0, 0.5],
        "trading_cost": [0.0, 0.0, 1.0],
        "positions": [0, 0, 2],
        "cash_weight": [1.0, 1.0, 0.0],
    })

    stats = oos_account_stats(daily, initial_capital=100.0, oos_start=date(2026, 9, 25))

    assert stats["trading_days"] == 2
    assert stats["total_return"] == pytest.approx(0.1)
    assert stats["trading_cost"] == pytest.approx(1.0)


def test_unstarted_cash_account_has_no_active_performance_window() -> None:
    daily = pl.DataFrame({
        "date": ["2026-09-28", "2026-09-29"],
        "target_exposure": [0.0, 0.0],
        "daily_return": [0.0, 0.0],
    })

    assert started_account_window(daily).is_empty()


def test_monthly_metrics_and_signal_overlap_are_auditable() -> None:
    daily = pl.DataFrame({
        "date": ["2026-09-28", "2026-09-29", "2026-10-09"],
        "daily_return": [0.01, -0.01, 0.02],
        "trading_cost": [1.0, 2.0, 3.0],
        "one_way_turnover": [0.0, 0.5, 0.2],
        "blocked_buy_count": [0, 1, 0],
        "blocked_sell_count": [0, 0, 1],
    })
    benchmark = pl.DataFrame({
        "date": ["2026-09-28", "2026-09-29", "2026-10-09"],
        "benchmark_return": [0.0, 0.0, 0.01],
    })
    metrics = monthly_account_metrics(daily, benchmark, "raw_primary")
    assert metrics.select("month", "account").rows() == [
        ("2026-09", "raw_primary"),
        ("2026-10", "raw_primary"),
    ]

    signals = pl.DataFrame({
        "strategy": [
            "abnormal_turnover_monthly_raw",
            "abnormal_turnover_monthly_raw",
            "abnormal_turnover_monthly_size_neutral",
            "abnormal_turnover_monthly_size_neutral",
        ],
        "signal_date": [date(2026, 9, 30)] * 4,
        "symbol": ["A", "B", "B", "C"],
    })
    overlap = signal_overlap(signals)
    assert overlap.select("intersection", "union", "jaccard").row(0) == (1, 3, pytest.approx(1 / 3))


def test_signal_rows_emit_pending_final_month_end_targets() -> None:
    values = np.array([
        [3.0, 1.0, 2.0],
        [2.0, 3.0, 1.0],
    ])

    rows = signal_rows(
        labels=("2026-09-29", "2026-09-30"),
        symbols=("A", "B", "C"),
        names=("Alpha", "Beta", "Gamma"),
        strategy="abnormal_turnover_monthly_raw",
        values=values,
        month_ends=np.array([False, True]),
        trading_calendar=(date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 9)),
        oos_start=date(2026, 9, 25),
        top_n=2,
    )

    assert rows.select("execution_date", "symbol", "rank", "target_weight").rows() == [
        (date(2026, 10, 9), "C", 1, 0.5),
        (date(2026, 10, 9), "A", 2, 0.5),
    ]
    pending = target_rows_with_pending(pl.DataFrame(schema={
        "strategy": pl.String,
        "date": pl.Date,
        "symbol": pl.String,
        "target_weight": pl.Float64,
    }), rows)
    assert pending.select("date", "symbol", "target_weight").rows() == [
        (date(2026, 10, 9), "A", 0.5),
        (date(2026, 10, 9), "C", 0.5),
    ]


def test_observation_path_is_date_scoped_and_refuses_overwrite(tmp_path: Path) -> None:
    path = observation_path(tmp_path, date(2026, 9, 30))
    assert path == tmp_path / "as_of=2026-09-30"

    path.mkdir()
    with pytest.raises(FileExistsError, match="observation already exists"):
        observation_path(tmp_path, date(2026, 9, 30))
