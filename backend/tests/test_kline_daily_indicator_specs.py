"""日 K 端点 indicator_specs opt-in 契约: 别名列 + 预热 + 参数校验。

覆盖两层:
- repository.get_daily: 请求 indicator_specs 时按最大周期加宽预热并补算**别名列**,
  规范列仍在; 未请求时不得出现 `_c` 别名列 (既有行为逐位不变)。
- /api/kline/daily: 解析并透传 indicator_specs; 非法参数返回 400 (fail-closed)。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kline import router
from app.indicators.custom import IndicatorSpec, parse_indicator_specs
from app.tickflow.repository import KlineRepository

SYM = "600001.SH"


def _raw_frame(days: int = 90) -> pl.DataFrame:
    base = date(2026, 4, 1)
    rows = []
    price = 10.0
    d = base
    while len(rows) < days:
        if d.weekday() < 5:
            close = price * (1 + ((len(rows) % 5) - 2) * 0.008)
            rows.append({
                "symbol": SYM, "date": d,
                "open": price, "high": max(price, close) * 1.01,
                "low": min(price, close) * 0.99, "close": close,
                "volume": 10000.0 + (len(rows) % 10) * 500.0,
                "amount": 1.0e7,
                "raw_close": close, "raw_high": max(price, close) * 1.01,
                "raw_low": min(price, close) * 0.99,
            })
            price = close
        d += timedelta(days=1)
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _bare_repo(raw: pl.DataFrame):
    repo = KlineRepository.__new__(KlineRepository)
    repo._enriched_history_cache = None
    repo._enriched_history_start = None
    repo._enriched_cache = None
    repo._enriched_cache_date = None
    repo.get_instruments = lambda: pl.DataFrame()  # type: ignore[method-assign]
    repo.get_historical_shares = lambda: pl.DataFrame()  # type: ignore[method-assign]
    repo.get_enriched_latest = lambda: (pl.DataFrame(), None)  # type: ignore[method-assign]

    def _scan(symbol, start, end, columns):
        return raw.filter((pl.col("date") >= start) & (pl.col("date") <= end))

    repo._scan_daily_symbol = _scan  # type: ignore[method-assign]
    return repo


# ── repository.get_daily ────────────────────────────────


def test_get_daily_with_indicator_specs_attaches_alias_columns() -> None:
    raw = _raw_frame()
    dates = raw["date"].to_list()
    start, end = dates[40], dates[-1]
    repo = _bare_repo(raw)

    df = repo.get_daily(SYM, start, end, indicator_specs=parse_indicator_specs("ma:13"))
    assert "ma_13_c" in df.columns
    assert "ma5" in df.columns, "规范列不得被移除或改写"
    assert df["date"].min() >= start and df["date"].max() <= end
    assert df["ma_13_c"].null_count() < df.height, "预热后应有非空别名列"


def test_get_daily_without_indicator_specs_has_no_alias_columns() -> None:
    raw = _raw_frame()
    dates = raw["date"].to_list()
    repo = _bare_repo(raw)

    df = repo.get_daily(SYM, dates[40], dates[-1])
    assert not any(c.endswith("_c") for c in df.columns)


# ── /api/kline/daily ────────────────────────────────────


class _FakeRepo:
    def __init__(self) -> None:
        self.spec_params: list = []

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_instruments(self) -> pl.DataFrame:
        return pl.DataFrame()

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None, ta_columns=None, indicator_specs=None):
        self.spec_params.append(indicator_specs)
        df = pl.DataFrame({
            "symbol": [symbol], "date": [date(2026, 9, 2)],
            "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
            "volume": [1.0e5],
        })
        for spec in indicator_specs or []:
            col = f"{spec.kind}_{'_'.join(str(p) for p in spec.params)}_c"
            df = df.with_columns(pl.lit(12.3).alias(col))
        return df


def _client() -> tuple[TestClient, _FakeRepo]:
    repo = _FakeRepo()
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    return TestClient(app), repo


def test_daily_endpoint_parses_and_passes_indicator_specs() -> None:
    client, repo = _client()

    resp = client.get("/api/kline/daily", params={"symbol": SYM, "indicator_specs": "ma:13"})
    assert resp.status_code == 200
    assert repo.spec_params[-1] == [IndicatorSpec("ma", (13,))]
    assert resp.json()["rows"][0]["ma_13_c"] == 12.3


def test_daily_endpoint_without_indicator_specs_passes_empty() -> None:
    client, repo = _client()

    resp = client.get("/api/kline/daily", params={"symbol": SYM})
    assert resp.status_code == 200
    assert repo.spec_params[-1] == []


def test_daily_endpoint_invalid_specs_returns_400() -> None:
    client, _ = _client()

    resp = client.get("/api/kline/daily", params={"symbol": SYM, "indicator_specs": "foo:5"})
    assert resp.status_code == 400
