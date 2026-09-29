"""Phase 1 bridge: strict PIT turnover + benchmark excess for candidate strategies."""
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
from app.backtest.research_run_manifest import (
    build_research_run_manifest,
    read_git_identity,
    write_research_run_manifest,
)
from app.backtest.research_strict_pit import (
    instrument_float_share_vector,
    with_strict_pit_turnover,
)
from app.backtest.research_target_weights import simulate_target_weights
from app.backtest.strategy import ResolvedFeaturePlan
from app.share_capital import load_share_history
from app.strategy.builtin._research_portfolio import (
    cross_sectional_residual,
    month_end_holding_weights,
    month_end_mask,
    select_low_bucket,
)
from app.tickflow.repository import DataStore, KlineRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = (
    "reversal_monthly",
    "abnormal_turnover_monthly_raw",
    "abnormal_turnover_monthly_size_neutral",
)
BENCHMARKS = ("000985.SH", "000852.SH")


def _feature_plan() -> ResolvedFeaturePlan:
    fields = frozenset({
        "symbol", "date", "open", "high", "low", "close", "volume", "amount",
        "raw_close", "raw_high", "turnover_rate",
    })
    return ResolvedFeaturePlan(
        base_columns=fields,
        intermediate_columns=frozenset(),
        indicator_columns=frozenset(),
        signal_columns=frozenset(),
        matrix_columns=fields,
        instrument_columns=frozenset({"name", "float_shares"}),
        warmup_bars=301,
        full_feature_fallback=False,
        execution_backend="matrix_native",
    )


def _abnormal_turnover(market: MarketDataMatrix, short: int, long: int) -> np.ndarray:
    from app.backtest.matrix import valid_rolling_mean

    turnover = market.field("turnover_rate")
    valid = np.isfinite(turnover)
    short_mean = valid_rolling_mean(turnover, valid, short)
    long_mean = valid_rolling_mean(turnover, valid, long)
    result = np.full(market.shape, np.nan, dtype=np.float64)
    ok = np.isfinite(short_mean) & np.isfinite(long_mean) & (long_mean > 0)
    np.divide(short_mean, long_mean, out=result, where=ok)
    return result


def _weights(market: MarketDataMatrix, strategy: str) -> np.ndarray:
    if strategy == "reversal_monthly":
        selected = select_low_bucket(matrix_feature(market, "momentum_20d"), top_n=20)
        return month_end_holding_weights(selected, month_end_mask(market.timestamp_labels))
    values = _abnormal_turnover(market, 20, 250)
    if strategy == "abnormal_turnover_monthly_size_neutral":
        values = cross_sectional_residual(values, matrix_feature(market, "log_float_mv"))
    selected = select_low_bucket(values, top_n=20)
    return month_end_holding_weights(selected, month_end_mask(market.timestamp_labels))


def _first_invested(weights: np.ndarray) -> int:
    rows = np.flatnonzero(weights.sum(axis=1) > 0)
    if rows.size == 0:
        raise ValueError("target weights never invest")
    return int(rows[0])


def _run_one(
    market: MarketDataMatrix,
    strategy: str,
    *,
    initial_capital: float,
) -> dict[str, object]:
    weights = _weights(market, strategy)
    start = _first_invested(weights)
    result = simulate_target_weights(
        slice_market_data_matrix(market, start, market.shape[0]),
        weights[start:],
        config=MatcherConfig(
            initial_capital=initial_capital,
            commission_pct=0.0002,
            stamp_tax_pct=0.0005,
            slippage_bps=5.0,
        ),
    )
    daily = result.daily.with_columns(
        (pl.col("equity") / initial_capital).alias("nav")
    )
    return {
        "strategy": strategy,
        "start": str(daily["date"][0]),
        "end": str(daily["date"][-1]),
        "trading_days": daily.height,
        "stats": result.stats,
        "daily": daily,
    }


