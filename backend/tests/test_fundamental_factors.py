"""财务因子点时接入测试: 公告日门控 / 无数据 null 安全 / 双路径一致 / 历史累积同步。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.fundamentals import (
    FUNDAMENTAL_FACTOR_NAMES,
    attach_fundamental_factors,
    build_fundamental_matrices,
    default_announce_date,
    load_fundamental_snapshot,
    sanitize_announce_dates,
)
from app.backtest.matrix import build_market_data_matrix


def _snapshot_frame(rows: list[dict]) -> pl.DataFrame:
    frame = pl.DataFrame({
        "period_end": ["2026-03-31"] * len(rows),
        "symbol": [r["symbol"] for r in rows],
        "announce_date": [r["announce"] for r in rows],
        "roe": [r.get("roe", 10.0) for r in rows],
        "bps": [r.get("bps", 5.0) for r in rows],
        "revenue_yoy": [r.get("revenue_yoy", 8.0) for r in rows],
    })
    return (
        frame.with_columns(
            pl.col("announce_date").str.slice(0, 10).str.to_date().alias("_announce")
        )
        .sort(["symbol", "_announce"])
    )


def _daily_panel(start: date, days: int, symbols: tuple[str, ...]) -> pl.DataFrame:
    rows = []
    for offset in range(days):
        for symbol in symbols:
            rows.append({
                "symbol": symbol,
                "date": start + timedelta(days=offset),
                "open": 10.0,
                "high": 10.5,
                "low": 9.5,
                "close": 10.0 + offset * 0.1,
                "volume": 1000.0,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def test_attach_gates_on_announce_date_strictly():
    panel = _daily_panel(date(2026, 4, 1), 10, ("600000.SH", "000001.SZ"))
    snapshot = _snapshot_frame([
        # 公告日 4-5: 4-5 当天不可用, 4-6 起 roe=20
        {"symbol": "600000.SH", "announce": "2026-04-05", "roe": 20.0, "bps": 4.0},
        # 无财务数据的标的
    ])
    attached = attach_fundamental_factors(panel, snapshot, ["roe_latest", "pb_latest"])

    values = attached.filter(pl.col("symbol") == "600000.SH").sort("date")
    roe = values["roe_latest"].to_list()
    assert roe[:5] == [None] * 5  # 4-1 ~ 4-5 均不可用 (含公告当日)
    assert roe[5:] == [20.0] * 5  # 4-6 起生效

    # 无财务数据标的全 null, 绝不填 0
    other = attached.filter(pl.col("symbol") == "000001.SZ")["roe_latest"]
    assert other.null_count() == other.len()

    # pb = close / bps, 公告前 null
    pb = values["pb_latest"].to_list()
    assert pb[:5] == [None] * 5
    close_6 = values.filter(pl.col("date") == date(2026, 4, 6))["close"].item()
    assert abs(pb[5] - close_6 / 4.0) < 1e-12


def test_attach_replaces_with_newer_announcement():
    panel = _daily_panel(date(2026, 4, 1), 20, ("600000.SH",))
    snapshot = _snapshot_frame([
        {"symbol": "600000.SH", "announce": "2026-04-05", "roe": 20.0},
        {"symbol": "600000.SH", "announce": "2026-04-15", "roe": 33.0},
    ])
    attached = attach_fundamental_factors(panel, snapshot, ["roe_latest"]).sort("date")
    roe = attached["roe_latest"].to_list()
    assert roe[4] is None            # 4-5 公告日
    assert roe[5] == 20.0            # 4-6 起 20.0
    assert roe[13] == 20.0           # 4-14
    assert roe[14] is None           # 4-15 二次公告日, 当天仍不可用 (严格大于)
    assert roe[15] == 33.0           # 4-16 起新公告生效


def test_attach_without_snapshot_keeps_null_columns():
    panel = _daily_panel(date(2026, 4, 1), 5, ("600000.SH",))
    attached = attach_fundamental_factors(panel, None, ["roe_latest", "pb_latest"])
    for name in ("roe_latest", "pb_latest"):
        assert name in attached.columns
        assert attached[name].null_count() == attached.height


def test_matrix_field_matches_polars_attach():
    panel = _daily_panel(date(2026, 4, 1), 12, ("600000.SH", "000001.SZ"))
    snapshot = _snapshot_frame([
        {"symbol": "600000.SH", "announce": "2026-04-05", "roe": 20.0, "bps": 4.0},
        {"symbol": "000001.SZ", "announce": "2026-04-08", "roe": -5.0, "bps": -1.0},
    ])
    attached = attach_fundamental_factors(panel, snapshot, ["roe_latest", "pb_latest"])
    market = build_market_data_matrix(panel)
    matrices = build_fundamental_matrices(market, snapshot, ["roe_latest", "pb_latest"])
    symbols = {s: i for i, s in enumerate(market.symbols)}
    dates = {str(d): t for t, d in enumerate(
        sorted({row["date"] for row in panel.iter_rows(named=True)})
    )}
    for name in ("roe_latest", "pb_latest"):
        matrix = matrices[name]
        for row in attached.iter_rows(named=True):
            expected = row[name]
            actual = matrix[dates[str(row["date"])], symbols[row["symbol"]]]
            if expected is None:
                assert np.isnan(actual), (name, row["date"], row["symbol"], actual)
            else:
                np.testing.assert_allclose(actual, expected, rtol=1e-6)


def test_bps_nonpositive_gives_null_pb():
    panel = _daily_panel(date(2026, 4, 1), 8, ("000001.SZ",))
    snapshot = _snapshot_frame([
        {"symbol": "000001.SZ", "announce": "2026-04-02", "bps": -1.0},
    ])
    attached = attach_fundamental_factors(panel, snapshot, ["pb_latest"])
    assert attached["pb_latest"].null_count() == attached.height


def test_load_snapshot_from_missing_dir_returns_none(tmp_path: Path):
    assert load_fundamental_snapshot(tmp_path) is None
    assert load_fundamental_snapshot(None) is None


def test_default_announce_date_by_quarter():
    assert default_announce_date(date(2024, 3, 31)) == date(2024, 4, 30)
    assert default_announce_date(date(2024, 6, 30)) == date(2024, 8, 31)
    assert default_announce_date(date(2024, 9, 30)) == date(2024, 10, 31)
    assert default_announce_date(date(2024, 12, 31)) == date(2025, 4, 30)


def test_sanitize_replaces_implausible_lag():
    frame = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ", "000002.SZ"],
        "period_end": ["2024-03-31", "2025-09-30", "2024-12-31"],
        # +1y 脏数据 / 正常滞后 / 年报合理滞后
        "announce_date": ["2025-04-19", "2025-10-25", "2025-03-21"],
        "roe": [3.0, 8.0, 9.0],
    })
    cleaned = sanitize_announce_dates(frame)
    assert cleaned["announce_date"].to_list() == [
        "2024-04-30",  # 脏 → 法定 Q1 截止
        "2025-10-25",  # 保留
        "2025-03-21",  # 保留
    ]


def test_load_snapshot_sanitizes_fuyao_lag(tmp_path: Path):
    out = tmp_path / "financials" / "metrics"
    out.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "period_end": ["2024-06-30"],
        "announce_date": ["2025-08-15"],  # 滞后 >180d
        "roe": [10.0],
        "gross_margin": [30.0],
        "net_margin": [5.0],
        "revenue_yoy": [8.0],
        "net_income_yoy": [6.0],
        "debt_to_asset_ratio": [40.0],
        "net_profit_cash_content": [80.0],
        "bps": [5.0],
    }).write_parquet(out / "part.parquet")
    snap = load_fundamental_snapshot(tmp_path)
    assert snap is not None
    assert snap["_announce"].to_list() == [date(2024, 8, 31)]


def test_snapshot_requires_announce_date(tmp_path: Path):
    out = tmp_path / "financials" / "metrics"
    out.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "announce_date": [None],
        "roe": [10.0],
        "gross_margin": [30.0],
        "net_margin": [5.0],
        "revenue_yoy": [8.0],
        "net_income_yoy": [6.0],
        "debt_to_asset_ratio": [40.0],
        "net_profit_cash_content": [80.0],
        "bps": [5.0],
    }).write_parquet(out / "part.parquet")
    # 无 period_end 且公告日缺失 → 无法门控, 视为无有效快照
    assert load_fundamental_snapshot(tmp_path) is None


def test_fundamental_factor_names_are_catalogued():
    from app.backtest.factor import FACTOR_COLUMNS

    catalog_ids = {item["id"] for item in FACTOR_COLUMNS}
    assert catalog_ids >= FUNDAMENTAL_FACTOR_NAMES


def test_financial_sync_merges_history(tmp_path: Path):
    from app.services import financial_sync as fs

    old = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "period_end": ["2025-09-30", "2025-12-31"],
        "announce_date": ["2025-10-28", "2026-01-20"],
        "roe": [8.0, 9.0],
    })
    latest = pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "period_end": ["2026-03-31", "2026-03-31"],
        "announce_date": ["2026-04-25", "2026-04-24"],
        "roe": [10.0, 5.0],
    })
    merged = fs._merge_report_history(old, latest)
    assert merged.height == 4  # 旧各期保留 + 新一期并入
    # 同期修正: 旧 2025-12-31 公告 2026-01-20 vs 更晚的修正公告
    revised = pl.DataFrame({
        "symbol": ["600000.SH"],
        "period_end": ["2025-12-31"],
        "announce_date": ["2026-02-01"],
        "roe": [9.5],
    })
    merged2 = fs._merge_report_history(old, revised)
    row = merged2.filter(
        (pl.col("symbol") == "600000.SH") & (pl.col("period_end") == "2025-12-31")
    )
    assert row.height == 1
    assert row["roe"].item() == 9.5
    assert merged2.height == 2  # 修正不增加行数


# 审计选定的公告日财务因子。数值是百分数, 必须原样进入面板。
_BASELINE_PERCENT_FACTORS = {
    "roe_latest": 16.75,
    "gross_margin_latest": 25.504,
    "net_margin_latest": 6.2463,
    "revenue_yoy_latest": 5.288939,
    "net_income_yoy_latest": 2.233075,
    "debt_ratio_latest": 39.8181,
    "ocf_to_ni_latest": 77.680954,
}


def test_baseline_percent_factors_stay_percent_and_null():
    panel = _daily_panel(date(2026, 4, 1), 8, ("600519.SH", "000001.SZ"))
    snapshot = pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"],
        "announce_date": ["2026-04-03", "2026-04-03"],
        "roe": [16.75, None],
        "gross_margin": [25.504, None],
        "net_margin": [6.2463, None],
        "revenue_yoy": [5.288939, None],
        "net_income_yoy": [2.233075, None],
        "debt_to_asset_ratio": [39.8181, 0.0],
        "net_profit_cash_content": [77.680954, None],
        "bps": [200.99, None],
    }).with_columns(
        pl.col("announce_date").str.to_date().alias("_announce")
    ).sort(["symbol", "_announce"])

    names = list(_BASELINE_PERCENT_FACTORS)
    attached = attach_fundamental_factors(panel, snapshot, names)
    moutai = attached.filter(pl.col("symbol") == "600519.SH").sort("date")
    # 4-1, 4-2, 4-3 公告日当天仍为空
    before = moutai.filter(pl.col("date") <= date(2026, 4, 3))
    after = moutai.filter(pl.col("date") >= date(2026, 4, 4))
    for name, expected in _BASELINE_PERCENT_FACTORS.items():
        assert before[name].null_count() == before.height
        assert after[name].to_list() == [expected] * after.height
        assert all(value is None or abs(value) > 1 for value in after[name].to_list())

    missing = attached.filter(pl.col("symbol") == "000001.SZ")
    for name in _BASELINE_PERCENT_FACTORS:
        if name == "debt_ratio_latest":
            visible = missing.filter(pl.col("date") >= date(2026, 4, 4))
            assert visible["debt_ratio_latest"].to_list() == [0.0] * visible.height
        else:
            assert missing[name].null_count() == missing.height

    market = build_market_data_matrix(panel)
    matrices = build_fundamental_matrices(market, snapshot, names)
    symbols = {symbol: index for index, symbol in enumerate(market.symbols)}
    dates = [date.fromisoformat(label[:10]) for label in market.timestamp_labels]
    for name, expected in _BASELINE_PERCENT_FACTORS.items():
        column = symbols["600519.SH"]
        for row_index, day in enumerate(dates):
            actual = matrices[name][row_index, column]
            if day <= date(2026, 4, 3):
                assert np.isnan(actual)
            else:
                np.testing.assert_allclose(actual, expected, rtol=1e-5)
