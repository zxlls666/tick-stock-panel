from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl
import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "build_a_share_st_history.py"
SPEC = importlib.util.spec_from_file_location("build_a_share_st_history", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_load_cached_raw_reuses_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "600000.SH.parquet"
    pl.DataFrame({
        "date": ["2020-09-09"],
        "code": ["sh.600000"],
        "isST": ["0"],
    }).write_parquet(path)

    cached = MODULE._load_cached_raw(path, "600000.SH")

    assert cached is not None
    assert cached.to_dicts() == [{
        "date": "2020-09-09",
        "code": "sh.600000",
        "isST": "0",
    }]


def test_merge_incremental_raw_appends_new_dates_without_duplicates() -> None:
    cached = pl.DataFrame({
        "date": ["2026-09-24"],
        "code": ["sh.600000"],
        "isST": ["0"],
    })
    incremental = pl.DataFrame({
        "date": ["2026-09-24", "2026-09-28", "2026-09-29"],
        "code": ["sh.600000"] * 3,
        "isST": ["0", "1", "1"],
    })

    merged = MODULE._merge_incremental_raw(cached, incremental)

    assert merged.to_dicts() == [
        {"date": "2026-09-24", "code": "sh.600000", "isST": "0"},
        {"date": "2026-09-28", "code": "sh.600000", "isST": "1"},
        {"date": "2026-09-29", "code": "sh.600000", "isST": "1"},
    ]


def test_load_cached_raw_rejects_wrong_symbol(tmp_path: Path) -> None:
    path = tmp_path / "600000.SH.parquet"
    pl.DataFrame({
        "date": ["2020-09-09"],
        "code": ["sh.600001"],
        "isST": ["0"],
    }).write_parquet(path)

    with pytest.raises(ValueError, match="unexpected BaoStock codes"):
        MODULE._load_cached_raw(path, "600000.SH")
