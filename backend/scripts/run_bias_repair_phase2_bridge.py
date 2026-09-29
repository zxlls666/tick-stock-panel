#!/usr/bin/env python3
"""Run the Phase 2 PIT-ST, delisted-universe, and recovery waterfall."""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import MarketDataMatrix, matrix_feature, slice_market_data_matrix
from app.backtest.research_benchmarks import (
    active_performance_stats,
    align_active_returns,
    load_benchmark_returns,
)
from app.backtest.research_strict_pit import (
    instrument_float_share_vector,
    with_strict_pit_turnover,
)
from app.backtest.research_target_weights import simulate_target_weights
from app.backtest.research_universe import (
    ResearchUniverse,
    append_research_bars,
    build_delisting_mask,
    build_pit_float_share_mask,
    build_research_universe,
    mask_research_values,
    with_research_price_limits,
)
from app.backtest.research_universe_data import merge_research_instruments
from app.share_capital import load_share_history
from app.strategy.builtin._research_portfolio import (
    cross_sectional_residual,
    month_end_holding_weights,
    month_end_mask,
    select_low_bucket,
)
from app.tickflow.repository import DataStore, KlineRepository
from scripts.run_bias_repair_phase1_bridge import _abnormal_turnover, _feature_plan

GENERATION = "0b7900c875e347088914a263585b81e5"
CANDIDATES = (
    "reversal_monthly",
    "abnormal_turnover_monthly_raw",
    "abnormal_turnover_monthly_size_neutral",
)
BENCHMARK = "000985.SH"


def _weights(
    market: MarketDataMatrix,
    strategy: str,
    universe: ResearchUniverse | None,
) -> np.ndarray:
    eligible = universe.eligible if universe is not None else None
    if strategy == "reversal_monthly":
        values = matrix_feature(market, "momentum_20d")
        if eligible is not None:
            values = mask_research_values(values, eligible)
    else:
        values = _abnormal_turnover(market, 20, 250)
        if strategy == "abnormal_turnover_monthly_size_neutral":
            size = matrix_feature(market, "log_float_mv")
            if eligible is not None:
                size = mask_research_values(size, eligible)
                values = mask_research_values(values, eligible)
            values = cross_sectional_residual(values, size)
        elif eligible is not None:
            values = mask_research_values(values, eligible)
    selected = select_low_bucket(values, top_n=20)
    return month_end_holding_weights(selected, month_end_mask(market.timestamp_labels))


def _run(
    market: MarketDataMatrix,
    strategy: str,
    universe: ResearchUniverse | None,
    *,
    initial_capital: float,
    delistings: np.ndarray | None = None,
    recovery_rate: float = 1.0,
) -> tuple[dict[str, object], pl.DataFrame]:
    weights = _weights(market, strategy, universe)
    invested = np.flatnonzero(weights.sum(axis=1) > 0)
    if invested.size == 0:
        raise ValueError(f"{strategy} target weights never invest")
    start = int(invested[0])
    result = simulate_target_weights(
        slice_market_data_matrix(market, start, market.shape[0]),
        weights[start:],
        config=MatcherConfig(
            initial_capital=initial_capital,
            commission_pct=0.0002,
            stamp_tax_pct=0.0005,
            slippage_bps=5.0,
        ),
        delistings=None if delistings is None else delistings[start:],
        delisting_recovery_rate=recovery_rate,
    )
    daily = result.daily.with_columns(
        (pl.col("equity") / initial_capital).alias("nav")
    )
    return {
        "start": str(daily["date"][0]),
        "end": str(daily["date"][-1]),
        "trading_days": daily.height,
        **result.stats,
        **result.execution,
    }, daily


def _universe(
    market: MarketDataMatrix,
    instruments: pl.DataFrame,
    st_history: pl.DataFrame,
    shares: pl.DataFrame,
    strategy: str,
) -> ResearchUniverse:
    dates = tuple(date.fromisoformat(label[:10]) for label in market.timestamp_labels)
    observed = np.isfinite(market.close)
    pit_valid = observed
    if strategy.startswith("abnormal_turnover_"):
        pit_valid = observed & build_pit_float_share_mask(dates, market.symbols, shares)
    return build_research_universe(
        dates,
        market.symbols,
        instruments,
        st_history,
        pit_valid=pit_valid,
    )


def _attach_benchmark(
    row: dict[str, object],
    daily: pl.DataFrame,
    repo: KlineRepository,
    load_start: date,
    end: date,
) -> None:
    benchmark = load_benchmark_returns(repo, BENCHMARK, start=load_start, end=end)
    row.update(active_performance_stats(align_active_returns(daily, benchmark)))


