"""日K落后标的明细 (repository.lagging_detail / symbols_lagging) 测试。

背景: 2026-10-08 盘后观察包建在不完整的 10/8 分区上, 2 个目标静默跳过; 当时只留了
"落后标的数量", 没有名单, 事后无法点名。此测试锁定"明细必须可用于复核"。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.tickflow.repository import DataStore, KlineRepository


def _frame(symbols: list[str], dt: date) -> pl.DataFrame:
    n = len(symbols)
    return pl.DataFrame({
        "symbol": symbols,
        "date": [dt] * n,
        "close": [10.0 + i for i in range(n)],
    })


def test_lagging_detail_reports_name_last_date_and_gap(tmp_path: Path) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    repo.merge_live_daily_asset("stock", _frame(["600001.SH", "600002.SH", "600003.SH"], date(2026, 9, 30)))
    repo.merge_live_daily_asset("stock", _frame(["600001.SH", "600002.SH"], date(2026, 10, 9)))
    repo.rebuild_views()

    # cutoff = 10/6 → 只有 600003.SH (最后 9/30) 落后; gap = 9 天
    detail = repo.lagging_detail(date(2026, 10, 9), min_gap_days=3)

    assert detail["symbol"].to_list() == ["600003.SH"]
    assert detail["last_date"].to_list() == [date(2026, 9, 30)]
    assert detail["gap_days"].to_list() == [9]
    assert repo.symbols_lagging(date(2026, 10, 9), min_gap_days=3) == ["600003.SH"]


def test_lagging_detail_empty_when_everything_is_fresh(tmp_path: Path) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    repo.merge_live_daily_asset("stock", _frame(["600001.SH"], date(2026, 10, 9)))
    repo.rebuild_views()

    detail = repo.lagging_detail(date(2026, 10, 9), min_gap_days=3)

    assert detail.height == 0
    assert detail.columns == ["symbol", "last_date", "gap_days"]
