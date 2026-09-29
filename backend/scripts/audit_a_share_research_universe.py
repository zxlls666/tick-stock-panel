#!/usr/bin/env python3
"""Build the Phase 2 research instrument overlay and daily universe audit."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.research_universe import (
    build_observation_mask,
    build_pit_float_share_mask,
    build_research_universe,
)
from app.backtest.research_universe_data import merge_research_instruments


def main() -> int:
    args = _parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"output already exists: {output}")
    output.mkdir(parents=True)

    current = pl.read_parquet(args.current_instruments)
    delisted = pl.read_parquet(args.delisted_instruments)
    st_history = pl.read_parquet(args.st_history)
    share_history = pl.read_parquet(args.share_history)

    observations = _load_observations(
        args.enriched_daily,
        args.delisted_daily,
        args.start,
        args.end,
    )
    trading_dates = tuple(
        observations["date"].unique().sort().to_list()
    )
    symbols = tuple(observations["symbol"].unique().sort().to_list())
    axis_symbols = pl.DataFrame({"symbol": symbols})
    instruments = merge_research_instruments(
        current.join(axis_symbols, on="symbol", how="semi"),
        delisted.join(axis_symbols, on="symbol", how="semi"),
    )
    missing_instruments = axis_symbols.join(instruments, on="symbol", how="anti")
    if missing_instruments.height:
        raise ValueError(
            f"daily observations missing instrument metadata: "
            f"{missing_instruments['symbol'].head(20).to_list()}"
        )
    observation_mask = build_observation_mask(trading_dates, symbols, observations)
    pit_float_shares = build_pit_float_share_mask(
        trading_dates,
        symbols,
        share_history,
    )
    reversal_universe = build_research_universe(
        trading_dates,
        symbols,
        instruments,
        st_history,
        pit_valid=observation_mask,
        seasoning_months=args.seasoning_months,
    )
    turnover_universe = build_research_universe(
        trading_dates,
        symbols,
        instruments,
        st_history,
        pit_valid=observation_mask & pit_float_shares,
        seasoning_months=args.seasoning_months,
    )

    axis = pl.DataFrame({
        "symbol": symbols,
        "asset_id": pl.Series(range(len(symbols)), dtype=pl.UInt32),
    })
    symbol_audit = _symbol_audit(
        axis,
        instruments,
        st_history,
        observation_mask,
        pit_float_shares,
        reversal_universe,
        turnover_universe,
    )
    unknown = _unknown_observations(
        trading_dates,
        symbols,
        observation_mask,
        reversal_universe,
    )

    instruments.write_parquet(output / "research_instruments.parquet")
    reversal_universe.funnel.write_parquet(output / "universe_funnel_reversal.parquet")
    turnover_universe.funnel.write_parquet(output / "universe_funnel_turnover.parquet")
    symbol_audit.write_parquet(output / "symbol_audit.parquet")
    unknown.write_parquet(output / "unknown_st_observations.parquet")
    _write_manifest(
        output,
        args,
        instruments,
        observations,
        reversal_universe.funnel,
        turnover_universe.funnel,
        symbol_audit,
        unknown,
    )
    _write_report(
        output,
        args,
        observations,
        reversal_universe.funnel,
        turnover_universe.funnel,
        symbol_audit,
        unknown,
    )
    print(output)
    return 0


def _load_observations(
    enriched_daily: Path,
    delisted_daily: Path,
    start: date,
    end: date,
) -> pl.DataFrame:
    scans = [
        pl.scan_parquet(enriched_daily / "**" / "*.parquet", hive_partitioning=True),
        pl.scan_parquet(delisted_daily / "**" / "*.parquet", hive_partitioning=True),
    ]
    observations = (
        pl.concat(scans, how="diagonal_relaxed")
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .select("symbol", "date")
        .sort("date", "symbol")
        .collect()
    )
    if observations.is_empty():
        raise ValueError("research universe observations are empty")
    if observations.select("symbol", "date").is_duplicated().any():
        raise ValueError("combined daily data requires unique symbol/date rows")
    return observations


def _symbol_audit(
    axis: pl.DataFrame,
    instruments: pl.DataFrame,
    st_history: pl.DataFrame,
    observed: np.ndarray,
    pit_float_shares: np.ndarray,
    reversal_universe,
    turnover_universe,
) -> pl.DataFrame:
    first_st = (
        st_history.group_by("symbol")
        .agg(pl.col("effective_date").min().alias("first_st_observation"))
    )
    counts = pl.DataFrame({
        "asset_id": axis["asset_id"],
        "observed_days": observed.sum(axis=0),
        "st_known_observed_days": (observed & reversal_universe.st_known).sum(axis=0),
        "st_unknown_observed_days": (observed & ~reversal_universe.st_known).sum(axis=0),
        "pit_float_share_observed_days": (observed & pit_float_shares).sum(axis=0),
        "reversal_eligible_days": reversal_universe.eligible.sum(axis=0),
        "turnover_eligible_days": turnover_universe.eligible.sum(axis=0),
    })
    return (
        axis.join(instruments, on="symbol", how="left")
        .join(first_st, on="symbol", how="left")
        .join(counts, on="asset_id", how="left")
        .drop("asset_id")
        .sort("symbol")
    )


def _unknown_observations(
    trading_dates: tuple[date, ...],
    symbols: tuple[str, ...],
    observed: np.ndarray,
    universe,
) -> pl.DataFrame:
    mask = observed & ~universe.st_known
    time_ids, asset_ids = np.nonzero(mask)
    return pl.DataFrame({
        "date": [trading_dates[index] for index in time_ids],
        "symbol": [symbols[index] for index in asset_ids],
    }).sort("date", "symbol")


def _write_manifest(
    output: Path,
    args: argparse.Namespace,
    instruments: pl.DataFrame,
    observations: pl.DataFrame,
    reversal_funnel: pl.DataFrame,
    turnover_funnel: pl.DataFrame,
    symbol_audit: pl.DataFrame,
    unknown: pl.DataFrame,
) -> None:
    payload = {
        "run_type": "phase2_research_universe_audit",
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "seasoning_months": args.seasoning_months,
        "instrument_count": instruments.height,
        "observation_count": observations.height,
        "observation_symbol_count": observations["symbol"].n_unique(),
        "trading_day_count": reversal_funnel.height,
        "unknown_st_observation_count": unknown.height,
        "unknown_st_symbol_count": unknown["symbol"].n_unique() if unknown.height else 0,
        "unknown_by_exchange": (
            symbol_audit.filter(pl.col("st_unknown_observed_days") > 0)
            .group_by("exchange").len().sort("exchange").to_dicts()
        ),
        "filter_order": [
            "listed", "not_delisted", "st_known", "not_st", "seasoned", "pit_valid",
        ],
        "limitations": [
            "Reversal PIT validity requires a forward-adjusted daily price observation.",
            "Turnover PIT validity additionally requires an as-of positive float-share observation.",
            "BaoStock ST history covers Shanghai/Shenzhen only; Beijing observations remain unknown and fail closed.",
            "Adjusted delisted bars are audited here but are not yet merged into the strategy matrix.",
        ],
        "sources": {
            "current_instruments": str(args.current_instruments),
            "delisted_instruments": str(args.delisted_instruments),
            "st_history": str(args.st_history),
            "enriched_daily": str(args.enriched_daily),
            "delisted_daily": str(args.delisted_daily),
            "share_history": str(args.share_history),
        },
        "files": {},
    }
    for path in sorted(output.glob("*.parquet")):
        payload["files"][path.name] = _sha256(path)
    (output / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_report(
    output: Path,
    args: argparse.Namespace,
    observations: pl.DataFrame,
    reversal_funnel: pl.DataFrame,
    turnover_funnel: pl.DataFrame,
    symbol_audit: pl.DataFrame,
    unknown: pl.DataFrame,
) -> None:
    unknown_by_exchange = (
        unknown.with_columns(pl.col("symbol").str.split(".").list.last().alias("exchange"))
        .group_by("exchange").agg(
            pl.len().alias("observation_count"),
            pl.col("symbol").n_unique().alias("symbol_count"),
        ).sort("exchange")
        if unknown.height else pl.DataFrame()
    )
    lines = [
        "# Phase 2 research universe audit",
        "",
        f"- Interval: {args.start}..{args.end}",
        f"- Trading days: {reversal_funnel.height}",
        f"- Daily observations: {observations.height}",
        f"- Observed symbols: {observations['symbol'].n_unique()}",
        f"- Unknown-ST observations: {unknown.height}",
        f"- Unknown-ST symbols: {unknown['symbol'].n_unique() if unknown.height else 0}",
        "- Unknown ST is excluded from eligible; it is never interpreted as non-ST.",
        "- Reversal validity requires adjusted price history; turnover validity additionally requires PIT float shares.",
        "- Adjusted delisted prices are not yet merged into the strategy matrix, so this audit does not publish returns.",
        "",
        "## Unknown ST by exchange",
        "",
    ]
    lines.extend(
        f"- {row['exchange']}: {row['symbol_count']} symbols, {row['observation_count']} observations"
        for row in unknown_by_exchange.iter_rows(named=True)
    )
    lines.extend([
        "",
        "## Last-day funnels",
        "",
        f"- Reversal: {reversal_funnel.tail(1).to_dicts()[0]}",
        f"- Turnover: {turnover_funnel.tail(1).to_dicts()[0]}",
        "",
        "## Instrument source counts",
        "",
    ])
    lines.extend(
        f"- {row['instrument_source']}: {row['len']}"
        for row in symbol_audit.group_by("instrument_source").len().sort("instrument_source").iter_rows(named=True)
    )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 9, 9))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 24))
    parser.add_argument("--seasoning-months", type=int, default=6)
    parser.add_argument("--current-instruments", type=Path, default=Path("data/instruments/instruments.parquet"))
    parser.add_argument("--delisted-instruments", type=Path, default=Path("data/research/a_share_universe/20260928T-phase2-baostock-v1/delisted_instruments.parquet"))
    parser.add_argument("--st-history", type=Path, default=Path("data/research/a_share_universe/20260928T-phase2-st-history-v1/st_history.parquet"))
    parser.add_argument("--enriched-daily", type=Path, default=Path("data/kline_daily_enriched"))
    parser.add_argument("--delisted-daily", type=Path, default=Path("data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v1/delisted_daily"))
    parser.add_argument("--share-history", type=Path, default=Path("data/financials/shares/part.parquet"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    if args.seasoning_months < 0:
        parser.error("--seasoning-months must be non-negative")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
