"""V2 预注册统计契约。不读取全市场行情，不改 V1 阈值。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.stats_v2 import newey_west_t
from app.services.flow_radar import RADAR_SPECS, signal_document
from app.services.flow_radar_v2 import (
    NW_LAG,
    V2_SPECS,
    aggregate_signal_days,
    long_gate,
    risk_excess_by_signal_day,
    risk_gate,
    signal_day_newey_west,
)
from app.strategy import custom_signals

FORBIDDEN = ("主力确认", "机构建仓", "量化操盘")
NW_SERIES = [
    0.01, 0.008, 0.004, -0.002, -0.006, -0.004, 0.001, 0.007,
    0.009, 0.003, -0.001, -0.005, -0.003, 0.002, 0.006, 0.008,
    0.002, -0.004, -0.007, 0.0, 0.005, 0.004, -0.002, 0.001,
]


def _spec(spec_id: str) -> dict:
    return next(item for item in V2_SPECS if item["id"] == spec_id)


def _v1(spec_id: str) -> dict:
    return next(item for item in RADAR_SPECS if item["id"] == spec_id)


def test_v2_formulas_are_frozen_extensions_of_v1():
    assert [item["id"] for item in V2_SPECS] == [
        "squeeze_expand_strong_close_v2",
        "high_turnover_weak_close_risk_v2",
    ]
    assert {item["id"] for item in V2_SPECS}.isdisjoint({item["id"] for item in RADAR_SPECS})
    long_spec = _spec("squeeze_expand_strong_close_v2")
    risk_spec = _spec("high_turnover_weak_close_risk_v2")
    squeeze = _v1("squeeze_expand_proxy")
    high_turn = _v1("high_turnover_risk")
    assert long_spec["direction"] == "long"
    assert risk_spec["direction"] == "risk"
    assert long_spec["conditions"][:5] == squeeze["conditions"]
    assert long_spec["conditions"][5:] == [
        {"left": "close_position", "op": ">=", "right": "0.75", "leftDays": 0, "rightDays": 0},
        {"left": "change_pct", "op": ">=", "right": "0.01", "leftDays": 0, "rightDays": 0},
    ]
    assert risk_spec["conditions"] == high_turn["conditions"]
    for spec in V2_SPECS:
        blob = str(spec)
        for word in FORBIDDEN:
            assert word not in blob
        custom_signals.validate(signal_document(spec))


def test_same_day_names_collapse_to_one_signal_day():
    day = date(2021, 6, 1)
    returns = [0.01 * ((index % 5) - 2) for index in range(100)]
    rows = [
        {"date": day, "net_ret": value, "entry_state": "tradable"}
        for value in returns
    ]
    rows.append({"date": day, "net_ret": 0.5, "entry_state": "limit_up_locked"})
    rows.append({"date": day, "net_ret": None, "entry_state": "tradable"})
    rows.append({"date": date(2021, 6, 2), "net_ret": -0.02, "entry_state": "tradable"})
    out = aggregate_signal_days(pl.DataFrame(rows))
    assert out.height == 2
    first = out.filter(pl.col("date") == day)
    assert first["n_names"][0] == 100
    assert first["portfolio_ret"][0] == pytest.approx(sum(returns) / 100)
    assert out.filter(pl.col("date") == date(2021, 6, 2))["portfolio_ret"][0] == pytest.approx(-0.02)


def test_newey_west_lag_is_fixed_at_four():
    assert NW_LAG == 4
    assert signal_day_newey_west(NW_SERIES) == newey_west_t(NW_SERIES, 4)
    assert signal_day_newey_west(NW_SERIES) != newey_west_t(NW_SERIES, 1)
    assert signal_day_newey_west([0.01] * 6) is None


def test_risk_excess_matches_same_day_size_bucket_and_ignores_industry():
    day = date(2022, 3, 1)
    rows = [
        {"date": day, "symbol": "A", "size_bucket": "小市值", "industry": "银行", "triggered": True, "entry_state": "tradable", "net_ret": -0.02},
        {"date": day, "symbol": "F", "size_bucket": "小市值", "industry": "医药", "triggered": True, "entry_state": "tradable", "net_ret": -0.04},
        {"date": day, "symbol": "B", "size_bucket": "小市值", "industry": "医药", "triggered": False, "entry_state": "tradable", "net_ret": 0.01},
        {"date": day, "symbol": "C", "size_bucket": "大市值", "industry": "银行", "triggered": False, "entry_state": "tradable", "net_ret": 0.50},
        {"date": day, "symbol": "E", "size_bucket": "小市值", "industry": "银行", "triggered": False, "entry_state": "suspended", "net_ret": 0.80},
        {"date": day, "symbol": "G", "size_bucket": "未知", "industry": "银行", "triggered": True, "entry_state": "tradable", "net_ret": -0.20},
    ]
    frame = pl.DataFrame(rows)
    out = risk_excess_by_signal_day(frame)
    assert out.height == 1
    assert out["n_names"][0] == 2
    assert out["excess"][0] == pytest.approx(-0.04)
    assert "industry" not in out.columns
    shuffled = frame.with_columns(pl.col("industry").reverse())
    again = risk_excess_by_signal_day(shuffled)
    assert again["excess"][0] == pytest.approx(-0.04)
    assert again["n_names"][0] == 2


def test_risk_day_without_peers_is_not_an_available_signal_day():
    day = date(2022, 3, 2)
    frame = pl.DataFrame([
        {"date": day, "symbol": "A", "size_bucket": "大市值", "industry": "银行", "triggered": True, "entry_state": "tradable", "net_ret": -0.02},
        {"date": day, "symbol": "B", "size_bucket": "小市值", "industry": "银行", "triggered": False, "entry_state": "tradable", "net_ret": 0.01},
    ])
    assert risk_excess_by_signal_day(frame).is_empty()


def _daily(values_by_year: dict[int, list[float]], column: str) -> pl.DataFrame:
    rows = []
    for year, values in values_by_year.items():
        start = date(year, 1, 4)
        for offset, value in enumerate(values):
            rows.append({"date": start + timedelta(days=offset), column: value})
    return pl.DataFrame(rows)


def _strong_positive(days: int) -> list[float]:
    return [0.02] * (days - 4) + [-0.005] * 4


def test_long_gate_passes_only_when_every_rule_holds():
    passed = long_gate(_daily({
        2021: _strong_positive(34),
        2022: _strong_positive(33),
        2023: _strong_positive(33),
    }, "portfolio_ret"))
    assert passed["passed"] is True
    assert passed["failures"] == []
    assert passed["observed"]["signal_days"] == 100
    assert passed["observed"]["newey_west_lag"] == 4

    short = long_gate(_daily({
        2021: _strong_positive(33),
        2022: _strong_positive(33),
        2023: _strong_positive(33),
    }, "portfolio_ret"))
    assert short["passed"] is False
    assert "signal_days" in short["failures"]

    flat = long_gate(_daily({
        2021: [0.02] * 40,
        2022: [0.02] * 40,
        2023: [0.02] * 40,
    }, "portfolio_ret"))
    assert flat["passed"] is False
    assert flat["failures"] == ["newey_west"]


def test_long_gate_rejects_right_tail_median_and_coin_flip_win_rate():
    right_tail = long_gate(_daily({
        2021: [-0.01] * 24 + [0.08] * 16,
        2022: [-0.01] * 24 + [0.08] * 16,
        2023: [-0.01] * 24 + [0.08] * 16,
    }, "portfolio_ret"))
    assert right_tail["passed"] is False
    assert "median" in right_tail["failures"]
    assert right_tail["observed"]["mean"] > 0

    split = []
    for _ in range(20):
        split.append(-0.001)
    split.extend([0.02] * 20)
    coin = long_gate(_daily({2021: split, 2022: split, 2023: split}, "portfolio_ret"))
    assert coin["passed"] is False
    assert coin["failures"] == ["win_rate"]
    assert coin["observed"]["median"] > 0


def test_long_gate_rejects_t_boundary_and_weak_year_coverage(monkeypatch):
    frame = _daily({
        2021: _strong_positive(40),
        2022: _strong_positive(40),
        2023: _strong_positive(40),
    }, "portfolio_ret")
    monkeypatch.setattr(
        "app.services.flow_radar_v2.signal_day_newey_west",
        lambda _values: (1.96, 0.01, 0.005),
    )
    edged = long_gate(frame)
    assert edged["passed"] is False
    assert "newey_west" in edged["failures"]

    two_years = long_gate(_daily({
        2021: _strong_positive(50),
        2022: _strong_positive(50),
    }, "portfolio_ret"))
    assert two_years["passed"] is False
    assert "years" in two_years["failures"]

    weak_year = [-0.02] * 5 + [0.001] * 35
    sign = long_gate(_daily({
        2021: [0.02] * 40,
        2022: weak_year,
        2023: weak_year,
    }, "portfolio_ret"))
    assert sign["passed"] is False
    assert "year_sign" in sign["failures"]
    assert sign["observed"]["median"] > 0


def test_risk_gate_passes_negative_excess_and_rejects_the_opposite_boundaries(monkeypatch):
    def negative(days: int) -> list[float]:
        return [-0.02] * (days - 4) + [0.005] * 4

    passed = risk_gate(_daily({
        2021: negative(34),
        2022: negative(33),
        2023: negative(33),
    }, "excess"))
    assert passed["passed"] is True
    assert passed["failures"] == []
    assert passed["observed"]["signal_days"] == 100

    positive = risk_gate(_daily({
        2021: _strong_positive(40),
        2022: _strong_positive(40),
        2023: _strong_positive(40),
    }, "excess"))
    assert positive["passed"] is False
    assert "mean" in positive["failures"]
    assert "median" in positive["failures"]

    frame = _daily({
        2021: negative(40),
        2022: negative(40),
        2023: negative(40),
    }, "excess")
    monkeypatch.setattr(
        "app.services.flow_radar_v2.signal_day_newey_west",
        lambda _values: (-1.96, -0.01, 0.005),
    )
    edged = risk_gate(frame)
    assert edged["passed"] is False
    assert "newey_west" in edged["failures"]

    split = [-0.02] * 20 + [0.001] * 20
    coin = risk_gate(_daily({2021: split, 2022: split, 2023: split}, "excess"))
    assert coin["passed"] is False
    assert "negative_rate" in coin["failures"]
    assert coin["observed"]["median"] < 0


def test_summarize_v2_uses_signal_days_and_keeps_draft_status():
    from app.services.flow_radar_v2 import summarize_v2

    day = date(2021, 6, 1)

    def row(**kwargs):
        base = {
            "date": day,
            "symbol": "X",
            "_bar": 300,
            "entry_state": "tradable",
            "path_ok_5": True,
            "exit_blocked_5": False,
            "regime": "震荡",
            "size_bucket": "小市值",
            "trade_ret_5": 0.0,
            "csg_squeeze_expand_strong_close_v2": False,
            "csg_high_turnover_weak_close_risk_v2": False,
        }
        base.update(kwargs)
        return base

    frame = pl.DataFrame([
        row(symbol="L1", csg_squeeze_expand_strong_close_v2=True, trade_ret_5=0.01),
        row(symbol="L2", csg_squeeze_expand_strong_close_v2=True, trade_ret_5=0.03),
        row(symbol="L3", csg_squeeze_expand_strong_close_v2=True, trade_ret_5=0.02, entry_state="limit_up_locked"),
        row(symbol="A", csg_high_turnover_weak_close_risk_v2=True, trade_ret_5=-0.02),
        row(symbol="F", csg_high_turnover_weak_close_risk_v2=True, trade_ret_5=-0.04),
        row(symbol="B", trade_ret_5=0.01),
        row(symbol="C", size_bucket="大市值", trade_ret_5=0.50),
        row(symbol="E", entry_state="suspended", trade_ret_5=0.80),
        row(symbol="G", size_bucket="未知", csg_high_turnover_weak_close_risk_v2=True, trade_ret_5=-0.20),
    ])
    long, risk = summarize_v2(
        frame,
        cost=0.0,
        study_start=date(2021, 1, 4),
        as_of=day,
        names={"A": "甲"},
    )
    assert long["status"] == "draft"
    assert long["status_label"] == "研究中"
    assert long["events"] == 3
    assert long["signal_days"] == 1
    assert long["names_per_day"]["max"] == 2
    assert "signal_days" in long["gate"]["failures"]
    assert risk["events"] == 3
    assert risk["signal_days"] == 1
    assert risk["names_per_day"]["max"] == 2
    assert risk["gate"]["observed"]["mean"] == pytest.approx(-0.04)
    assert risk["gate"]["observed"]["newey_west_lag"] == 4
    assert {item["symbol"] for item in risk["candidates"]} == {"A", "F", "G"}
    assert next(item for item in risk["candidates"] if item["symbol"] == "A")["name"] == "甲"


def test_ensure_v2_signals_adds_two_files_and_keeps_edits(tmp_path):
    from app.services.flow_radar import ensure_radar_signals
    from app.services.flow_radar_v2 import ensure_v2_signals

    ensure_radar_signals(tmp_path)
    written = ensure_v2_signals(tmp_path)
    folder = tmp_path / "user_data" / "custom_signals"
    names = sorted(path.stem for path in folder.glob("*.json"))
    assert len(names) == 8
    assert [item["id"] for item in written] == [
        "squeeze_expand_strong_close_v2",
        "high_turnover_weak_close_risk_v2",
    ]
    path = folder / "squeeze_expand_strong_close_v2.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["name"] = "人工改名"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    again = ensure_v2_signals(tmp_path)
    assert again[0]["name"] == "人工改名"


def test_historical_overlay_uses_saved_status_only_when_spec_hash_matches(tmp_path):
    from app.services.flow_radar_v2 import historical_status_fields, overlay_historical_status, spec_hash

    rejected = historical_status_fields(False)
    candidate = historical_status_fields(True)
    assert rejected["status"] == "historical_rejected"
    assert candidate["status"] == "historical_candidate"
    rows = [{
        "id": "squeeze_expand_strong_close_v2",
        "status": "draft",
        "status_label": "研究中",
        "status_note": "尚未落盘",
    }]
    path = tmp_path / "historical.json"
    path.write_text(json.dumps({
        "spec_hash": spec_hash(),
        "signals": [{
            "id": "squeeze_expand_strong_close_v2",
            **rejected,
        }],
    }, ensure_ascii=False), encoding="utf-8")
    overlaid = overlay_historical_status(rows, path)
    assert overlaid[0]["status"] == "historical_rejected"
    assert overlaid[0]["status_label"] == "历史未通过"

    path.write_text(json.dumps({
        "spec_hash": "其他规格",
        "signals": [{"id": "squeeze_expand_strong_close_v2", **candidate}],
    }, ensure_ascii=False), encoding="utf-8")
    kept = overlay_historical_status(rows, path)
    assert kept[0]["status"] == "draft"
    assert kept[0]["status_label"] == "研究中"