def _attach_benchmarks(
    run: dict[str, object],
    repo: KlineRepository,
    *,
    load_start: date,
    end: date,
) -> dict[str, object]:
    daily = run["daily"]
    assert isinstance(daily, pl.DataFrame)
    benchmark_stats: dict[str, object] = {}
    for symbol in BENCHMARKS:
        returns = load_benchmark_returns(repo, symbol, start=load_start, end=end)
        aligned = align_active_returns(daily, returns)
        benchmark_stats[symbol] = active_performance_stats(aligned)
    payload = {
        "strategy": run["strategy"],
        "start": run["start"],
        "end": run["end"],
        "trading_days": run["trading_days"],
        **run["stats"],
        "benchmarks": benchmark_stats,
    }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 9, 9))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 24))
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/research/a_share_strategy_validation/20260928T-phase1-bridge"),
    )
    args = parser.parse_args()

    load_start = args.start - timedelta(days=500)
    repo = KlineRepository(DataStore())
    engine = BacktestEngine(repo)
    generation = engine.data_generation_await("stock")
    if generation != "0b7900c875e347088914a263585b81e5":
        raise RuntimeError(
            f"stock generation drifted from Phase 0 freeze: {generation}"
        )

    market = engine.load_market_data_matrix_for_backtest(
        None,
        load_start,
        args.end,
        _feature_plan(),
        asset_type="stock",
        expected_generation=generation,
    )

    legacy_rows = []
    for strategy in CANDIDATES:
        run = _run_one(market, strategy, initial_capital=args.initial_capital)
        legacy_rows.append(_attach_benchmarks(run, repo, load_start=load_start, end=args.end))

    original_turnover = np.array(market.field("turnover_rate"), copy=True)
    instruments = repo.get_instruments()
    strict_market, pit_stats = with_strict_pit_turnover(
        market,
        historical_shares=load_share_history(Path("data")),
        instrument_float_shares=instrument_float_share_vector(
            market.symbols, instruments
        ),
        today=date.today(),
    )
    if pit_stats["current_fallback_count"] != 0:
        raise RuntimeError(
            f"strict PIT still has current_fallback_count={pit_stats['current_fallback_count']}"
        )

    strict_rows = []
    for strategy in CANDIDATES:
        run = _run_one(strict_market, strategy, initial_capital=args.initial_capital)
        row = _attach_benchmarks(run, repo, load_start=load_start, end=args.end)
        row["strict_pit"] = pit_stats
        strict_rows.append(row)

    # sanity: legacy matrix turnover unchanged
    if not np.allclose(
        market.field("turnover_rate"),
        original_turnover,
        equal_nan=True,
    ):
        raise RuntimeError("legacy market turnover was mutated")


    bridge = []
    for legacy, strict in zip(legacy_rows, strict_rows, strict=True):
        bridge.append({
            "strategy": legacy["strategy"],
            "legacy_total_return": legacy["total_return"],
            "strict_total_return": strict["total_return"],
            "delta_total_return": strict["total_return"] - legacy["total_return"],
            "legacy_csi_all_excess": legacy["benchmarks"]["000985.SH"]["excess_total_return"],
            "strict_csi_all_excess": strict["benchmarks"]["000985.SH"]["excess_total_return"],
            "legacy_csi_all_ir": legacy["benchmarks"]["000985.SH"]["information_ratio"],
            "strict_csi_all_ir": strict["benchmarks"]["000985.SH"]["information_ratio"],
            "legacy_csi1000_excess": legacy["benchmarks"]["000852.SH"]["excess_total_return"],
            "strict_csi1000_excess": strict["benchmarks"]["000852.SH"]["excess_total_return"],
        })

    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        "legacy": legacy_rows,
        "strict_pit": strict_rows,
        "bridge": bridge,
        "pit_overlay": pit_stats,
    }
    # daily frames are not JSON-serializable; drop before write
    for block in (summary["legacy"], summary["strict_pit"]):
        for row in block:
            row.pop("daily", None)

    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    pl.DataFrame(bridge).write_parquet(output / "bridge.parquet")

    code = read_git_identity(REPO_ROOT)
    manifest = build_research_run_manifest(
        run_id=output.name,
        run_type="phase1_benchmark_strict_pit_bridge",
        code=code,
        data={
            "data_generation": generation,
            "market_shape": list(market.shape),
            "pit_overlay": pit_stats,
            "benchmarks": list(BENCHMARKS),
        },
        parameters={
            "start": args.start.isoformat(),
            "end": args.end.isoformat(),
            "initial_capital": args.initial_capital,
            "cost": "base",
            "candidates": list(CANDIDATES),
        },
        degradations=[
            "stock_universe_missing_delisted_history",
            "st_status_from_current_name_not_pit",
            "no_adv_capacity_or_impact_model",
            "queue_model_unavailable_day_bar_only",
        ],
        sources={
            "shares": "data/financials/shares/part.parquet",
            "index_daily": "data/kline_index_daily/",
            "enriched": "data/kline_daily_enriched/",
        },
    )
    write_research_run_manifest(output / "manifest.json", manifest)

    lines = [
        "# Phase 1 Bridge: Benchmark Excess + Strict PIT",
        "",
        f"- data_generation: `{generation}`",
        f"- market shape: {list(market.shape)}",
        f"- strict current_fallback_count: {pit_stats['current_fallback_count']}",
        f"- strict missing_count: {pit_stats['missing_count']}",
        "",
        "| strategy | legacy total | strict total | Δ | CSI全指 excess (strict) | CSI全指 IR (strict) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in bridge:
        lines.append(
            "| {strategy} | {legacy_total_return:.4f} | {strict_total_return:.4f} | "
            "{delta_total_return:.4f} | {strict_csi_all_excess:.4f} | {strict_csi_all_ir} |".format(
                **{
                    **row,
                    "strict_csi_all_ir": (
                        "null"
                        if row["strict_csi_all_ir"] is None
                        else f"{row['strict_csi_all_ir']:.4f}"
                    ),
                }
            )
        )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps({"bridge": bridge, "pit_overlay": pit_stats}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
