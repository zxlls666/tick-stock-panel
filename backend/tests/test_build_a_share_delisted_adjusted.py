from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl
import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "build_a_share_delisted_adjusted.py"
SPEC = importlib.util.spec_from_file_location("build_a_share_delisted_adjusted", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_load_cached_adjusted_reuses_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "600001.SH.parquet"
    pl.DataFrame({
        "date": ["2024-01-02"],
        "code": ["sh.600001"],
        "close": ["5.0"],
        "tradestatus": ["1"],
    }).write_parquet(path)

    cached = MODULE._load_cached_adjusted(path, "600001.SH")

    assert cached is not None
    assert cached.to_dicts() == [{
        "date": "2024-01-02",
        "code": "sh.600001",
        "close": "5.0",
        "tradestatus": "1",
    }]


def test_load_cached_adjusted_rejects_wrong_symbol(tmp_path: Path) -> None:
    path = tmp_path / "600001.SH.parquet"
    pl.DataFrame({
        "date": ["2024-01-02"],
        "code": ["sh.600002"],
        "close": ["5.0"],
        "tradestatus": ["1"],
    }).write_parquet(path)

    with pytest.raises(ValueError, match="unexpected BaoStock codes"):
        MODULE._load_cached_adjusted(path, "600001.SH")
