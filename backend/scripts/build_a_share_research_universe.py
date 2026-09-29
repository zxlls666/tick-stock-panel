#!/usr/bin/env python3
"""Build isolated Phase 2 delisted-bar and PIT-ST research files from BaoStock."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

from app.backtest.research_universe_data import (
    normalize_baostock_daily,
    normalize_baostock_instruments,
    st_changes_from_daily,
)

_FIELDS = "date,code,open,high,low,close,volume,amount,tradestatus,isST"


def main() -> int:
    args = _parse_args()
    bs = _import_baostock()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"output already exists: {output}")
    raw_dir = output / "raw"
    raw_dir.mkdir(parents=True)

    login = bs.login()
    if login.error_code != "0":
        raise SystemExit(f"BaoStock login failed: {login.error_code} {login.error_msg}")
    try:
        metadata_raw = _result_frame(bs.query_stock_basic())
        metadata_raw.write_parquet(raw_dir / "stock_basic.parquet")
        _write_source_metadata(output)
        instruments = normalize_baostock_instruments(metadata_raw).filter(
            (pl.col("delist_date") >= args.start)
            & (pl.col("list_date") <= args.end)
        )
        if args.limit:
            instruments = instruments.head(args.limit)

        bars: list[pl.DataFrame] = []
        statuses: list[dict[str, object]] = []
        for row in instruments.iter_rows(named=True):
            symbol = str(row["symbol"])
            code = _baostock_code(symbol)
            try:
                raw = _result_frame(bs.query_history_k_data_plus(
                    code,
                    _FIELDS,
                    start_date=args.start.isoformat(),
                    end_date=min(args.end, row["delist_date"]).isoformat(),
                    frequency="d",
                    adjustflag="3",
                ))
                raw.write_parquet(raw_dir / f"{symbol}.parquet")
                normalized = normalize_baostock_daily(raw)
                status = "loaded" if normalized.height else "no-data"
                if normalized.height:
                    bars.append(normalized)
                statuses.append({
                    "symbol": symbol,
                    "load_status": status,
                    "bar_count": normalized.height,
                    "error": None,
                })
            except Exception as exc:
                statuses.append({
                    "symbol": symbol,
                    "load_status": "error",
                    "bar_count": 0,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                })

        status_frame = pl.DataFrame(statuses)
        instruments = (
            instruments.drop("load_status", "bar_count", "error")
            .join(status_frame, on="symbol", how="left")
            .sort("symbol")
        )
        combined = pl.concat(bars, how="vertical_relaxed") if bars else pl.DataFrame()
        st_history = st_changes_from_daily(combined) if combined.height else _empty_st_history()
        _publish(output, instruments, combined, st_history)
        _write_manifest(output, instruments, combined, st_history, args)
        _write_report(output, instruments, combined, st_history)
    finally:
        bs.logout()
    return 0


def _publish(
    output: Path,
    instruments: pl.DataFrame,
    bars: pl.DataFrame,
    st_history: pl.DataFrame,
) -> None:
    instruments.write_parquet(output / "delisted_instruments.parquet")
    st_history.write_parquet(output / "st_history.parquet")
    if bars.is_empty():
        return
    for key, frame in bars.partition_by("date", as_dict=True).items():
        day = key[0]
        partition = output / "delisted_daily" / f"date={day.isoformat()}"
        partition.mkdir(parents=True)
        frame.drop("is_st").write_parquet(partition / "part.parquet")


def _write_source_metadata(output: Path) -> None:
    (output / "source_metadata.md").write_text(
        """# Source metadata

- Provider: BaoStock
- API: `query_stock_basic`, `query_history_k_data_plus`
- Fields: `date,code,open,high,low,close,volume,amount,tradestatus,isST`
- Adjustment: `adjustflag=3` (unadjusted)
- Documentation: https://www.baostock.com/mainContent?file=stockKData.md
- Retrieved by: `backend/scripts/build_a_share_research_universe.py`

BaoStock documentation states daily history is available from 1990-12-19, `isST` is a
point-in-time daily flag, volume is in shares, amount is in CNY, and suspension rows use
zero/blank turnover fields. This run retains only rows with `tradestatus=1` and nonblank
volume/amount as tradable bars.
""",
        encoding="utf-8",
    )


def _write_report(
    output: Path,
    instruments: pl.DataFrame,
    bars: pl.DataFrame,
    st_history: pl.DataFrame,
) -> None:
    loaded = instruments.filter(pl.col("load_status") == "loaded").height
    no_data = instruments.filter(pl.col("load_status") == "no-data")
    errors = instruments.filter(pl.col("load_status") == "error")
    coverage = loaded / instruments.height if instruments.height else 0.0
    lines = [
        "# Phase 2 A-share universe data report",
        "",
        f"- Delisted instruments in research interval: {instruments.height}",
        f"- Loaded: {loaded}; no-data: {no_data.height}; error: {errors.height}",
        f"- Symbol coverage: {coverage:.2%}",
        f"- Tradable daily bars: {bars.height}",
        f"- PIT ST state changes: {st_history.height}",
        "- Price basis: unadjusted BaoStock OHLC; adj_factor is fixed at 1.0.",
        "- Suspension placeholders are excluded from tradable bars.",
        "- Beijing delisted stocks and independent corporate-action factors remain unverified.",
        "",
        "## No-data symbols",
        "",
    ]
    lines.extend(
        f"- {row['symbol']} {row['name']} ({row['list_date']}..{row['delist_date']})"
        for row in no_data.iter_rows(named=True)
    )
    lines.extend(["", "## Error symbols", ""])
    lines.extend(
        f"- {row['symbol']} {row['name']}: {row['error']}"
        for row in errors.iter_rows(named=True)
    )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_manifest(
    output: Path,
    instruments: pl.DataFrame,
    bars: pl.DataFrame,
    st_history: pl.DataFrame,
    args: argparse.Namespace,
) -> None:
    counts = (
        instruments.group_by("load_status").len().sort("load_status").to_dicts()
        if instruments.height else []
    )
    payload = {
        "source": "baostock",
        "source_url": "https://www.baostock.com/mainContent?file=stockKData.md",
        "retrieved_at": date.today().isoformat(),
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "instrument_count": instruments.height,
        "load_status_counts": counts,
        "bar_count": bars.height,
        "bar_symbol_count": bars["symbol"].n_unique() if bars.height else 0,
        "st_change_count": st_history.height,
        "limitations": [
            "BaoStock covers Shanghai/Shenzhen only; Beijing delisted stocks remain unverified.",
            "adj_factor=1 denotes unadjusted raw prices, not an independent corporate-action factor.",
            "ST status is observed on returned daily rows; days before the first row remain unknown.",
            "Suspension placeholder rows are excluded from tradable bars.",
        ],
        "files": {},
    }
    for path in sorted(output.rglob("*.parquet")):
        payload["files"][str(path.relative_to(output))] = _sha256(path)
    (output / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _result_frame(result) -> pl.DataFrame:
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock query failed: {result.error_code} {result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    return pl.DataFrame(rows, schema=result.fields, orient="row")


def _empty_st_history() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "symbol": pl.String,
        "effective_date": pl.Date,
        "st_flag": pl.Boolean,
        "st_type": pl.String,
        "name": pl.String,
        "source": pl.String,
        "source_date": pl.Date,
    })


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
            "python backend/scripts/build_a_share_research_universe.py"
        ) from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 9, 9))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 24))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0, help="Smoke-test instrument limit")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    return args


if __name__ == "__main__":
    sys.exit(main())
