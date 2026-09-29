#!/usr/bin/env python3
"""Run Phase 3 ADV-capacity and square-root-impact scenarios."""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import slice_market_data_matrix
from app.backtest.research_benchmarks import (
    active_performance_stats,
    align_active_returns,
    load_benchmark_returns,
)
from app.backtest.research_strict_pit import instrument_float_share_vector, with_strict_pit_turnover
from app.backtest.research_target_weights import simulate_target_weights
from app.backtest.research_universe import (
    append_research_bars,
    build_delisting_mask,
    build_pit_float_share_mask,
    build_research_universe,
    with_research_price_limits,
)
from app.backtest.research_universe_data import merge_research_instruments
from app.share_capital import load_share_history
from app.tickflow.repository import DataStore, KlineRepository
from scripts.run_bias_repair_phase1_bridge import _feature_plan
from scripts.run_bias_repair_phase2_bridge import BENCHMARK, GENERATION, _weights

STRATEGIES = (
    "abnormal_turnover_monthly_raw",
    "abnormal_turnover_monthly_size_neutral",
)
CAPITALS = (1_000_000.0, 10_000_000.0, 50_000_000.0, 100_000_000.0)
ADV_CAPS = (0.01, 0.02, 0.05, 0.10)
IMPACT_ETAS = (0.10, 0.25, 0.50)
QUEUE_PROXIES = ("optimistic", "base", "conservative")


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
    observed = np.isfinite(market.close)
    universe = build_research_universe(
        dates,
        market.symbols,
        instruments,
        st_history,
        pit_valid=observed & build_pit_float_share_mask(dates, market.symbols, shares),
    )
    market = with_research_price_limits(market, universe)
    delistings = build_delisting_mask(dates, market.symbols, instruments)
    benchmark = load_benchmark_returns(repo, BENCHMARK, start=load_start, end=args.end)

    rows: list[dict[str, object]] = []
    daily_frames: list[pl.DataFrame] = []
    for strategy in STRATEGIES:
        weights = _weights(market, strategy, universe)
        invested = np.flatnonzero(weights.sum(axis=1) > 0)
        if invested.size == 0:
            raise ValueError(f"{strategy} target weights never invest")
        start = int(invested[0])
        sliced_market = slice_market_data_matrix(market, start, market.shape[0])
        sliced_weights = weights[start:]
        sliced_delistings = delistings[start:]
        scenarios = [("unconstrained", None, 0.0, "base")] + [
            (
                f"adv_{int(cap * 100)}pct_eta_{eta:.2f}_queue_{queue_proxy}",
                cap,
                eta,
                queue_proxy,
            )
            for cap in ADV_CAPS
            for eta in IMPACT_ETAS
            for queue_proxy in QUEUE_PROXIES
        ]
        for initial_capital in CAPITALS:
            for scenario, cap, eta, queue_proxy in scenarios:
                result = simulate_target_weights(
                    sliced_market,
                    sliced_weights,
                    config=MatcherConfig(
                        initial_capital=initial_capital,
                        commission_pct=0.0002,
                        stamp_tax_pct=0.0005,
                        slippage_bps=5.0,
                    ),
                    delistings=sliced_delistings,
                    delisting_recovery_rate=0.7,
                    max_adv_participation=cap,
                    impact_eta=eta,
                    queue_proxy=queue_proxy,
                )
                daily = result.daily.with_columns(
                    (pl.col("equity") / initial_capital).alias("nav")
                )
                row = {
                    "strategy": strategy,
                    "scenario": scenario,
                    "initial_capital": initial_capital,
                    "max_adv_participation_config": cap,
                    "impact_eta": eta,
                    "queue_model": "proxy",
                    "queue_proxy": queue_proxy,
                    "start": str(daily["date"][0]),
                    "end": str(daily["date"][-1]),
                    "trading_days": daily.height,
                    **result.stats,
                    **result.execution,
                    **active_performance_stats(align_active_returns(daily, benchmark)),
                }
                requested = (
                    daily["buy_notional"].sum()
                    + daily["sell_notional"].sum()
                    + daily["capacity_unfilled_buy_notional"].sum()
                    + daily["capacity_unfilled_sell_notional"].sum()
                    if cap is not None else daily["buy_notional"].sum() + daily["sell_notional"].sum()
                )
                unfilled = (
                    daily["capacity_unfilled_buy_notional"].sum()
                    + daily["capacity_unfilled_sell_notional"].sum()
                    if cap is not None else 0.0
                )
                row.setdefault("capacity_unfilled_buy_notional", 0.0)
                row.setdefault("capacity_unfilled_sell_notional", 0.0)
                row.setdefault("impact_cost", 0.0)
                row.update({
                    "unfilled_rate": float(unfilled / requested) if requested > 0 else 0.0,
                    "observed_max_adv_participation": (
                        float(daily["max_adv_participation"].max()) if cap is not None else None
                    ),
                    "observed_p95_adv_participation": (
                        float(daily["p95_adv_participation"].max()) if cap is not None else None
                    ),
                    "max_requested_adv_participation": (
                        float(daily["max_requested_adv_participation"].max()) if cap is not None else None
                    ),
                    "max_liquidation_days": (
                        int(daily["max_liquidation_days"].max()) if cap is not None else 0
                    ),
                })
                rows.append(row)
                daily_frames.append(daily.with_columns(
                    pl.lit(strategy).alias("strategy"),
                    pl.lit(scenario).alias("scenario"),
                    pl.lit(initial_capital).alias("initial_capital"),
                ))

    summary = pl.DataFrame(rows).sort("strategy", "initial_capital", "scenario")
    summary.write_parquet(output / "summary.parquet")
    pl.concat(daily_frames, how="diagonal_relaxed").write_parquet(output / "daily.parquet")
    _write_manifest(output, args, generation, pit)
    _write_report(output, summary)
    print(output)
    return 0