def main() -> int:
    args = _parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"output already exists: {output}")
    output.mkdir(parents=True)

    repo = KlineRepository(DataStore())
    engine = BacktestEngine(repo)
    generation = engine.data_generation_await("stock")
    if generation != GENERATION:
        raise RuntimeError(f"stock generation drifted: {generation}")
    load_start = args.start - timedelta(days=500)
    production = engine.load_market_data_matrix_for_backtest(
        None,
        load_start,
        args.end,
        _feature_plan(),
        asset_type="stock",
        expected_generation=generation,
    )

    current = pl.read_parquet(args.current_instruments)
    delisted = pl.read_parquet(args.delisted_instruments)
    bars = pl.scan_parquet(args.delisted_daily / "**" / "*.parquet").collect()
    st_history = pl.read_parquet(args.st_history)
    shares = load_share_history(Path("data"))

    combined = append_research_bars(
        production,
        bars,
        delisted.select("symbol", "name"),
    )
    production_axis = pl.DataFrame({"symbol": production.symbols})
    combined_axis = pl.DataFrame({"symbol": combined.symbols})
    production_instruments = merge_research_instruments(
        current.join(production_axis, on="symbol", how="semi"),
        delisted.join(production_axis, on="symbol", how="semi"),
    )
    combined_instruments = merge_research_instruments(
        current.join(combined_axis, on="symbol", how="semi"),
        delisted.join(combined_axis, on="symbol", how="semi"),
    )

    strict_production, production_pit = with_strict_pit_turnover(
        production,
        historical_shares=shares,
        instrument_float_shares=instrument_float_share_vector(production.symbols, current),
        today=date.today(),
    )
    strict_combined, combined_pit = with_strict_pit_turnover(
        combined,
        historical_shares=shares,
        instrument_float_shares=instrument_float_share_vector(combined.symbols, current),
        today=date.today(),
    )
    if production_pit["current_fallback_count"] or combined_pit["current_fallback_count"]:
        raise RuntimeError("strict PIT unexpectedly used current float-share fallback")

    dates = tuple(date.fromisoformat(label[:10]) for label in combined.timestamp_labels)
    delisting_mask = build_delisting_mask(dates, combined.symbols, combined_instruments)
    rows: list[dict[str, object]] = []
    daily_frames: list[pl.DataFrame] = []

    for strategy in CANDIDATES:
        production_universe = _universe(
            strict_production,
            production_instruments,
            st_history,
            shares,
            strategy,
        )
        combined_universe = _universe(
            strict_combined,
            combined_instruments,
            st_history,
            shares,
            strategy,
        )
        limited_production = with_research_price_limits(
            strict_production,
            production_universe,
        )
        limited_combined = with_research_price_limits(
            strict_combined,
            combined_universe,
        )
        stages = (
            ("strict_pit", strict_production, None, None, 1.0),
            ("pit_st", limited_production, production_universe, None, 1.0),
            ("delisted_recovery_100", limited_combined, combined_universe, delisting_mask, 1.0),
            ("delisted_recovery_70", limited_combined, combined_universe, delisting_mask, 0.7),
            ("delisted_recovery_0", limited_combined, combined_universe, delisting_mask, 0.0),
        )
        for stage, market, universe, delistings, recovery_rate in stages:
            row, daily = _run(
                market,
                strategy,
                universe,
                initial_capital=args.initial_capital,
                delistings=delistings,
                recovery_rate=recovery_rate,
            )
            row.update({
                "strategy": strategy,
                "stage": stage,
                "recovery_rate": recovery_rate,
            })
            _attach_benchmark(row, daily, repo, load_start, args.end)
            rows.append(row)
            daily_frames.append(daily.with_columns(
                pl.lit(strategy).alias("strategy"),
                pl.lit(stage).alias("stage"),
            ))

    summary = pl.DataFrame(rows).sort("strategy", "stage")
    summary.write_parquet(output / "summary.parquet")
    pl.concat(daily_frames, how="vertical_relaxed").write_parquet(output / "daily.parquet")
    manifest = {
        "run_type": "phase2_bias_repair_waterfall",
        "generation": generation,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "production_shape": list(production.shape),
        "combined_shape": list(combined.shape),
        "production_pit": production_pit,
        "combined_pit": combined_pit,
        "strategies": list(CANDIDATES),
        "stages": [
            "strict_pit",
            "pit_st",
            "delisted_recovery_100",
            "delisted_recovery_70",
            "delisted_recovery_0",
        ],
        "sources": {
            "delisted_instruments": str(args.delisted_instruments),
            "delisted_daily": str(args.delisted_daily),
            "st_history": str(args.st_history),
            "shares": "data/financials/shares/part.parquet",
        },
        "limitations": [
            "BaoStock covers Shanghai/Shenzhen delisted bars; Beijing ST remains unknown and fail-closed.",
            "Daily-bar execution still lacks ADV capacity, market impact, and queue calibration.",
            "Results remain research_only.",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    _write_report(output, summary)
    print(output)
    return 0


def _write_report(output: Path, summary: pl.DataFrame) -> None:
    lines = [
        "# Phase 2 bias-repair waterfall",
        "",
        "All stages rerun the account path. No result is produced by post-hoc subtraction.",
        "",
        "| strategy | stage | total return | annualized | Sharpe | max drawdown | CSI All excess | delisted positions |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.iter_rows(named=True):
        lines.append(
            "| {strategy} | {stage} | {total_return:.4f} | {annual_return:.4f} | "
            "{sharpe:.3f} | {max_drawdown:.4f} | {excess_total_return:.4f} | "
            "{delisted_position_count} |".format(**row)
        )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 9, 9))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 24))
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--current-instruments", type=Path, default=Path("data/instruments/instruments.parquet"))
    parser.add_argument("--delisted-instruments", type=Path, default=Path("data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/delisted_instruments.parquet"))
    parser.add_argument("--delisted-daily", type=Path, default=Path("data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/delisted_daily"))
    parser.add_argument("--st-history", type=Path, default=Path("data/research/a_share_universe/20260928T-phase2-st-history-v1/st_history.parquet"))
    parser.add_argument("--output", type=Path, default=Path("data/research/a_share_strategy_validation/20260928T-phase2-waterfall-v1"))
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    if args.initial_capital <= 0:
        parser.error("--initial-capital must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
