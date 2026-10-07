#!/usr/bin/env python3
"""Run one immutable paper-only OOS observation for abnormal-turnover strategies.

This is a one-shot CLI shared by manual runs and an external scheduler. It never places orders,
never installs a schedule, and never overwrites an existing observation.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import MarketDataMatrix, matrix_feature, slice_market_data_matrix
from app.backtest.research_benchmarks import (
    active_performance_stats,
    align_active_returns,
    load_benchmark_returns,
)
from app.backtest.research_run_manifest import (
    build_research_run_manifest,
    file_fingerprint,
    read_git_identity,
    sha256_file,
    write_research_run_manifest,
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
from app.strategy.builtin._research_portfolio import (
    cross_sectional_residual,
    month_end_holding_weights,
    select_low_bucket,
)
from app.tickflow.repository import DataStore, KlineRepository
from scripts.run_bias_repair_phase1_bridge import _abnormal_turnover, _feature_plan
from scripts.run_bias_repair_phase2_bridge import BENCHMARK

OOS_START = date(2026, 9, 25)
TOP_N = 20
TURNOVER_SHORT = 20
TURNOVER_LONG = 250
DELISTING_RECOVERY_RATE = 0.70


@dataclass(frozen=True)
class AccountScenario:
    name: str
    strategy: str
    initial_capital: float
    max_adv_participation: float | None
    impact_eta: float
    queue_proxy: Literal["base", "conservative"]


def account_scenarios() -> tuple[AccountScenario, ...]:
    """Return the four frozen paper accounts."""
    return (
        AccountScenario(
            "raw_primary", "abnormal_turnover_monthly_raw", 1_000_000.0, None, 0.0, "base"
        ),
        AccountScenario(
            "raw_stress", "abnormal_turnover_monthly_raw", 10_000_000.0, 0.01, 0.50,
            "conservative",
        ),
        AccountScenario(
            "size_neutral_primary", "abnormal_turnover_monthly_size_neutral",
            1_000_000.0, None, 0.0, "base",
        ),
        AccountScenario(
            "size_neutral_stress", "abnormal_turnover_monthly_size_neutral",
            10_000_000.0, 0.01, 0.50, "conservative",
        ),
    )


def observation_path(root: Path, as_of: date) -> Path:
    path = root / f"as_of={as_of.isoformat()}"
    if path.exists():
        raise FileExistsError(f"observation already exists: {path}")
    return path


def completed_month_end_mask(
    labels: tuple[str, ...],
    trading_calendar: tuple[date, ...],
    *,
    as_of: date,
) -> np.ndarray:
    """Mark completed calendar month ends without requiring a future market bar."""
    month_ends: set[date] = set()
    grouped: dict[tuple[int, int], list[date]] = {}
    for day in trading_calendar:
        grouped.setdefault((day.year, day.month), []).append(day)
    for days in grouped.values():
        last = max(days)
        if last <= as_of:
            month_ends.add(last)
    return np.asarray([date.fromisoformat(label[:10]) in month_ends for label in labels], dtype=bool)


def require_calendar_horizon(trading_calendar: tuple[date, ...], as_of: date) -> None:
    if not any((day.year, day.month) > (as_of.year, as_of.month) for day in trading_calendar):
        raise RuntimeError(f"calendar does not reach beyond {as_of:%Y-%m}")


def freeze_oos_month_ends(
    labels: tuple[str, ...],
    month_ends: np.ndarray,
    *,
    oos_start: date,
) -> np.ndarray:
    frozen = np.asarray(month_ends, dtype=bool).copy()
    before = np.asarray([date.fromisoformat(label[:10]) < oos_start for label in labels])
    frozen[before] = False
    return frozen


def load_trading_calendar(path: Path) -> tuple[date, ...]:
    days = tuple(
        date.fromisoformat(line.strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if not days:
        raise ValueError("trading calendar is empty")
    if len(set(days)) != len(days) or tuple(sorted(days)) != days:
        raise ValueError("trading calendar must be sorted and unique")
    return days


def manifest_covered_through(path: Path) -> date:
    payload = json.loads(path.read_text(encoding="utf-8"))
    value = payload.get("end")
    if not value:
        raise RuntimeError(f"source manifest missing end date: {path}")
    return date.fromisoformat(str(value))


def require_instrument_snapshot(snapshot_date: date, as_of: date) -> None:
    if snapshot_date != as_of:
        raise RuntimeError(f"instrument snapshot is {snapshot_date}, expected as-of {as_of}")


def require_source_coverage(name: str, covered_through: date, as_of: date) -> None:
    if covered_through < as_of:
        raise RuntimeError(f"{name} only covers through {covered_through}, before as-of {as_of}")


def require_market_completeness(close: np.ndarray, *, minimum_ratio: float = 0.95) -> None:
    values = np.asarray(close, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] < 21:
        raise RuntimeError("market completeness requires at least 21 daily rows")
    baseline = np.median(np.isfinite(values[-21:-1]).sum(axis=1))
    latest = int(np.isfinite(values[-1]).sum())
    if baseline <= 0 or latest < baseline * minimum_ratio:
        raise RuntimeError(
            f"latest market coverage is incomplete: latest={latest}, baseline={baseline:.0f}"
        )


def restrict_bars_to_market_axis(
    bars: pl.DataFrame,
    labels: tuple[str, ...],
) -> pl.DataFrame:
    if not labels:
        raise ValueError("market date axis is empty")
    start = date.fromisoformat(labels[0][:10])
    end = date.fromisoformat(labels[-1][:10])
    return bars.filter(pl.col("date").is_between(start, end, closed="both"))


def build_oos_weights(
    market: MarketDataMatrix,
    strategy: str,
    universe: ResearchUniverse,
    month_ends: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    values = mask_research_values(
        _abnormal_turnover(market, TURNOVER_SHORT, TURNOVER_LONG),
        universe.eligible,
    )
    if strategy == "abnormal_turnover_monthly_size_neutral":
        size = mask_research_values(matrix_feature(market, "log_float_mv"), universe.eligible)
        values = cross_sectional_residual(values, size)
    selected = select_low_bucket(values, top_n=TOP_N)
    return month_end_holding_weights(selected, month_ends), values


def warmup_start_index(labels: tuple[str, ...], boundary: date, *, bars: int) -> int:
    boundary_index = _date_index(labels, boundary)
    return max(0, boundary_index - bars)


def signal_rows(
    labels: tuple[str, ...],
    symbols: tuple[str, ...],
    names: tuple[str, ...],
    strategy: str,
    values: np.ndarray,
    month_ends: np.ndarray,
    trading_calendar: tuple[date, ...],
    *,
    oos_start: date,
    top_n: int = TOP_N,
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for time_id in np.flatnonzero(month_ends):
        signal_date = date.fromisoformat(labels[time_id][:10])
        if signal_date < oos_start:
            continue
        finite = np.flatnonzero(np.isfinite(values[time_id]))
        order = sorted(finite, key=lambda asset_id: (values[time_id, asset_id], asset_id))[:top_n]
        later_days = [day for day in trading_calendar if day > signal_date]
        execution_date = min(later_days) if later_days else None
        for rank, asset_id in enumerate(order, start=1):
            rows.append({
                "strategy": strategy,
                "signal_date": signal_date,
                "execution_date": execution_date,
                "symbol": symbols[asset_id],
                "name": names[asset_id],
                "rank": rank,
                "factor_value": float(values[time_id, asset_id]),
                "target_weight": 1.0 / len(order),
            })
    return pl.DataFrame(rows, schema={
        "strategy": pl.String,
        "signal_date": pl.Date,
        "execution_date": pl.Date,
        "symbol": pl.String,
        "name": pl.String,
        "rank": pl.Int64,
        "factor_value": pl.Float64,
        "target_weight": pl.Float64,
    })


def target_rows_with_pending(targets: pl.DataFrame, signals: pl.DataFrame) -> pl.DataFrame:
    pending = (
        signals.filter(pl.col("execution_date").is_not_null())
        .select(
            "strategy",
            pl.col("execution_date").alias("date"),
            "symbol",
            "target_weight",
        )
    )
    return (
        pl.concat([targets, pending], how="vertical_relaxed")
        .unique(["strategy", "date", "symbol"], keep="last")
        .sort("date", "strategy", "symbol")
    )


def signal_overlap(signals: pl.DataFrame) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for signal_date in sorted(signals["signal_date"].unique().to_list()):
        day = signals.filter(pl.col("signal_date") == signal_date)
        raw = set(day.filter(
            pl.col("strategy") == "abnormal_turnover_monthly_raw"
        )["symbol"].to_list())
        neutral = set(day.filter(
            pl.col("strategy") == "abnormal_turnover_monthly_size_neutral"
        )["symbol"].to_list())
        union = raw | neutral
        rows.append({
            "signal_date": signal_date,
            "intersection": len(raw & neutral),
            "union": len(union),
            "jaccard": len(raw & neutral) / len(union) if union else None,
            "raw_only": sorted(raw - neutral),
            "size_neutral_only": sorted(neutral - raw),
        })
    return pl.DataFrame(rows, schema={
        "signal_date": pl.Date,
        "intersection": pl.Int64,
        "union": pl.Int64,
        "jaccard": pl.Float64,
        "raw_only": pl.List(pl.String),
        "size_neutral_only": pl.List(pl.String),
    })


def started_account_window(daily: pl.DataFrame) -> pl.DataFrame:
    starts = np.flatnonzero(daily["target_exposure"].to_numpy() > 0)
    return daily.slice(int(starts[0])) if starts.size else daily.head(0)


def monthly_account_metrics(
    daily: pl.DataFrame,
    benchmark: pl.DataFrame,
    account: str,
) -> pl.DataFrame:
    aligned = daily.join(benchmark, on="date", how="left")
    if aligned["benchmark_return"].null_count():
        raise ValueError("monthly metrics missing benchmark days")
    return (
        aligned.with_columns(pl.col("date").str.slice(0, 7).alias("month"))
        .group_by("month")
        .agg(
            ((1.0 + pl.col("daily_return")).product() - 1.0).alias("portfolio_return"),
            ((1.0 + pl.col("benchmark_return")).product() - 1.0).alias("benchmark_return"),
            pl.col("trading_cost").sum().alias("trading_cost"),
            pl.col("one_way_turnover").sum().alias("one_way_turnover"),
            pl.col("blocked_buy_count").sum().alias("blocked_buy_count"),
            pl.col("blocked_sell_count").sum().alias("blocked_sell_count"),
        )
        .with_columns(
            ((1.0 + pl.col("portfolio_return")) / (1.0 + pl.col("benchmark_return")) - 1.0)
            .alias("excess_return"),
            pl.lit(account).alias("account"),
        )
        .sort("month")
    )


def _empty_monthly_metrics() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "month": pl.String,
        "portfolio_return": pl.Float64,
        "benchmark_return": pl.Float64,
        "trading_cost": pl.Float64,
        "one_way_turnover": pl.Float64,
        "blocked_buy_count": pl.Int64,
        "blocked_sell_count": pl.Int64,
        "excess_return": pl.Float64,
        "account": pl.String,
    })


def oos_account_stats(
    daily: pl.DataFrame,
    *,
    initial_capital: float,
    oos_start: date,
) -> dict[str, object]:
    observed = daily.filter(pl.col("date") >= oos_start.isoformat())
    if observed.is_empty():
        raise ValueError("OOS account window is empty")
    equity = observed["equity"].to_numpy()
    returns = observed["daily_return"].to_numpy()
    total_return = float(equity[-1] / initial_capital - 1.0)
    periods = observed.height
    annual_return = (
        (1.0 + total_return) ** (252.0 / periods) - 1.0
        if total_return > -1 else total_return
    )
    volatility = float(np.std(returns) * np.sqrt(252.0))
    sharpe = (
        float(np.mean(returns) / np.std(returns) * np.sqrt(252.0))
        if np.std(returns) > 0 else 0.0
    )
    return {
        "trading_days": periods,
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": float(observed["drawdown"].min()),
        "annualized_turnover": float(observed["one_way_turnover"].mean() * 252.0),
        "trading_cost": float(observed["trading_cost"].sum()),
        "average_positions": float(observed["positions"].mean()),
        "average_cash_weight": float(observed["cash_weight"].mean()),
    }


def main() -> int:
    args = _parse_args()
    output = observation_path(args.output_root.resolve(), args.as_of)
    calendar = load_trading_calendar(args.trading_calendar)
    require_calendar_horizon(calendar, args.as_of)
    if args.as_of not in calendar:
        raise RuntimeError(f"--as-of is not in the frozen trading calendar: {args.as_of}")

    repo = KlineRepository(DataStore())
    engine = BacktestEngine(repo)
    generation = engine.data_generation_await("stock")
    if generation != args.expected_generation:
        raise RuntimeError(
            f"stock generation drifted: expected {args.expected_generation}, got {generation}"
        )
    load_start = OOS_START - timedelta(days=500)
    production = engine.load_market_data_matrix_for_backtest(
        None,
        load_start,
        args.as_of,
        _feature_plan(),
        asset_type="stock",
        expected_generation=generation,
    )
    last_market_date = date.fromisoformat(production.timestamp_labels[-1][:10])
    if last_market_date != args.as_of:
        raise RuntimeError(
            f"market data incomplete for as-of: expected {args.as_of}, got {last_market_date}"
        )
    require_market_completeness(production.close)

    current = pl.read_parquet(args.current_instruments)
    require_instrument_snapshot(current["as_of"].max(), args.as_of)
    delisted = pl.read_parquet(args.delisted_instruments)
    bars = restrict_bars_to_market_axis(
        pl.scan_parquet(args.delisted_daily / "**" / "*.parquet").collect(),
        production.timestamp_labels,
    )
    shares = pl.read_parquet(args.share_history)
    if not {"symbol", "period_end", "float_shares"} <= set(shares.columns):
        raise RuntimeError("share history is missing required PIT columns")
    st_history = pl.read_parquet(args.st_history)
    exclusion_history = pl.read_parquet(args.exclusion_history)
    require_source_coverage(
        "ST history",
        manifest_covered_through(args.st_manifest),
        args.as_of,
    )
    require_source_coverage(
        "delisted universe",
        manifest_covered_through(args.delisted_coverage_manifest),
        args.as_of,
    )
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
        today=args.as_of,
    )
    if pit["current_fallback_count"]:
        raise RuntimeError("strict PIT unexpectedly used current float-share fallback")

    dates = tuple(date.fromisoformat(label[:10]) for label in market.timestamp_labels)
    observed = np.isfinite(market.close)
    universe = build_research_universe(
        dates,
        market.symbols,
        instruments,
        st_history,
        exclusion_history=exclusion_history,
        pit_valid=observed & build_pit_float_share_mask(dates, market.symbols, shares),
    )
    market = with_research_price_limits(market, universe)
    delistings = build_delisting_mask(dates, market.symbols, instruments)
    month_ends = freeze_oos_month_ends(
        market.timestamp_labels,
        completed_month_end_mask(
            market.timestamp_labels,
            calendar,
            as_of=args.as_of,
        ),
        oos_start=OOS_START,
    )
    benchmark = load_benchmark_returns(repo, BENCHMARK, start=load_start, end=args.as_of)

    strategy_books: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    signal_frames: list[pl.DataFrame] = []
    target_frames: list[pl.DataFrame] = []
    for strategy in {scenario.strategy for scenario in account_scenarios()}:
        weights, values = build_oos_weights(market, strategy, universe, month_ends)
        strategy_books[strategy] = weights, values
        signal_frames.append(signal_rows(
            market.timestamp_labels,
            market.symbols,
            market.names,
            strategy,
            values,
            month_ends,
            calendar,
            oos_start=OOS_START,
        ))
        target_frames.append(_target_rows(market, strategy, weights, start=OOS_START))

    signals = pl.concat(signal_frames, how="vertical_relaxed").sort(
        "signal_date", "strategy", "rank"
    )
    targets = target_rows_with_pending(
        pl.concat(target_frames, how="vertical_relaxed"),
        signals,
    )
    account_frames: list[pl.DataFrame] = []
    monthly_frames: list[pl.DataFrame] = []
    metrics: list[dict[str, object]] = []
    for scenario in account_scenarios():
        weights, _ = strategy_books[scenario.strategy]
        start = warmup_start_index(market.timestamp_labels, OOS_START, bars=20)
        sliced = slice_market_data_matrix(market, start, market.shape[0])
        result = simulate_target_weights(
            sliced,
            weights[start:],
            config=MatcherConfig(
                initial_capital=scenario.initial_capital,
                commission_pct=0.0002,
                stamp_tax_pct=0.0005,
                slippage_bps=5.0,
            ),
            delistings=delistings[start:],
            delisting_recovery_rate=DELISTING_RECOVERY_RATE,
            max_adv_participation=scenario.max_adv_participation,
            impact_eta=scenario.impact_eta,
            queue_proxy=scenario.queue_proxy,
        )
        daily = result.daily.with_columns(
            pl.lit(scenario.name).alias("account"),
            pl.lit(scenario.strategy).alias("strategy"),
            (pl.col("equity") / scenario.initial_capital).alias("nav"),
        )
        observed = daily.filter(pl.col("date") >= OOS_START.isoformat())
        started = started_account_window(observed)
        active_stats = (
            active_performance_stats(align_active_returns(started, benchmark))
            if not started.is_empty() else {
                "benchmark_total_return": None,
                "excess_total_return": None,
                "annual_excess_return": None,
                "tracking_error": None,
                "information_ratio": None,
                "relative_drawdown": None,
                "relative_nav": None,
            }
        )
        metrics.append({
            "account": scenario.name,
            "strategy": scenario.strategy,
            "as_of": args.as_of,
            **oos_account_stats(
                daily,
                initial_capital=scenario.initial_capital,
                oos_start=OOS_START,
            ),
            **result.execution,
            **active_stats,
        })
        account_frames.append(observed)
        if not started.is_empty():
            monthly_frames.append(monthly_account_metrics(started, benchmark, scenario.name))

    accounts = pl.concat(account_frames, how="diagonal_relaxed").sort("date", "account")
    summary = pl.DataFrame(metrics).sort("account")
    monthly = (
        pl.concat(monthly_frames, how="vertical_relaxed").sort("month", "account")
        if monthly_frames else _empty_monthly_metrics()
    )
    overlap = signal_overlap(signals)
    quality = universe.funnel.filter(pl.col("date") >= OOS_START).with_columns(
        pl.lit(pit["current_fallback_count"]).alias("current_float_share_fallback_count"),
        pl.lit(generation).alias("generation"),
    )

    output.mkdir(parents=True)
    signals.write_parquet(output / "signals.parquet")
    targets.write_parquet(output / "target_weights.parquet")
    accounts.write_parquet(output / "daily_accounts.parquet")
    summary.write_parquet(output / "summary.parquet")
    monthly.write_parquet(output / "monthly_metrics.parquet")
    overlap.write_parquet(output / "signal_overlap.parquet")
    quality.write_parquet(output / "data_quality.parquet")
    _write_manifest(output, args, generation, pit)
    _write_report(output, args, signals, summary, quality)
    print(output)
    return 0


def _target_rows(
    market: MarketDataMatrix,
    strategy: str,
    weights: np.ndarray,
    *,
    start: date,
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for time_id in range(market.shape[0]):
        day = date.fromisoformat(market.timestamp_labels[time_id][:10])
        if day < start:
            continue
        for asset_id in np.flatnonzero(weights[time_id] > 0):
            rows.append({
                "strategy": strategy,
                "date": day,
                "symbol": market.symbols[asset_id],
                "target_weight": float(weights[time_id, asset_id]),
            })
    return pl.DataFrame(rows, schema={
        "strategy": pl.String,
        "date": pl.Date,
        "symbol": pl.String,
        "target_weight": pl.Float64,
    })


def _date_index(labels: tuple[str, ...], boundary: date) -> int:
    matches = [index for index, label in enumerate(labels) if label[:10] >= boundary.isoformat()]
    if not matches:
        raise ValueError(f"no market date on or after {boundary}")
    return min(matches)


def _write_manifest(
    output: Path,
    args: argparse.Namespace,
    generation: str,
    pit: dict[str, object],
) -> None:
    source_paths = (
        args.current_instruments,
        args.delisted_instruments,
        args.delisted_coverage_manifest,
        args.delisted_adjusted_manifest,
        args.share_history,
        args.st_history,
        args.st_manifest,
        args.exclusion_history,
        args.trading_calendar,
    )
    manifest = build_research_run_manifest(
        run_id=output.name,
        run_type="a_share_abnormal_turnover_oos_observer",
        code=read_git_identity(Path.cwd()),
        data={
            "generation": generation,
            "as_of": args.as_of.isoformat(),
            "oos_start": OOS_START.isoformat(),
            "pit": pit,
            "inputs": {str(path): file_fingerprint(path) for path in source_paths},
        },
        parameters={
            "top_n": TOP_N,
            "turnover_short": TURNOVER_SHORT,
            "turnover_long": TURNOVER_LONG,
            "delisting_recovery_rate": DELISTING_RECOVERY_RATE,
            "accounts": [asdict(item) for item in account_scenarios()],
        },
        degradations=[
            "Beijing PIT ST remains unknown and fail-closed.",
            "Daily execution uses queue_model=proxy, not minute or Level-2 calibration.",
            "Paper-only observation; no real orders are sent.",
        ],
        sources={str(path): sha256_file(path) for path in source_paths},
        extras={
            "outputs": {
                path.name: sha256_file(path)
                for path in sorted(output.glob("*.parquet"))
            },
        },
    )
    write_research_run_manifest(output / "manifest.json", manifest)


def _write_report(
    output: Path,
    args: argparse.Namespace,
    signals: pl.DataFrame,
    summary: pl.DataFrame,
    quality: pl.DataFrame,
) -> None:
    lines = [
        "# A-share abnormal-turnover OOS observation",
        "",
        f"- As of: {args.as_of}",
        f"- Frozen OOS start: {OOS_START}",
        "- Mode: paper-only; no real orders.",
        "- Invocation: one-shot CLI; manual and external scheduler use the same command.",
        "",
        "## Accounts",
        "",
        "| account | strategy | total return | excess | IR | max drawdown |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in summary.iter_rows(named=True):
        lines.append(
            f"| {row['account']} | {row['strategy']} | {row['total_return']:.4f} | "
            f"{_format_optional(row['excess_total_return'])} | "
            f"{_format_optional(row['information_ratio'])} | "
            f"{row['max_drawdown']:.4f} |"
        )
    lines.extend([
        "",
        "## Observation quality",
        "",
        f"- OOS signal rows: {signals.height}",
        f"- Last eligible count: {quality['eligible'][-1] if quality.height else 0}",
        f"- Last ST unknown count: {quality['st_unknown'][-1] if quality.height else 0}",
        "- Queue model: daily proxy only.",
    ])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_optional(value: object) -> str:
    return "not started" if value is None else f"{float(value):.4f}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--expected-generation", required=True)
    parser.add_argument(
        "--trading-calendar",
        type=Path,
        required=True,
        help="Frozen newline-delimited A-share trading dates; required to identify real-time month ends.",
    )
    parser.add_argument(
        "--output-root", type=Path, default=Path("data/research/a_share_oos")
    )
    parser.add_argument(
        "--current-instruments", type=Path,
        default=Path("data/instruments/instruments.parquet"),
    )
    parser.add_argument(
        "--delisted-instruments", type=Path,
        default=Path(
            "data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/"
            "delisted_instruments.parquet"
        ),
    )
    parser.add_argument(
        "--delisted-coverage-manifest", type=Path,
        default=Path(
            "data/research/a_share_universe/20260928T-phase2-baostock-v1/manifest.json"
        ),
    )
    parser.add_argument(
        "--delisted-adjusted-manifest", type=Path,
        default=Path(
            "data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/"
            "manifest.json"
        ),
    )
    parser.add_argument(
        "--delisted-daily", type=Path,
        default=Path(
            "data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/"
            "delisted_daily"
        ),
    )
    parser.add_argument(
        "--share-history", type=Path,
        default=Path("data/financials/shares/part.parquet"),
    )
    parser.add_argument(
        "--st-history", type=Path,
        default=Path(
            "data/research/a_share_universe/20260928T-phase2-st-history-v1/"
            "st_history.parquet"
        ),
    )
    parser.add_argument("--exclusion-history", type=Path, required=True)
    parser.add_argument(
        "--st-manifest", type=Path,
        default=Path(
            "data/research/a_share_universe/20260928T-phase2-st-history-v1/manifest.json"
        ),
    )
    args = parser.parse_args()
    if args.as_of < OOS_START:
        parser.error(f"--as-of must be on or after {OOS_START}")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
