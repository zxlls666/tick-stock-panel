#!/usr/bin/env python3
"""Build full Shanghai/Shenzhen PIT ST history from BaoStock daily flags."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

_FIELDS = "date,code,isST"
_RAW_SCHEMA = {"date": pl.String, "code": pl.String, "isST": pl.String}


def main() -> int:
    args = _parse_args()
    bs = _import_baostock()
    output = args.output.resolve()
    if output.exists() and not args.resume:
        raise SystemExit(f"output already exists: {output}; pass --resume to continue")
    raw_dir = output / "raw"
    raw_dir.mkdir(parents=True, exist_ok=args.resume)

    login = bs.login()
    if login.error_code != "0":
        raise SystemExit(f"BaoStock login failed: {login.error_code} {login.error_msg}")
    try:
        metadata = _result_frame(bs.query_stock_basic())
        stocks = _research_stocks(metadata, args.start, args.end)
        if args.limit:
            stocks = stocks.head(args.limit)

        statuses: list[dict[str, object]] = []
        raw_files: list[Path] = []
        reused = 0
        fetched = 0
        for index, row in enumerate(stocks.iter_rows(named=True), start=1):
            symbol = str(row["symbol"])
            path = raw_dir / f"{symbol}.parquet"
            try:
                raw = _load_cached_raw(path, symbol) if args.resume else None
                requested_end = min(args.end, row["delist_date"] or args.end)
                if raw is None:
                    raw = _result_frame(bs.query_history_k_data_plus(
                        _baostock_code(symbol),
                        _FIELDS,
                        start_date=max(args.start, row["list_date"]).isoformat(),
                        end_date=requested_end.isoformat(),
                        frequency="d",
                        adjustflag="3",
                    ))
                    raw.write_parquet(path)
                    fetched += 1
                else:
                    last_date = raw["date"].max()
                    incremental_start = max(
                        args.start,
                        row["list_date"],
                        date.fromisoformat(last_date),
                    )
                    if incremental_start <= requested_end:
                        incremental = _result_frame(bs.query_history_k_data_plus(
                            _baostock_code(symbol),
                            _FIELDS,
                            start_date=incremental_start.isoformat(),
                            end_date=requested_end.isoformat(),
                            frequency="d",
                            adjustflag="3",
                        ))
                        raw = _merge_incremental_raw(raw, incremental)
                        raw.write_parquet(path)
                        fetched += 1
                    else:
                        reused += 1
                raw_files.append(path)
                statuses.append({
                    "symbol": symbol,
                    "status": "loaded" if raw.height else "no-data",
                    "row_count": raw.height,
                    "error": None,
                })
            except Exception as exc:
                statuses.append({
                    "symbol": symbol,
                    "status": "error",
                    "row_count": 0,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                })
            if index % 250 == 0:
                print(
                    f"[{index}/{stocks.height}] processed; reused={reused}, fetched={fetched}",
                    flush=True,
                )

        status_frame = pl.DataFrame(statuses)
        history = _build_st_history(raw_files)
        status_frame.write_parquet(output / "status.parquet")
        history.write_parquet(output / "st_history.parquet")
        _write_manifest(output, stocks, status_frame, history, args)
    finally:
        bs.logout()
    return 0


def _research_stocks(metadata: pl.DataFrame, start: date, end: date) -> pl.DataFrame:
    return (
        metadata.filter(
            (pl.col("type") == "1")
            & (
                pl.col("code").str.starts_with("sh.")
                | pl.col("code").str.starts_with("sz.")
            )
        )
        .with_columns(
            _symbol_expr("code").alias("symbol"),
            pl.col("ipoDate").str.to_date(strict=False).alias("list_date"),
            pl.when(pl.col("outDate") == "").then(None)
            .otherwise(pl.col("outDate")).str.to_date(strict=False).alias("delist_date"),
        )
        .filter(
            (pl.col("list_date") <= end)
            & (pl.col("delist_date").is_null() | (pl.col("delist_date") >= start))
        )
        .select("symbol", "list_date", "delist_date")
        .unique("symbol")
        .sort("symbol")
    )


def _load_cached_raw(path: Path, symbol: str) -> pl.DataFrame | None:
    if not path.exists():
        return None
    raw = pl.read_parquet(path)
    missing = set(_RAW_SCHEMA) - set(raw.columns)
    if missing:
        raise ValueError(f"cached raw file missing columns: {sorted(missing)}")
    expected_code = _baostock_code(symbol)
    unexpected = raw.filter(pl.col("code") != expected_code).select("code").unique()
    if unexpected.height:
        raise ValueError(f"cached raw file has unexpected BaoStock codes: {unexpected['code'].to_list()}")
    return raw.select(*_RAW_SCHEMA)


def _merge_incremental_raw(cached: pl.DataFrame, incremental: pl.DataFrame) -> pl.DataFrame:
    return (
        pl.concat([cached, incremental], how="vertical_relaxed")
        .unique(["date", "code"], keep="last")
        .sort("date", "code")
        .select(*_RAW_SCHEMA)
    )


def _build_st_history(paths: list[Path]) -> pl.DataFrame:
    if not paths:
        return _empty_st_history()
    daily = (
        pl.scan_parquet(paths)
        .with_columns(
            _symbol_expr("code").alias("symbol"),
            pl.col("date").str.to_date(strict=False),
            (pl.col("isST") == "1").alias("st_flag"),
        )
        .select("symbol", "date", "st_flag")
        .sort("symbol", "date")
        .collect()
    )
    if daily.select("symbol", "date").is_duplicated().any():
        raise ValueError("BaoStock ST history requires unique symbol/date rows")
    return (
        daily.with_columns(pl.col("st_flag").shift(1).over("symbol").alias("_previous"))
        .filter(pl.col("_previous").is_null() | (pl.col("st_flag") != pl.col("_previous")))
        .select(
            "symbol",
            pl.col("date").alias("effective_date"),
            "st_flag",
            pl.when(pl.col("st_flag")).then(pl.lit("ST"))
            .otherwise(pl.lit("normal")).alias("st_type"),
            pl.lit(None, dtype=pl.String).alias("name"),
            pl.lit("baostock:daily_isST").alias("source"),
            pl.col("date").alias("source_date"),
        )
    )


def _write_manifest(
    output: Path,
    stocks: pl.DataFrame,
    statuses: pl.DataFrame,
    history: pl.DataFrame,
    args: argparse.Namespace,
) -> None:
    counts = statuses.group_by("status").len().sort("status").to_dicts()
    payload = {
        "source": "baostock:daily_isST",
        "source_url": "https://www.baostock.com/mainContent?file=stockKData.md",
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "instrument_count": stocks.height,
        "status_counts": counts,
        "st_change_count": history.height,
        "st_symbol_count": history["symbol"].n_unique() if history.height else 0,
        "limitations": [
            "BaoStock covers Shanghai/Shenzhen only; Beijing ST history remains unknown.",
            "A symbol is unknown before its first returned daily ST observation.",
            "Daily isST states are independent of current instrument names.",
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
    schema = result.fields
    return pl.DataFrame(rows, schema=schema, orient="row") if rows else pl.DataFrame(schema={name: pl.String for name in schema})


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


def _symbol_expr(column: str) -> pl.Expr:
    return pl.col(column).str.slice(3, 6) + pl.lit(".") + pl.col(column).str.slice(0, 2).str.to_uppercase()


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
            "python backend/scripts/build_a_share_st_history.py"
        ) from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 9, 9))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 24))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0, help="Smoke-test instrument limit")
    parser.add_argument("--resume", action="store_true", help="Reuse valid raw parquet files")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    return args


if __name__ == "__main__":
    sys.exit(main())
