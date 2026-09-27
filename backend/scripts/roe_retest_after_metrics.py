#!/usr/bin/env python3
"""ROE single-factor re-test after metrics history + announce_date sanitization."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from app.config import settings
from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task


def summarize(out: dict, label: str) -> dict:
    stats = out.get("stats") or {}
    trades = out.get("trades") or []
    by: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    for t in trades:
        ed = str(t.get("entry_date") or t.get("entry_day") or "")[:4]
        if not ed:
            continue
        by[ed]["n"] += 1
        pnl = t.get("pnl_pct")
        if pnl is None:
            pnl = t.get("return_pct")
        if isinstance(pnl, (int, float)) and pnl > 0:
            by[ed]["wins"] += 1
    compact = {
        "label": label,
        "sharpe": stats.get("sharpe"),
        "total_return": stats.get("total_return"),
        "max_drawdown": stats.get("max_drawdown"),
        "n_trades": stats.get("n_trades"),
        "win_rate": stats.get("win_rate"),
        "mc_maxdd_p95": stats.get("mc_maxdd_p95"),
        "trades_by_year": dict(sorted(by.items())),
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2), flush=True)
    return compact


def bt(start: date, end: date, label: str) -> tuple[dict, dict]:
    cfg = StrategyBacktestConfig(
        strategy_id="custom_factor_roe_latest",
        symbols=None,
        start=start,
        end=end,
        matching="open_t+1",
        commission_pct=0.0002,
        stamp_tax_pct=0.0005,
        slippage_bps=5.0,
        max_positions=10,
        initial_capital=1_000_000.0,
        position_sizing="equal",
        mode="position",
        asset_type="stock",
    )
    task = make_worker_task("backtest", settings.data_dir, cfg)
    print(f"\n=== {label} {start} -> {end} ===", flush=True)
    out = run_worker_task(task)
    return summarize(out, label), out


def main() -> None:
    full_c, full = bt(date(2023, 6, 13), date(2026, 9, 10), "full")
    train_c, _ = bt(date(2023, 6, 13), date(2026, 1, 13), "train")
    hold_c, _ = bt(date(2026, 1, 14), date(2026, 9, 10), "holdout")
    Path("/tmp/tsp_roe_retest.json").write_text(
        json.dumps(
            {"full": full_c, "train": train_c, "holdout": hold_c},
            ensure_ascii=False,
            indent=2,
        )
    )
    Path("/tmp/tsp_bt_custom_factor_roe_latest_r2.json").write_text(
        json.dumps(full, ensure_ascii=False, default=str)
    )
    print("\nWROTE /tmp/tsp_roe_retest.json")


if __name__ == "__main__":
    main()
