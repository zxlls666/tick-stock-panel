"""日 K 端点 ta_columns opt-in 契约: 预热 + 裁剪 + 参数校验。

覆盖两层:
- repository.get_daily: 请求 ta_columns 时在含 warmup 的整段上补算, 再裁剪到 start..end;
  未请求时不得出现 ta 列 (既有行为逐位不变)。
- /api/kline/daily: 解析并透传 ta_columns (仅认已注册 id), 结果随行返回 ta 列。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kline import router
from app.factors import ta_factors
from app.tickflow.repository import KlineRepository

SYM = "600001.SH"


def _require_ta() -> None:
    if not ta_factors.available():
        pytest.skip("polars-talib (可选 extra `ta`) 未安装")


def _raw_frame(days: int = 80) -> pl.DataFrame:
    """构造有涨跌交替的 14 列形态数据, 使 ADX 有方向性运动 (非全 NaN)。"""
    base = date(2026, 4, 1)
    rows = []
    price = 10.0
    d = base
    while len(rows) < days:
        if d.weekday() < 5:
            open_ = price * (1 + ((len(rows) % 7) - 3) * 0.006)
            close = price * (1 + ((len(rows) % 5) - 2) * 0.008)
            high = max(open_, close) * 1.01
            low = min(open_, close) * 0.99
            price = close
            rows.append({
                "symbol": SYM, "date": d,
                "open": round(open_, 4), "high": round(high, 4),
                "low": round(low, 4), "close": round(close, 4),
                "volume": 10000.0 + (len(rows) % 10) * 500.0,
                "amount": 1.0e7,
                "raw_close": round(close, 4), "raw_high": round(high, 4),
                "raw_low": round(low, 4),
            })
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
    calls = {"scan": 0}

    def _scan(symbol, start, end, columns):
        calls["scan"] += 1
        return raw.filter((pl.col("date") >= start) & (pl.col("date") <= end))

    repo._scan_daily_symbol = _scan  # type: ignore[method-assign]
    return repo, calls


# ── repository.get_daily ────────────────────────────────


def test_get_daily_with_ta_columns_attaches_values() -> None:
    _require_ta()
    raw = _raw_frame()
    dates = raw["date"].to_list()
    start, end = dates[40], dates[-1]
    repo, _ = _bare_repo(raw)

    df = repo.get_daily(SYM, start, end, ta_columns={"ta_adx_14"})
    assert "ta_adx_14" in df.columns
    assert df.height > 0
    assert df["date"].min() >= start and df["date"].max() <= end
    assert df["ta_adx_14"].null_count() < df.height, "预热后应有非空 ADX"


def test_get_daily_without_ta_columns_has_no_ta_column() -> None:
    raw = _raw_frame()
    dates = raw["date"].to_list()
    repo, _ = _bare_repo(raw)

    df = repo.get_daily(SYM, dates[40], dates[-1])
    assert "ta_adx_14" not in df.columns


def test_get_daily_ta_columns_reuses_history_cache_with_warmup() -> None:
    _require_ta()
    raw = _raw_frame()
    dates = raw["date"].to_list()
    repo, calls = _bare_repo(raw)
    hist = repo._compute_enriched_range(raw)
    repo._enriched_history_cache = hist
    repo._enriched_history_start = hist["date"].min()

    df = repo.get_daily(SYM, dates[40], dates[-1], ta_columns={"ta_adx_14"})
    assert calls["scan"] == 0, "缓存命中时不应回退扫描路径"
    assert "ta_adx_14" in df.columns
    assert df["ta_adx_14"].null_count() < df.height


# ── /api/kline/daily ────────────────────────────────────


class _FakeRepo:
    def __init__(self) -> None:
        self.ta_params: list[set[str] | None] = []

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_instruments(self) -> pl.DataFrame:
        return pl.DataFrame()

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None, ta_columns=None, indicator_specs=None):
        self.ta_params.append(ta_columns)
        df = pl.DataFrame({
            "symbol": [symbol], "date": [date(2026, 9, 2)],
            "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
            "volume": [1.0e5],
        })
        for c in ta_columns or set():
            df = df.with_columns(pl.lit(30.0).alias(c))
        return df


def _client() -> tuple[TestClient, _FakeRepo]:
    repo = _FakeRepo()
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    return TestClient(app), repo


def test_daily_endpoint_passes_known_ta_columns_and_returns_them() -> None:
    _require_ta()
    client, repo = _client()

    resp = client.get("/api/kline/daily", params={
        "symbol": SYM, "ta_columns": "ta_adx_14,ta_not_real",
    })
    assert resp.status_code == 200
    # 仅透传已注册 id; 未知名被静默忽略
    assert repo.ta_params[-1] == {"ta_adx_14"}
    assert resp.json()["rows"][0]["ta_adx_14"] == 30.0


def test_daily_endpoint_without_ta_columns_passes_none() -> None:
    client, repo = _client()

    resp = client.get("/api/kline/daily", params={"symbol": SYM})
    assert resp.status_code == 200
    assert repo.ta_params[-1] is None
    assert "ta_adx_14" not in resp.json()["rows"][0]
