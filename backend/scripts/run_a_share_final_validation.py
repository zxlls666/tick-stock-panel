#!/usr/bin/env python3
"""Run the Phase 4 final A-share research publication gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
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
from app.backtest.research_strict_pit import instrument_float_share_vector, with_strict_pit_turnover
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
from scripts.run_a_share_strategy_validation import CostScenario, ParameterScenario
from scripts.run_bias_repair_phase1_bridge import _abnormal_turnover, _feature_plan
from scripts.run_bias_repair_phase2_bridge import BENCHMARK, GENERATION

STRATEGIES = (
    "reversal_monthly",
    "abnormal_turnover_monthly_raw",
    "abnormal_turnover_monthly_size_neutral",
)
COSTS = (
    CostScenario("base", 0.0002, 0.0005, 5.0),
    CostScenario("20bps", 0.0, 0.0, 20.0),
    CostScenario("40bps", 0.0, 0.0, 40.0),
)
_GATE_COLUMNS = (
    "phase2_direction_positive",
    "phase2_excess_positive",
    "phase2_ir_positive",
    "extreme_recovery_positive",
    "cost_20bps_positive",
    "cost_40bps_positive",
    "subperiod_gate",
    "parameter_gate",
    "capacity_10m_gate",
    "year_contribution_gate",
    "unknowns_disclosed",
)


def build_final_verdicts(gates: pl.DataFrame) -> pl.DataFrame:
    """Assign the final verdict only when every frozen gate passes."""
    missing = {"strategy", *_GATE_COLUMNS} - set(gates.columns)
    if missing:
        raise ValueError(f"final gates missing columns: {sorted(missing)}")
    passed = pl.all_horizontal(pl.col(column) for column in _GATE_COLUMNS)
    return gates.with_columns(
        pl.when(passed)
        .then(pl.lit("bias_reduced_candidate"))
        .otherwise(pl.lit("pause"))
        .alias("verdict")
    )


def build_bias_waterfall(
    phase1: dict[str, list[dict[str, object]]],
    phase2: pl.DataFrame,
    phase3: pl.DataFrame,
) -> pl.DataFrame:
    """Combine frozen Phase 1-3 outputs into one auditable bias waterfall."""
    rows: list[dict[str, object]] = []
    strategies = [str(row["strategy"]) for row in phase1["legacy"]]
    for strategy in strategies:
        for stage in ("legacy", "strict_pit"):
            source = next(row for row in phase1[stage] if row["strategy"] == strategy)
            benchmark = source["benchmarks"][BENCHMARK]
            rows.append({
                "strategy": strategy,
                "stage": stage,
                "total_return": source["total_return"],
                "excess_total_return": benchmark["excess_total_return"],
                "information_ratio": benchmark["information_ratio"],
            })
        for stage in ("pit_st", "delisted_recovery_70"):
            selected = phase2.filter(
                (pl.col("strategy") == strategy) & (pl.col("stage") == stage)
            )
            if selected.height != 1:
                raise ValueError(f"phase2 waterfall row missing: {strategy} {stage}")
            row = selected.row(0, named=True)
            rows.append({
                "strategy": strategy,
                "stage": stage,
                "total_return": row["total_return"],
                "excess_total_return": row["excess_total_return"],
                "information_ratio": row["information_ratio"],
            })
        capacity = phase3.filter(
            (pl.col("strategy") == strategy)
            & (pl.col("scenario") == "adv_1pct_eta_0.50_queue_conservative")
            & (pl.col("initial_capital") == 10_000_000.0)
        )
        if capacity.height:
            row = capacity.row(0, named=True)
            rows.append({
                "strategy": strategy,
                "stage": "capacity_10m_conservative",
                "total_return": row["total_return"],
                "excess_total_return": row["excess_total_return"],
                "information_ratio": row["information_ratio"],
            })
    order = {
        "legacy": 0,
        "strict_pit": 1,
        "pit_st": 2,
        "delisted_recovery_70": 3,
        "capacity_10m_conservative": 4,
    }
    return (
        pl.DataFrame(rows)
        .with_columns(pl.col("stage").replace_strict(order).alias("_order"))
        .sort("strategy", "_order")
        .drop("_order")
    )


def _weights(
    market: MarketDataMatrix,
    strategy: str,
    universe: ResearchUniverse,
    params: ParameterScenario,
) -> np.ndarray:
    if strategy == "reversal_monthly":
        values = mask_research_values(
            matrix_feature(market, f"momentum_{params.reversal_lookback}d"),
            universe.eligible,
        )
    else:
        values = mask_research_values(
            _abnormal_turnover(market, params.turnover_short, params.turnover_long),
            universe.eligible,
        )
        if strategy == "abnormal_turnover_monthly_size_neutral":
            size = mask_research_values(matrix_feature(market, "log_float_mv"), universe.eligible)
            values = cross_sectional_residual(values, size)
    selected = select_low_bucket(values, top_n=params.top_n)
    return month_end_holding_weights(selected, month_end_mask(market.timestamp_labels))


def _parameter_scenarios(strategy: str) -> tuple[ParameterScenario, ...]:
    if strategy == "reversal_monthly":
        return (
            ParameterScenario("baseline"),
            ParameterScenario("top_n_10", top_n=10),
            ParameterScenario("top_n_30", top_n=30),
            ParameterScenario("lookback_10", reversal_lookback=10),
            ParameterScenario("lookback_30", reversal_lookback=30),
        )
    return (
        ParameterScenario("baseline"),
        ParameterScenario("top_n_10", top_n=10),
        ParameterScenario("top_n_30", top_n=30),
        ParameterScenario("windows_15_200", turnover_short=15, turnover_long=200),
        ParameterScenario("windows_30_300", turnover_short=30, turnover_long=300),
    )


def _run(
    market: MarketDataMatrix,
    weights: np.ndarray,
    delistings: np.ndarray,
    start: int,
    stop: int,
    cost: CostScenario,
    initial_capital: float,
) -> tuple[dict[str, object], pl.DataFrame, pl.DataFrame]:
    result = simulate_target_weights(
        slice_market_data_matrix(market, start, stop),
        weights[start:stop],
        config=MatcherConfig(
            initial_capital=initial_capital,
            commission_pct=cost.commission_pct,
            stamp_tax_pct=cost.stamp_tax_pct,
            slippage_bps=cost.slippage_bps,
        ),
        delistings=delistings[start:stop],
        delisting_recovery_rate=0.7,
        queue_proxy="base",
    )
    daily = result.daily.with_columns((pl.col("equity") / initial_capital).alias("nav"))
    return {**result.stats, **result.execution}, daily, result.yearly_returns


def _universe(
    market: MarketDataMatrix,
    instruments: pl.DataFrame,
    st_history: pl.DataFrame,
    shares: pl.DataFrame,
    strategy: str,
) -> ResearchUniverse:
    dates = tuple(date.fromisoformat(label[:10]) for label in market.timestamp_labels)
    pit = np.isfinite(market.close)
    if strategy.startswith("abnormal_turnover_"):
        pit &= build_pit_float_share_mask(dates, market.symbols, shares)
    return build_research_universe(dates, market.symbols, instruments, st_history, pit_valid=pit)


def _subperiods(start: date, end: date) -> tuple[tuple[str, date, date], ...]:
    periods = (
        ("early", start, min(end, date(2022, 12, 31))),
        ("middle", max(start, date(2023, 1, 1)), min(end, date(2024, 12, 31))),
        ("late", max(start, date(2025, 1, 1)), end),
    )
    return tuple(item for item in periods if item[1] <= item[2])


def _date_index(labels: tuple[str, ...], boundary: date, *, stop: bool = False) -> int:
    text = boundary.isoformat()
    if stop:
        matches = [index for index, label in enumerate(labels) if label[:10] <= text]
        return max(matches) + 1
    matches = [index for index, label in enumerate(labels) if label[:10] >= text]
    return min(matches)


def _yearly_active(aligned: pl.DataFrame, strategy: str) -> pl.DataFrame:
    return (
        aligned.with_columns(pl.col("date").str.slice(0, 4).cast(pl.Int32).alias("year"))
        .group_by("year")
        .agg(
            ((1.0 + pl.col("daily_return")).product() - 1.0).alias("portfolio_return"),
            ((1.0 + pl.col("benchmark_return")).product() - 1.0).alias("benchmark_return"),
        )
        .with_columns(
            ((1.0 + pl.col("portfolio_return")) / (1.0 + pl.col("benchmark_return")) - 1.0)
            .alias("excess_return"),
            pl.lit(strategy).alias("strategy"),
        )
        .select("strategy", "year", "portfolio_return", "benchmark_return", "excess_return")
        .sort("strategy", "year")
    )


def _year_contribution_gate(yearly: pl.DataFrame) -> bool:
    positive = yearly.filter(pl.col("excess_return") > 0)["excess_return"].to_numpy()
    if positive.size < 2:
        return False
    return float(positive.max() / positive.sum()) < 0.8


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
    shares = load_share_history(Path("data"))
    st_history = pl.read_parquet(args.st_history)
    market = append_research_bars(production, bars, delisted.select("symbol", "name"))
    axis = pl.DataFrame({"symbol": market.symbols})
    instruments = merge_research_instruments(
        current.join(axis, on="symbol", how="semi"),
        delisted.join(axis, on="symbol", how="semi"),
    )
    market, pit = with_strict_pit_turnover(
        market,
        historical_shares=shares,
        instrument_float_shares=instrument_float_share_vector(market.symbols, current),
        today=date.today(),
    )
    if pit["current_fallback_count"]:
        raise RuntimeError("strict PIT unexpectedly used current float-share fallback")
    dates = tuple(date.fromisoformat(label[:10]) for label in market.timestamp_labels)
    delistings = build_delisting_mask(dates, market.symbols, instruments)
    benchmark = load_benchmark_returns(repo, BENCHMARK, start=load_start, end=args.end)

    summaries: list[dict[str, object]] = []
    daily_frames: list[pl.DataFrame] = []
    yearly_frames: list[pl.DataFrame] = []
    final_yearly: list[pl.DataFrame] = []
    gates: list[dict[str, object]] = []
    phase1 = json.loads(args.phase1_summary.read_text(encoding="utf-8"))
    phase2 = pl.read_parquet(args.phase2_summary)
    phase3 = pl.read_parquet(args.phase3_summary)
    waterfall = build_bias_waterfall(phase1, phase2, phase3)

    for strategy in STRATEGIES:
        universe = _universe(market, instruments, st_history, shares, strategy)
        limited = with_research_price_limits(market, universe)
        baseline_params = ParameterScenario("baseline")
        baseline_weights = _weights(limited, strategy, universe, baseline_params)
        baseline_start = int(np.flatnonzero(baseline_weights.sum(axis=1) > 0)[0])
        strategy_start = date.fromisoformat(limited.timestamp_labels[baseline_start][:10])
        cost_rows: dict[str, dict[str, object]] = {}
        base_aligned: pl.DataFrame | None = None

        for cost in COSTS:
            stats, daily, yearly = _run(
                limited,
                baseline_weights,
                delistings,
                baseline_start,
                limited.shape[0],
                cost,
                args.initial_capital,
            )
            aligned = align_active_returns(daily, benchmark)
            row = {
                "strategy": strategy,
                "analysis": "cost",
                "scenario": cost.name,
                "period": "full",
                "start": str(daily["date"][0]),
                "end": str(daily["date"][-1]),
                **stats,
                **active_performance_stats(aligned),
            }
            cost_rows[cost.name] = row
            summaries.append(row)
            daily_frames.append(daily.with_columns(
                pl.lit(strategy).alias("strategy"),
                pl.lit("cost").alias("analysis"),
                pl.lit(cost.name).alias("scenario"),
            ))
            yearly_frames.append(yearly.with_columns(
                pl.lit(strategy).alias("strategy"),
                pl.lit("cost").alias("analysis"),
                pl.lit(cost.name).alias("scenario"),
            ))
            if cost.name == "base":
                base_aligned = aligned

        subperiod_positive = 0
        for name, period_start, period_end in _subperiods(strategy_start, args.end):
            start = _date_index(limited.timestamp_labels, period_start)
            stop = _date_index(limited.timestamp_labels, period_end, stop=True)
            stats, daily, yearly = _run(
                limited,
                baseline_weights,
                delistings,
                start,
                stop,
                COSTS[0],
                args.initial_capital,
            )
            aligned = align_active_returns(daily, benchmark)
            active = active_performance_stats(aligned)
            subperiod_positive += int(active["excess_total_return"] > 0)
            summaries.append({
                "strategy": strategy,
                "analysis": "subperiod",
                "scenario": name,
                "period": name,
                "start": str(daily["date"][0]),
                "end": str(daily["date"][-1]),
                **stats,
                **active,
            })
            yearly_frames.append(yearly.with_columns(
                pl.lit(strategy).alias("strategy"),
                pl.lit("subperiod").alias("analysis"),
                pl.lit(name).alias("scenario"),
            ))

        parameter_positive = True
        parameter_sets = [(params, _weights(limited, strategy, universe, params)) for params in _parameter_scenarios(strategy)]
        common_start = max(int(np.flatnonzero(weights.sum(axis=1) > 0)[0]) for _, weights in parameter_sets)
        for params, weights in parameter_sets:
            stats, daily, yearly = _run(
                limited,
                weights,
                delistings,
                common_start,
                limited.shape[0],
                COSTS[0],
                args.initial_capital,
            )
            parameter_positive &= stats["total_return"] > 0
            aligned = align_active_returns(daily, benchmark)
            summaries.append({
                "strategy": strategy,
                "analysis": "parameter",
                "scenario": params.name,
                "period": "common_full",
                "start": str(daily["date"][0]),
                "end": str(daily["date"][-1]),
                **stats,
                **active_performance_stats(aligned),
            })
            yearly_frames.append(yearly.with_columns(
                pl.lit(strategy).alias("strategy"),
                pl.lit("parameter").alias("analysis"),
                pl.lit(params.name).alias("scenario"),
            ))

        if base_aligned is None:
            raise RuntimeError("base aligned returns missing")
        yearly_active = _yearly_active(base_aligned, strategy)
        final_yearly.append(yearly_active)
        phase2_70 = phase2.filter(
            (pl.col("strategy") == strategy) & (pl.col("stage") == "delisted_recovery_70")
        ).row(0, named=True)
        phase2_0 = phase2.filter(
            (pl.col("strategy") == strategy) & (pl.col("stage") == "delisted_recovery_0")
        ).row(0, named=True)
        if strategy.startswith("abnormal_turnover_"):
            capacity = phase3.filter(
                (pl.col("strategy") == strategy)
                & (pl.col("initial_capital") == 10_000_000.0)
                & (pl.col("queue_proxy") == "conservative")
                & (pl.col("scenario") != "unconstrained")
            )
            capacity_gate = bool(
                (capacity["total_return"] > 0).all()
                and (capacity["excess_total_return"] > 0).all()
                and (capacity["information_ratio"] > 0).all()
            )
        else:
            capacity_gate = False
        gates.append({
            "strategy": strategy,
            "phase2_direction_positive": phase2_70["total_return"] > 0,
            "phase2_excess_positive": phase2_70["excess_total_return"] > 0,
            "phase2_ir_positive": phase2_70["information_ratio"] > 0,
            "extreme_recovery_positive": phase2_0["total_return"] > 0,
            "cost_20bps_positive": cost_rows["20bps"]["total_return"] > 0,
            "cost_40bps_positive": cost_rows["40bps"]["total_return"] > 0,
            "subperiod_gate": subperiod_positive >= 2,
            "parameter_gate": bool(parameter_positive),
            "capacity_10m_gate": capacity_gate,
            "year_contribution_gate": _year_contribution_gate(yearly_active),
            "unknowns_disclosed": True,
        })

    summary = pl.DataFrame(summaries).sort("strategy", "analysis", "scenario")
    verdicts = build_final_verdicts(pl.DataFrame(gates)).sort("strategy")
    yearly = pl.concat(final_yearly, how="vertical_relaxed")
    summary.write_parquet(output / "summary.parquet")
    verdicts.write_parquet(output / "verdicts.parquet")
    waterfall.write_parquet(output / "bias_waterfall.parquet")
    yearly.write_parquet(output / "yearly_active.parquet")
    pl.concat(daily_frames, how="diagonal_relaxed").write_parquet(output / "daily.parquet")
    pl.concat(yearly_frames, how="diagonal_relaxed").write_parquet(output / "yearly_returns.parquet")
    capacity_summary = phase3.filter(pl.col("initial_capital") == 10_000_000.0)
    capacity_summary.write_parquet(output / "capacity_summary_10m.parquet")
    shutil.copy2(args.universe_funnel_reversal, output / "universe_funnel_reversal.parquet")
    shutil.copy2(args.universe_funnel_turnover, output / "universe_funnel_turnover.parquet")
    _write_manifest(output, args, generation, pit)
    _write_report(output, summary, verdicts, yearly, waterfall)
    print(output)
    return 0


def _write_manifest(
    output: Path,
    args: argparse.Namespace,
    generation: str,
    pit: dict[str, object],
) -> None:
    input_files = (
        args.phase1_summary,
        args.phase2_summary,
        args.phase3_summary,
        args.universe_funnel_reversal,
        args.universe_funnel_turnover,
    )
    payload = {
        "run_type": "phase4_final_publication_gate",
        "generation": generation,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "costs": [item.__dict__ for item in COSTS],
        "delisting_recovery_rate": 0.7,
        "queue_model": "proxy",
        "queue_proxy": "base",
        "year_contribution_gate": "at least two positive excess years and largest positive year <80% of positive excess sum",
        "pit": pit,
        "sources": {
            str(path): _sha256(path)
            for path in input_files
        },
        "limitations": [
            "Beijing PIT ST remains unknown and fail-closed.",
            "Daily execution uses queue_model=proxy, not minute or Level-2 calibration.",
            "A passing verdict remains research_only and requires human review or paper-only observation.",
        ],
    }
    payload["outputs"] = {
        str(path.relative_to(output)): _sha256(path)
        for path in sorted(output.glob("*.parquet"))
    }
    (output / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_report(
    output: Path,
    summary: pl.DataFrame,
    verdicts: pl.DataFrame,
    yearly: pl.DataFrame,
    waterfall: pl.DataFrame,
) -> None:
    lines = [
        "# A-share final research publication gate",
        "",
        "Passing means `bias_reduced_candidate`, not production-ready. All strategies remain `research_only`.",
        "",
        "## Verdicts",
        "",
        "| strategy | verdict | failed gates |",
        "|---|---|---|",
    ]
    for row in verdicts.iter_rows(named=True):
        failed = [column for column in _GATE_COLUMNS if not row[column]]
        lines.append(f"| {row['strategy']} | {row['verdict']} | {', '.join(failed) or '-'} |")
    lines.extend(["", "## Bias waterfall", "", "| strategy | stage | total return | excess | IR |", "|---|---|---:|---:|---:|"])
    for row in waterfall.iter_rows(named=True):
        lines.append(
            f"| {row['strategy']} | {row['stage']} | {row['total_return']:.4f} | "
            f"{row['excess_total_return']:.4f} | {row['information_ratio']:.3f} |"
        )
    lines.extend(["", "## Full-sample costs", "", "| strategy | cost | total return | excess | IR | max drawdown |", "|---|---|---:|---:|---:|---:|"])
    for row in summary.filter(pl.col("analysis") == "cost").iter_rows(named=True):
        lines.append(
            f"| {row['strategy']} | {row['scenario']} | {row['total_return']:.4f} | "
            f"{row['excess_total_return']:.4f} | {row['information_ratio']:.3f} | {row['max_drawdown']:.4f} |"
        )
    lines.extend(["", "## Base yearly active returns", "", "| strategy | year | portfolio | benchmark | excess |", "|---|---:|---:|---:|---:|"])
    for row in yearly.iter_rows(named=True):
        lines.append(
            f"| {row['strategy']} | {row['year']} | {row['portfolio_return']:.4f} | "
            f"{row['benchmark_return']:.4f} | {row['excess_return']:.4f} |"
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
    parser.add_argument("--phase1-summary", type=Path, default=Path("data/research/a_share_strategy_validation/20260928T-phase1-bridge/summary.json"))
    parser.add_argument("--phase2-summary", type=Path, default=Path("data/research/a_share_strategy_validation/20260928T-phase2-waterfall-v2/summary.parquet"))
    parser.add_argument("--phase3-summary", type=Path, default=Path("data/research/a_share_strategy_validation/20260928T-phase3-capacity-v2/summary.parquet"))
    parser.add_argument("--universe-funnel-reversal", type=Path, default=Path("data/research/a_share_universe/20260928T-phase2-universe-audit-v2/universe_funnel_reversal.parquet"))
    parser.add_argument("--universe-funnel-turnover", type=Path, default=Path("data/research/a_share_universe/20260928T-phase2-universe-audit-v2/universe_funnel_turnover.parquet"))
    parser.add_argument("--output", type=Path, default=Path("data/research/a_share_strategy_validation/20260928T-phase4-final-v3"))
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    if args.initial_capital <= 0:
        parser.error("--initial-capital must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
