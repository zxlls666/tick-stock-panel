"""图表指标注册表 (registry.py) 契约测试。

关键: 描述符的 ``col_template`` 必须与 ``indicators.custom`` 的别名列命名**逐字一致**
(ADR-005 alias 契约 + ADR-006 注册表契约), 由交叉校验锁定。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.indicators import router
from app.factors import ta_factors
from app.indicators.custom import compute_custom_indicators, parse_indicator_specs
from app.indicators.registry import ChartIndicator, all_indicators, catalog, render_template


def _bars(n: int = 40) -> pl.DataFrame:
    rows = []
    for i in range(n):
        close = 10.0 + i * 0.05
        rows.append({
            "symbol": "600000",
            "date": date(2024, 1, 1) + timedelta(days=i),
            "open": close - 0.02, "high": close + 0.1, "low": close - 0.1, "close": close,
            "volume": 1000.0 + i, "amount": 1.0e6,
        })
    return pl.DataFrame(rows).sort(["symbol", "date"])


# ── 目录结构 ─────────────────────────────────────────────


def test_builtin_catalog_ids_and_panes() -> None:
    by_id = {i.id: i for i in all_indicators()}
    assert {"ma", "ema", "boll", "macd", "rsi", "kdj", "vol"} <= set(by_id)
    assert by_id["ma"].pane == "overlay"
    assert by_id["boll"].pane == "overlay"
    assert by_id["macd"].pane == "panel"
    assert by_id["rsi"].y_axis == {"min": 0, "max": 100}
    assert by_id["vol"].special == "volume"


def test_ta_entries_follow_availability(monkeypatch) -> None:
    monkeypatch.setattr(ta_factors, "available", lambda: False)
    assert not any(i.source == "ta" for i in all_indicators())

    monkeypatch.setattr(ta_factors, "available", lambda: True)
    ta_ids = {i.id for i in all_indicators() if i.source == "ta"}
    assert {"ta_adx", "ta_stoch", "ta_aroon", "ta_supertrend"} <= ta_ids


def test_catalog_marks_availability(monkeypatch) -> None:
    monkeypatch.setattr(ta_factors, "available", lambda: False)
    items = catalog()
    assert items and all(i["available"] is True for i in items)
    assert not any(i["source"] == "ta" for i in items)


# ── 与 custom.py 的别名列命名交叉校验 (ADR-005/006) ──────────


SAMPLE_SPECS = "ma:5:13,ema:7,rsi:7,macd:8:17:9,boll:26:3,kdj:14"
SAMPLE_VALUES: dict[str, dict] = {
    "ma": {"p": [5, 13]},
    "ema": {"p": [7]},
    "rsi": {"p": [7]},
    "macd": {"fast": 8, "slow": 17, "signal": 9},
    "boll": {"period": 26, "std": 3},
    "kdj": {"n": 14},
}


def _expected_alias_cols(ind: ChartIndicator, values: dict) -> list[str]:
    """按描述符模板展开出别名列名 (模拟前端渲染器的展开逻辑)。"""
    cols: list[str] = []
    for out in ind.outputs:
        if not out.col_template:
            continue
        if out.expand_param:
            for v in values[out.expand_param]:
                cols.append(render_template(out.col_template, {**values, out.expand_param: v}))
        else:
            cols.append(render_template(out.col_template, values))
    return cols


def test_col_templates_match_custom_alias_naming() -> None:
    df = compute_custom_indicators(_bars(), parse_indicator_specs(SAMPLE_SPECS))
    actual = {c for c in df.columns if c.endswith("_c")}

    by_id = {i.id: i for i in all_indicators()}
    expected: set[str] = set()
    for kind, values in SAMPLE_VALUES.items():
        expected.update(_expected_alias_cols(by_id[kind], values))

    assert expected == actual, f"模板 {expected} != custom 产出 {actual}"


def test_render_template() -> None:
    assert render_template("ma_{p}_c", {"p": 13}) == "ma_13_c"
    assert render_template("macd_{fast}_{slow}_{signal}_dif_c", {"fast": 8, "slow": 17, "signal": 9}) == "macd_8_17_9_dif_c"


# ── 端点 ────────────────────────────────────────────────


def test_catalog_endpoint() -> None:
    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).get("/api/indicators/catalog")
    assert resp.status_code == 200
    data = resp.json()["indicators"]
    ma = next(i for i in data if i["id"] == "ma")
    assert ma["pane"] == "overlay"
    assert ma["params"][0]["default"] == [5, 10, 20, 60]
    assert ma["outputs"][0]["col_template"] == "ma_{p}_c"
