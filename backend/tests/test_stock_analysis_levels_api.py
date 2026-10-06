"""个股分析 /levels 与 /reports 的 methodology_version 契约测试。

覆盖: GET /levels 正常与空数据分支都下发版本; POST /reports 保留所传版本,
缺省时后端兜底为当前版本; 落库后可读回 (向后兼容: 旧报告无该字段不报错)。
"""
from __future__ import annotations

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.stock_analysis import router
from app.indicators.levels import LEVELS_METHODOLOGY_VERSION


def _levels_frame(n: int = 60) -> pl.DataFrame:
    rows = []
    for i in range(n):
        c = 10.0 + (i % 7) * 0.3
        rows.append({
            "symbol": "600000.SH", "date": f"2026-01-{i + 1:02d}",
            "open": c, "high": c + 0.5, "low": c - 0.5, "close": c,
            "volume": 1000.0, "amount": 1e6,
        })
    return pl.DataFrame(rows)


class _FakeRepo:
    def __init__(self, empty: bool = False) -> None:
        self._empty = empty

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type: str, symbol: str, start, end) -> pl.DataFrame:
        return pl.DataFrame() if self._empty else _levels_frame()


def _client(empty: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = _FakeRepo(empty)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    from app import config as app_config

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)


def test_levels_normal_branch_has_methodology_version() -> None:
    resp = _client().get("/api/stock-analysis/levels", params={"symbol": "600000.SH"})
    assert resp.status_code == 200
    assert resp.json()["methodology_version"] == LEVELS_METHODOLOGY_VERSION


def test_levels_empty_branch_has_methodology_version() -> None:
    resp = _client(empty=True).get("/api/stock-analysis/levels", params={"symbol": "600000.SH"})
    assert resp.status_code == 200
    assert resp.json()["methodology_version"] == LEVELS_METHODOLOGY_VERSION


def test_report_save_persists_and_defaults_methodology_version() -> None:
    client = _client()

    explicit = client.post("/api/stock-analysis/reports", json={
        "symbol": "600000.SH", "content": "x", "methodology_version": "levels_v9",
    })
    assert explicit.status_code == 200
    assert explicit.json()["report"]["methodology_version"] == "levels_v9"

    defaulted = client.post("/api/stock-analysis/reports", json={
        "symbol": "600001.SH", "content": "y",
    })
    assert defaulted.json()["report"]["methodology_version"] == LEVELS_METHODOLOGY_VERSION

    listed = client.get("/api/stock-analysis/reports").json()["reports"]
    by_symbol = {r["symbol"]: r["methodology_version"] for r in listed}
    assert by_symbol["600000.SH"] == "levels_v9"
    assert by_symbol["600001.SH"] == LEVELS_METHODOLOGY_VERSION
