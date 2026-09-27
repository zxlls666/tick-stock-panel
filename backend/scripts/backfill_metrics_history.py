#!/usr/bin/env python3
"""Backfill financials/metrics history via Fuyao indicators (resumable).

Uses local income periods as the report calendar, then calls
financial_indicators per (symbol, yyyy-N). Checkpoint:
  data/cache/metrics_history_done.txt

Usage (from backend/):
  .venv/bin/python scripts/backfill_metrics_history.py
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import polars as pl
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("metrics_hist")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT.parent / ".env")
load_dotenv(ROOT / ".env")

from app.config import settings  # noqa: E402
from app.data_providers import custom as custom_sources  # noqa: E402
from app.plugins.fuyao.provider import _to_float  # noqa: E402
from app.services.financial_sync import (  # noqa: E402
    _merge_report_history,
    _write_table,
    get_financial_df,
)


def pe_quarter(pe: str) -> int | None:
    try:
        m = int(str(pe)[5:7])
    except Exception:
        return None
    return {3: 1, 6: 2, 9: 3, 12: 4}.get(m)


def main() -> None:
    custom_sources.load_all()
    if "fuyao" not in custom_sources.names():
        raise SystemExit(f"fuyao not available: {custom_sources.list_plugins()}")

    provider = custom_sources.get_provider("fuyao")
    client = provider._get_client()
    field_map = provider._METRICS_FIELD_MAP

    data_dir = settings.data_dir
    income = pl.read_parquet(data_dir / "financials" / "income" / "part.parquet")

    done_path = data_dir / "cache" / "metrics_history_done.txt"
    done_path.parent.mkdir(parents=True, exist_ok=True)
    done = set(done_path.read_text().split()) if done_path.exists() else set()

    symbols = sorted(income["symbol"].unique().to_list())
    pending = [s for s in symbols if s not in done]
    log.info("symbols total=%d done=%d pending=%d", len(symbols), len(done), len(pending))

    existing = get_financial_df(data_dir, "metrics")
    buffer: list[dict] = []
    flush_every = 50
    interval = 0.12

    def flush(*, force: bool = False) -> None:
        nonlocal existing, buffer
        if not buffer:
            return
        if not force and len({r["symbol"] for r in buffer}) < flush_every:
            return
        new_df = pl.DataFrame(buffer, infer_schema_length=None)
        existing = _merge_report_history(existing, new_df)
        _write_table("metrics", existing, data_dir)
        with done_path.open("a") as f:
            for s in sorted({r["symbol"] for r in buffer}):
                f.write(s + "\n")
                done.add(s)
        log.info(
            "flushed +%d rows, metrics now %d rows / %d syms",
            new_df.height,
            existing.height,
            existing["symbol"].n_unique(),
        )
        buffer = []

    t0 = time.time()
    for i, sym in enumerate(pending):
        rows = income.filter(pl.col("symbol") == sym).sort("period_end", descending=True).head(20)
        for r in rows.iter_rows(named=True):
            fy = r.get("fiscal_year")
            q = pe_quarter(r.get("period_end"))
            if not fy or not q:
                continue
            report = f"{fy}-{q}"
            row: dict = {
                "symbol": sym,
                "period_end": r.get("period_end"),
                "announce_date": r.get("announce_date"),
                "eps_basic": r.get("basic_eps"),
                "bps": None,
            }
            try:
                abilities = client.financial_indicators(sym, report)
            except Exception as e:
                log.warning("indicators %s %s: %s", sym, report, e)
                abilities = []
            time.sleep(interval)
            for ability in abilities or []:
                for ind in ability.get("indicators") or []:
                    index_id = ind.get("index_id")
                    if not index_id:
                        continue
                    value = _to_float(ind.get("value"))
                    if value is not None:
                        row[field_map.get(index_id, index_id)] = value
            buffer.append(row)
        if not rows.height:
            with done_path.open("a") as f:
                f.write(sym + "\n")
            done.add(sym)
        flush()
        if (i + 1) % 20 == 0 or i == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / max(elapsed, 1)
            eta = (len(pending) - i - 1) / max(rate, 1e-6)
            log.info(
                "progress %d/%d (%.1f sym/s, eta %.0f min)",
                i + 1,
                len(pending),
                rate,
                eta / 60,
            )

    flush(force=True)
    if existing.is_empty():
        log.info("DONE empty")
        return
    per = existing.group_by("symbol").agg(pl.len().alias("n"))
    log.info(
        "DONE metrics rows=%d symbols=%d periods/symbol min=%s p50=%s max=%s",
        existing.height,
        existing["symbol"].n_unique(),
        per["n"].min(),
        per["n"].median(),
        per["n"].max(),
    )


if __name__ == "__main__":
    main()
