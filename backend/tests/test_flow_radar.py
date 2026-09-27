"""盘后雷达：规格、表达式和事件统计。不读取全市场行情。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import polars as pl
import pytest

from app.services.flow_radar import (
    HORIZONS,
    RADAR_SPECS,
    ROUND_TRIP_COST,
    attach_event_columns,
    ensure_radar_signals,
    research_gate,
    signal_document,
    summarize_triggers,
)
from app.strategy import custom_signals

FORBIDDEN = ("主力确认", "机构建仓", "量化操盘")


def test_radar_specs_validate_and_stay_proxy_language():
    assert len(RADAR_SPECS) == 6
    ids = [spec["id"] for spec in RADAR_SPECS]
    assert len(ids) == len(set(ids))
    for spec in RADAR_SPECS:
        blob = json.dumps(spec, ensure_ascii=False)
        for word in FORBIDDEN:
            assert word not in blob
        assert "代理" in spec["name"] or "风险" in spec["name"]
        assert 1 <= len(spec["conditions"]) <= 8
        custom_signals.validate(signal_document(spec))


def test_ensure_radar_signals_writes_missing_files(tmp_path):
    written = ensure_radar_signals(tmp_path)
    assert [item["id"] for item in written] == [spec["id"] for spec in RADAR_SPECS]
    again = ensure_radar_signals(tmp_path)
    assert [item["id"] for item in again] == [spec["id"] for spec in RADAR_SPECS]
    path = tmp_path / "user_data" / "custom_signals" / "abnormal_turnover.json"
    path.write_text(json.dumps({
        **signal_document(RADAR_SPECS[0]),
        "name": "人工改名",
    }, ensure_ascii=False), encoding="utf-8")
    kept = ensure_radar_signals(tmp_path)
    assert kept[0]["name"] == "人工改名"


def test_null_turnover_does_not_trigger():
    spec = next(item for item in RADAR_SPECS if item["id"] == "abnormal_turnover")
    frame = pl.DataFrame({
        "symbol": ["A", "A", "A"],
        "date": [date(2021, 1, 4), date(2021, 1, 5), date(2021, 1, 6)],
        "turnover_z_60d": [2.5, None, 1.5],
    })
    exprs = custom_signals.build_expressions([signal_document(spec)])
    out = custom_signals.inject(frame, exprs)
    flags = out["csg_abnormal_turnover"].to_list()
    assert flags[0] is True
    assert flags[1] is None
    assert flags[2] is False


def _session(day: date, **overrides) -> dict:
    row = {
        "date": day,
        "open": 10.0,
        "high": 10.2,
        "low": 9.8,
        "close": 10.0,
        "volume": 100.0,
        "signal_limit_up": False,
        "signal_limit_down": False,
    }
    row.update(overrides)
    return row


def test_event_path_uses_next_open_and_blocks_limit_up():
    start = date(2021, 1, 4)
    days = [start + timedelta(days=i) for i in range(6)]
    closes = [10.0, 10.0, 11.0, 12.0, 13.0, 14.0]
    opens = [10.0, 10.5, 11.0, 12.0, 13.0, 14.0]
    highs = [10.2, 11.0, 12.0, 13.0, 14.0, 15.0]
    lows = [9.8, 10.0, 10.5, 11.0, 12.0, 13.0]
    rows = []
    for index, day in enumerate(days):
        rows.append({
            "symbol": "A",
            **_session(day, open=opens[index], high=highs[index], low=lows[index], close=closes[index]),
        })
    rows.append({"symbol": "B", **_session(days[0])})
    rows.append({"symbol": "B", **_session(days[1], open=10, high=10, low=10, close=10, volume=0)})
    rows.append({"symbol": "C", **_session(days[0])})
    rows.append({
        "symbol": "C",
        **_session(days[1], open=11, high=11, low=11, close=11, volume=100, signal_limit_up=True),
    })
    rows.append({"symbol": "D", **_session(days[0])})
    frame = pl.DataFrame(rows).with_columns([
        pl.col("signal_limit_up").cast(pl.Boolean),
        pl.col("signal_limit_down").cast(pl.Boolean),
    ])
    out = attach_event_columns(frame, HORIZONS)
    states = {
        row["symbol"]: row["entry_state"]
        for row in out.filter(pl.col("date") == days[0]).iter_rows(named=True)
    }
    assert states["A"] == "tradable"
    assert states["B"] == "suspended"
    assert states["C"] == "limit_up_locked"
    assert states["D"] == "no_next_bar"

    first = out.filter((pl.col("symbol") == "A") & (pl.col("date") == days[0]))
    trade_1 = first["trade_ret_1"][0]
    assert trade_1 == pytest.approx(10.0 / 10.5 - 1.0)
    assert first["mfe_1"][0] == pytest.approx(11.0 / 10.5 - 1.0)
    assert first["mae_1"][0] == pytest.approx(10.0 / 10.5 - 1.0)
    assert first["price_ret_1"][0] == pytest.approx(0.0)
    assert first["trade_ret_5"][0] == pytest.approx(14.0 / 10.5 - 1.0)
    assert first["mfe_5"][0] == pytest.approx(15.0 / 10.5 - 1.0)
    assert first["mae_5"][0] == pytest.approx(10.0 / 10.5 - 1.0)
    assert first["path_ok_20"][0] is False

    sample = first.with_columns([
        pl.lit("小市值").alias("size_bucket"),
        pl.lit("震荡").alias("regime"),
    ])
    summary = summarize_triggers(sample, RADAR_SPECS[0])
    assert summary["triggers"] == 1
    assert summary["entry"]["tradable"] == 1
    assert summary["horizons"]["1"]["n"] == 1
    assert summary["horizons"]["1"]["mean_net_ret"] == pytest.approx(trade_1 - ROUND_TRIP_COST, abs=1e-6)
    assert summary["horizons"]["1"]["win_rate_net"] == 0
    assert summary["horizons"]["5"]["win_rate_net"] == 1
    assert summary["gate"]["passed"] is False
    assert summary["gate"]["label"] == "未显示稳定的费后优势"


def test_research_gate_requires_cost_adjusted_stability():
    def horizon(n, mean_net, win):
        return {"n": n, "mean_net_ret": mean_net, "win_rate_net": win}

    weak = {
        "horizons": {"5": horizon(10, 0.02, 0.6)},
        "by_year": [{"n": 30, "mean_net_ret": 0.01}],
    }
    assert research_gate(weak)["passed"] is False

    years = [{"n": 40, "mean_net_ret": 0.01} for _ in range(3)]
    strong = {"horizons": {"5": horizon(120, 0.01, 0.55)}, "by_year": years}
    assert research_gate(strong)["passed"] is True

    mixed = {
        "horizons": {"5": horizon(120, 0.01, 0.55)},
        "by_year": [
            {"n": 40, "mean_net_ret": 0.02},
            {"n": 40, "mean_net_ret": -0.01},
            {"n": 40, "mean_net_ret": -0.02},
        ],
    }
    assert research_gate(mixed)["passed"] is False
