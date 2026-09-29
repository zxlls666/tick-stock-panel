#!/usr/bin/env python3
"""Build forward-adjusted research bars for frozen BaoStock delisted data."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from pathlib import Path

import polars as pl

from app.backtest.research_universe_data import apply_baostock_forward_adjustment

_FIELDS = "date,code,close,tradestatus"
_RAW_SCHEMA = {name: pl.String for name in _FIELDS.split(",")}


def main() -> int:
    args = _parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if output.exists() and not args.resume:
        raise SystemExit(f"output already exists: {output}; pass --resume to continue")
    instruments_path = source / "delisted_instruments.parquet"
    daily_glob = source / "delisted_daily" / "**" / "*.parquet"
    if not instruments_path.exists():
        raise SystemExit(f"missing source instruments: {instruments_path}")
    source_files = list((source / "delisted_daily").rglob("*.parquet"))
    if not source_files:
        raise SystemExit(f"missing source bars: {daily_glob}")

    raw_dir = output / "raw_forward_adjusted"
    raw_dir.mkdir(parents=True, exist_ok=args.resume)
    instruments = pl.read_parquet(instruments_path)
    bars = pl.scan_parquet(source_files).collect().sort("symbol", "date")
    by_symbol = {
        key[0]: frame
        for key, frame in bars.partition_by("symbol", as_dict=True).items()
    }
    symbols = sorted(by_symbol)
    if args.limit:
        symbols = symbols[:args.limit]

    bs = _import_baostock()
    login = bs.login()
    if login.error_code != "0":
        raise SystemExit(f"BaoStock login failed: {login.error_code} {login.error_msg}")
    adjusted_frames: list[pl.DataFrame] = []
    statuses: list[dict[str, object]] = []
    reused = fetched = 0
    try:
        for index, symbol in enumerate(symbols, start=1):
            raw_bars = by_symbol[symbol]
            cache_path = raw_dir / f"{symbol}.parquet"
            try:
                adjusted = _load_cached_adjusted(cache_path, symbol) if args.resume else None
                if adjusted is None:
                    adjusted = _result_frame(bs.query_history_k_data_plus(
                        _baostock_code(symbol),
                        _FIELDS,
                        start_date=raw_bars["date"].min().isoformat(),
                        end_date=raw_bars["date"].max().isoformat(),
                        frequency="d",
                        adjustflag="2",
                    ))
                    adjusted.write_parquet(cache_path)
                    fetched += 1
                else:
                    reused += 1
                normalized = apply_baostock_forward_adjustment(raw_bars, adjusted)
                adjusted_frames.append(normalized)
                statuses.append({
                    "symbol": symbol,
                    "status": "loaded",
                    "row_count": normalized.height,
                    "error": None,
                })
            except Exception as exc:
                statuses.append({
                    "symbol": symbol,
                    "status": "error",
                    "row_count": 0,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                })
            if index % 50 == 0:
                print(
                    f"[{index}/{len(symbols)}] processed; reused={reused}, fetched={fetched}",
                    flush=True,
                )
    finally:
        bs.logout()

    status = pl.DataFrame(statuses)
    combined = pl.concat(adjusted_frames, how="vertical_relaxed") if adjusted_frames else pl.DataFrame()
    _publish(output, instruments, status, combined)
    _write_manifest(output, source, instruments, status, combined, args)
    _write_report(output, source, status, combined)
    return 0 if status.filter(pl.col("status") == "error").is_empty() else 1


def _publish(
    output: Path,
    instruments: pl.DataFrame,
    status: pl.DataFrame,
    bars: pl.DataFrame,
) -> None:
    instruments.write_parquet(output / "delisted_instruments.parquet")
    status.write_parquet(output / "status.parquet")
    if bars.is_empty():
        return
    for key, frame in bars.partition_by("date", as_dict=True).items():
        partition = output / "delisted_daily" / f"date={key[0].isoformat()}"
        partition.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(partition / "part.parquet")


def _load_cached_adjusted(path: Path, symbol: str) -> pl.DataFrame | None:
    if not path.exists():
        return None
    raw = pl.read_parquet(path)
    missing = set(_RAW_SCHEMA) - set(raw.columns)
    if missing:
        raise ValueError(f"cached adjusted file missing columns: {sorted(missing)}")
    expected_code = _baostock_code(symbol)
    unexpected = raw.filter(pl.col("code") != expected_code).select("code").unique()
    if unexpected.height:
        raise ValueError(
            f"cached adjusted file has unexpected BaoStock codes: {unexpected['code'].to_list()}"
        )
    return raw.select(*_RAW_SCHEMA)


def _write_manifest(
    output: Path,
    source: Path,
    instruments: pl.DataFrame,
    status: pl.DataFrame,
    bars: pl.DataFrame,
    args: argparse.Namespace,
) -> None:
    payload = {
        "source": "baostock:query_history_k_data_plus:adjustflag=2",
        "source_url": "https://www.baostock.com/mainContent?file=stockKData.md",
        "source_snapshot": str(source),
        "source_manifest_sha256": _sha256(source / "manifest.json"),
        "adjustment_method": "factor = forward_adjusted_close / raw_close; factor applied to raw OHLC",
        "volume_normalization": "floor(BaoStock shares / 100) to internal hands contract",
        "instrument_count": instruments.height,
        "requested_symbol_count": status.height,
        "status_counts": status.group_by("status").len().sort("status").to_dicts(),
        "bar_count": bars.height,
        "bar_symbol_count": bars["symbol"].n_unique() if bars.height else 0,
        "limitations": [
            "BaoStock uses a return-based adjustment algorithm that may differ from other vendors.",
            "Only symbols with frozen unadjusted source bars are adjusted.",
            "Beijing delisted stocks remain outside this snapshot.",
        ],
        "files": {},
    }
    for path in sorted(output.rglob("*.parquet")):
        payload["files"][str(path.relative_to(output))] = _sha256(path)
    (output / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_report(output: Path, source: Path, status: pl.DataFrame, bars: pl.DataFrame) -> None:
    loaded = status.filter(pl.col("status") == "loaded")
    errors = status.filter(pl.col("status") == "error")
    factor_stats = (
        bars.select(
            pl.col("adj_factor").min().alias("min"),
            pl.col("adj_factor").max().alias("max"),
            (pl.col("adj_factor") != 1.0).sum().alias("adjusted_rows"),
        ).to_dicts()[0]
        if bars.height else {"min": None, "max": None, "adjusted_rows": 0}
    )
    lines = [
        "# Phase 2 delisted forward-adjustment report",
        "",
        f"- Frozen raw source: `{source}`",
        f"- Loaded symbols: {loaded.height}; errors: {errors.height}",
        f"- Adjusted bars: {bars.height}",
        f"- Factor range: {factor_stats['min']}..{factor_stats['max']}",
        f"- Rows with factor != 1: {factor_stats['adjusted_rows']}",
        "- Method: BaoStock adjustflag=2 close / frozen raw close, applied uniformly to raw OHLC.",
        "- Volume: BaoStock shares converted to internal hands using floor(shares / 100).",
        "- `raw_close`, `raw_high`, and `raw_low` preserve execution and price-limit prices.",
        "",
        "## Errors",
        "",
    ]
    lines.extend(f"- {row['symbol']}: {row['error']}" for row in errors.iter_rows(named=True))
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _result_frame(result) -> pl.DataFrame:
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock query failed: {result.error_code} {result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    return (
        pl.DataFrame(rows, schema=result.fields, orient="row")
        if rows else pl.DataFrame(schema=_RAW_SCHEMA)
    )


def _baostock_code(symbol: str) -> str:
    code, exchange = symbol.split(".")
    return f"{exchange.lower()}.{code}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _import_baostock():
    try:
        return importlib.import_module("baostock")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "baostock is required; run with: uv run --project backend --with baostock "
            "python backend/scripts/build_a_share_delisted_adjusted.py"
        ) from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0, help="Smoke-test symbol limit")
    parser.add_argument("--resume", action="store_true", help="Reuse valid raw parquet files")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    return args


if __name__ == "__main__":
    sys.exit(main())