def _write_manifest(output: Path, args: argparse.Namespace, generation: str, pit: dict[str, object]) -> None:
    payload = {
        "run_type": "phase3_capacity_impact",
        "generation": generation,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "strategies": list(STRATEGIES),
        "initial_capitals": list(CAPITALS),
        "adv_caps": list(ADV_CAPS),
        "impact_etas": list(IMPACT_ETAS),
        "queue_proxies": list(QUEUE_PROXIES),
        "delisting_recovery_rate": 0.7,
        "queue_model": "proxy",
        "queue_proxy_definition": {
            "optimistic": "allow limit-locked queue fills when the daily bar is otherwise tradable",
            "base": "block one-price locked bars only",
            "conservative": "block orders against any close-locked daily bar",
        },
        "pit": pit,
        "limitations": [
            "Daily-bar queue_model=proxy; no minute or Level-2 queue position is claimed.",
            "Square-root impact parameters are frozen scenarios, not locally calibrated truth.",
            "Results remain research_only.",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _write_report(output: Path, summary: pl.DataFrame) -> None:
    lines = [
        "# Phase 3 capacity and impact validation",
        "",
        "Daily data supports only `queue_model=proxy`; this is not a Level-2 queue model.",
        "",
        "| strategy | capital | scenario | total return | excess | IR | unfilled | impact cost | max participation | liquidation days |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.iter_rows(named=True):
        lines.append(
            "| {strategy} | {initial_capital:.0f} | {scenario} | {total_return:.4f} | "
            "{excess_total_return:.4f} | {information_ratio:.3f} | {unfilled_rate:.4f} | "
            "{impact_cost:.2f} | {observed_max_adv_participation} | {max_liquidation_days} |".format(**row)
        )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 9, 9))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 24))
    parser.add_argument("--current-instruments", type=Path, default=Path("data/instruments/instruments.parquet"))
    parser.add_argument("--delisted-instruments", type=Path, default=Path("data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/delisted_instruments.parquet"))
    parser.add_argument("--delisted-daily", type=Path, default=Path("data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/delisted_daily"))
    parser.add_argument("--st-history", type=Path, default=Path("data/research/a_share_universe/20260928T-phase2-st-history-v1/st_history.parquet"))
    parser.add_argument("--output", type=Path, default=Path("data/research/a_share_strategy_validation/20260928T-phase3-capacity-v2"))
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
