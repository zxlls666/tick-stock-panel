"""Phase 0 read-only audit: freeze research data fingerprints.

Does not mutate market data or strategy definitions. Writes a baseline
manifest under data/research so later phases can prove they started from
the same snapshot.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import polars as pl

from app.backtest.research_run_manifest import (
    build_research_run_manifest,
    file_fingerprint,
    read_git_identity,
    stable_json_sha256,
    write_research_run_manifest,
)
from app.tickflow.repository import DataStore, KlineRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_BASELINE_RUN = "20260928T-full-history-v1"
DEFAULT_OUTPUT_RUN = "20260928T-phase0-baseline"
BENCHMARKS = ("000985.SH", "000300.SH", "000905.SH", "000852.SH")
STRATEGY_WINDOW = (date(2020, 9, 9), date(2026, 9, 24))

KNOWN_DEGRADATIONS = (
    "stock_universe_missing_delisted_history",
    "st_status_from_current_name_not_pit",
    "float_shares_current_fallback_allowed",
    "benchmark_index_history_incomplete",
    "no_adv_capacity_or_impact_model",
    "queue_model_unavailable_day_bar_only",
)


def _enriched_partition_dates(data_dir: Path) -> list[str]:
    root = data_dir / "kline_daily_enriched"
    if not root.exists():
        return []
    dates = []
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("date="):
            dates.append(path.name.split("=", 1)[1])
    return sorted(dates)


def _benchmark_coverage(
    repo: KlineRepository,
    *,
    start: date,
    end: date,
) -> dict[str, dict[str, object]]:
    coverage: dict[str, dict[str, object]] = {}
    for symbol in BENCHMARKS:
        frame = repo.get_index_daily(
            symbol,
            start,
            end,
            columns=["symbol", "date", "open", "high", "low", "close"],
        )
        if frame.is_empty():
            coverage[symbol] = {
                "rows": 0,
                "min_date": None,
                "max_date": None,
                "duplicate_keys": 0,
                "invalid_ohlc": 0,
            }
            continue
        keys = frame.select(["symbol", "date"])
        duplicate_keys = keys.height - keys.unique().height
        invalid_ohlc = frame.filter(
            pl.col("open").is_null()
            | pl.col("high").is_null()
            | pl.col("low").is_null()
            | pl.col("close").is_null()
            | (pl.col("close") <= 0)
        ).height
        coverage[symbol] = {
            "rows": int(frame.height),
            "min_date": str(frame["date"].min()),
            "max_date": str(frame["date"].max()),
            "duplicate_keys": int(duplicate_keys),
            "invalid_ohlc": int(invalid_ohlc),
        }
    return coverage


def _shares_stats(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"rows": 0, "symbols": 0, "min_period_end": None, "max_period_end": None}
    frame = pl.read_parquet(path)
    period = frame.select(pl.col("period_end").cast(pl.Utf8))
    return {
        "rows": int(frame.height),
        "symbols": int(frame["symbol"].n_unique()),
        "min_period_end": period["period_end"].min(),
        "max_period_end": period["period_end"].max(),
    }


def audit_baseline(
    *,
    data_dir: Path,
    repo_root: Path,
    baseline_run_id: str,
    output_run_id: str,
) -> dict[str, object]:
    generation_before = None
    store = DataStore(data_dir)
    repo = KlineRepository(store)
    generation_before = repo.get_matrix_data_generation("stock")

    instruments_path = data_dir / "instruments" / "instruments.parquet"
    shares_path = data_dir / "financials" / "shares" / "part.parquet"
    baseline_dir = (
        data_dir / "research" / "a_share_strategy_validation" / baseline_run_id
    )
    output_dir = (
        data_dir / "research" / "a_share_strategy_validation" / output_run_id
    )

    instruments = (
        pl.read_parquet(instruments_path)
        if instruments_path.exists()
        else pl.DataFrame()
    )
    shares_stats = _shares_stats(shares_path)
    enriched_dates = _enriched_partition_dates(data_dir)
    start, end = STRATEGY_WINDOW
    benchmarks = _benchmark_coverage(repo, start=start, end=end)

    research_files = {}
    for name in (
        "manifest.json",
        "summary.parquet",
        "daily.parquet",
        "yearly_returns.parquet",
        "report.md",
    ):
        path = baseline_dir / name
        rows = None
        if path.exists() and path.suffix == ".parquet":
            rows = int(pl.read_parquet(path).height)
        research_files[name] = file_fingerprint(path, rows=rows)

    generation_after = repo.get_matrix_data_generation("stock")
    if generation_before != generation_after:
        raise RuntimeError(
            "data generation changed during audit; refusing to freeze baseline"
        )

    code = read_git_identity(repo_root)
    data = {
        "data_generation": generation_before,
        "strategy_window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        "market": {
            "enriched_trading_days": len(enriched_dates),
            "enriched_min_date": enriched_dates[0] if enriched_dates else None,
            "enriched_max_date": enriched_dates[-1] if enriched_dates else None,
            "expected_shape": [1466, 5569],
            "expected_generation": "0b7900c875e347088914a263585b81e5",
            "generation_matches_full_history_run": (
                generation_before == "0b7900c875e347088914a263585b81e5"
            ),
        },
        "instruments": {
            **file_fingerprint(instruments_path, rows=int(instruments.height)),
            "symbol_count": (
                int(instruments["symbol"].n_unique())
                if not instruments.is_empty() and "symbol" in instruments.columns
                else 0
            ),
        },
        "shares": {
            **file_fingerprint(shares_path, rows=int(shares_stats["rows"])),
            **shares_stats,
        },
        "benchmarks": benchmarks,
        "prior_full_history_run": {
            "run_id": baseline_run_id,
            "files": research_files,
            "summary_rows_expected": 52,
        },
    }
    parameters = {
        "phase": 0,
        "audit_mode": "read_only_fingerprint",
        "benchmark_symbols": list(BENCHMARKS),
        "baseline_full_history_run_id": baseline_run_id,
    }
    sources = {
        "instruments": "data/instruments/instruments.parquet",
        "shares": "data/financials/shares/part.parquet",
        "enriched": "data/kline_daily_enriched/date=*/",
        "index_daily": "data/kline_index_daily/",
        "prior_research_run": baseline_dir.as_posix(),
    }

    manifest = build_research_run_manifest(
        run_id=output_run_id,
        run_type="phase0_data_baseline_audit",
        code=code,
        data=data,
        parameters=parameters,
        degradations=KNOWN_DEGRADATIONS,
        sources=sources,
        extras={
            "entry_condition": {
                "prior_commit_expected": "8fab63150b1687c2f91e2db11ee81836e63e657c",
                "prior_commit_observed": code["git_commit"],
                "prior_full_history_generation": "0b7900c875e347088914a263585b81e5",
            },
        },
    )
    content_sha = stable_json_sha256(manifest)
    manifest["fingerprint"] = {"content_sha256": content_sha}

    output_dir.mkdir(parents=True, exist_ok=True)
    write_research_run_manifest(output_dir / "manifest.json", manifest)
    (output_dir / "audit.json").write_text(
        json.dumps(
            {
                "content_sha256": content_sha,
                "data_generation": generation_before,
                "enriched_trading_days": len(enriched_dates),
                "benchmark_rows": {
                    symbol: info["rows"] for symbol, info in benchmarks.items()
                },
                "shares_rows": shares_stats["rows"],
                "instruments_rows": int(instruments.height),
                "summary_sha256": research_files["summary.parquet"]["sha256"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--baseline-run-id", default=DEFAULT_BASELINE_RUN)
    parser.add_argument("--output-run-id", default=DEFAULT_OUTPUT_RUN)
    args = parser.parse_args()
    manifest = audit_baseline(
        data_dir=args.data_dir.resolve(),
        repo_root=args.repo_root.resolve(),
        baseline_run_id=args.baseline_run_id,
        output_run_id=args.output_run_id,
    )
    print(json.dumps({
        "run_id": manifest["run_id"],
        "content_sha256": manifest["fingerprint"]["content_sha256"],
        "data_generation": manifest["data"]["data_generation"],
        "enriched_trading_days": manifest["data"]["market"]["enriched_trading_days"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
