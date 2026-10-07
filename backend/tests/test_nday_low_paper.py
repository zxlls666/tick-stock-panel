"""n_day_low_reversal paper 观察器的纯函数测试 (Phase 10 口径)。

只测可复现的纯逻辑: 观察目录不可覆盖、regime T-1 归因、逐月落脚、滚动回撤、
预注册停止条件。端到端影子回测由真实数据手工跑, 不在单测里重跑全市场。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from scripts.run_nday_low_paper import (
    STOP_RULES,
    assess_stop_conditions,
    monthly_settlement,
    observation_path,
    regime_attribution,
    regime_point_at_prev,
    trailing_max_drawdown,
)


def test_observation_path_refuses_existing(tmp_path) -> None:
    path = observation_path(tmp_path, date(2026, 10, 31))
    assert path.name == "as_of=2026-10-31"
    path.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        observation_path(tmp_path, date(2026, 10, 31))


def test_regime_point_at_prev_is_strictly_before() -> None:
    rows = [
        (date(2026, 10, 1), "strong", 80.0),
        (date(2026, 10, 2), "weak", 20.0),
    ]
    assert regime_point_at_prev(rows, date(2026, 10, 2)) == ("strong", 80.0)
    assert regime_point_at_prev(rows, date(2026, 10, 3)) == ("weak", 20.0)
    assert regime_point_at_prev(rows, date(2026, 10, 1)) is None


def test_trailing_max_drawdown() -> None:
    assert trailing_max_drawdown([100, 120, 90, 110], 126) == pytest.approx(-0.25)
    assert trailing_max_drawdown([100], 126) == 0.0
    # 只看最近 window 个点
    assert trailing_max_drawdown([100, 50, 100, 90], 2) == pytest.approx(-0.10)


def test_monthly_settlement_equity_and_trades() -> None:
    equity = [
        {"date": "2026-10-01", "value": 100.0},
        {"date": "2026-10-31", "value": 110.0},
        {"date": "2026-11-30", "value": 99.0},
    ]
    trades = [
        {"exit_date": "2026-10-15", "pnl_amount": 5.0},
        {"exit_date": "2026-10-20", "pnl_amount": -2.0},
        {"exit_date": "2026-11-10", "pnl_amount": 7.0},
    ]
    monthly = monthly_settlement(equity, trades)
    assert monthly["month"].to_list() == ["2026-10", "2026-11"]
    assert monthly["equity_end"].to_list() == [110.0, 99.0]
    assert monthly["monthly_return"].to_list()[0] is None
    assert monthly["monthly_return"].to_list()[1] == pytest.approx(-0.10)
    assert monthly["closed_trades"].to_list() == [2, 1]
    assert monthly["realized_pnl"].to_list() == pytest.approx([3.0, 7.0])
    assert monthly["win_rate"].to_list()[0] == pytest.approx(0.5)


def test_regime_attribution_groups_by_entry_t_minus_one() -> None:
    regime = [
        (date(2026, 10, 1), "strong", 80.0),
        (date(2026, 10, 2), "weak", 20.0),
    ]
    trades = [
        {"entry_signal_date": "2026-10-02", "pnl_amount": 10.0, "pnl_pct": 0.01},   # prev=strong
        {"entry_signal_date": "2026-10-03", "pnl_amount": -4.0, "pnl_pct": -0.01},  # prev=weak
        {"entry_signal_date": "2026-10-03", "pnl_amount": 6.0, "pnl_pct": 0.02},    # prev=weak
    ]
    table = regime_attribution(trades, regime)
    by_state = {r["regime_state"]: r for r in table.iter_rows(named=True)}
    assert by_state["strong"]["trades"] == 1
    assert by_state["strong"]["win_rate"] == pytest.approx(1.0)
    assert by_state["weak"]["trades"] == 2
    assert by_state["weak"]["win_rate"] == pytest.approx(0.5)
    assert by_state["weak"]["sum_pnl_amount"] == pytest.approx(2.0)


def _monthly(returns: list[float]) -> pl.DataFrame:
    return pl.DataFrame({"monthly_return": returns})


def test_stop_conditions_continue_when_healthy() -> None:
    outcome = assess_stop_conditions(
        equity_curve=[{"value": 100.0}, {"value": 101.0}, {"value": 102.0}],
        trades=[{"pnl_amount": 5.0}, {"pnl_amount": 3.0}],
        monthly=_monthly([0.01, 0.01]),
    )
    assert outcome["status"] == "continue"
    assert outcome["triggered"] == []


def test_stop_conditions_consecutive_negative_months() -> None:
    outcome = assess_stop_conditions(
        equity_curve=[{"value": 100.0}, {"value": 99.0}],
        trades=[],
        monthly=_monthly([0.01, -0.02, -0.03]),
    )
    assert outcome["status"] == "stop"
    assert any("连续" in reason for reason in outcome["triggered"])


def test_stop_conditions_cum_return_floor() -> None:
    outcome = assess_stop_conditions(
        equity_curve=[{"value": 100.0}, {"value": 70.0}],
        trades=[],
        monthly=_monthly([0.01]),
    )
    assert outcome["status"] == "stop"
    assert any("累计收益" in reason for reason in outcome["triggered"])


def test_stop_conditions_win_rate_floor_needs_min_trades() -> None:
    losing = [{"pnl_amount": -1.0} for _ in range(STOP_RULES["min_closed_trades_for_win_rate"])]
    triggered = assess_stop_conditions(
        equity_curve=[{"value": 100.0}, {"value": 99.0}],
        trades=losing,
        monthly=_monthly([0.0]),
    )
    assert any("胜率" in reason for reason in triggered["triggered"])

    # 少于最小样本 → 不因胜率触发
    few = assess_stop_conditions(
        equity_curve=[{"value": 100.0}, {"value": 99.0}],
        trades=losing[:5],
        monthly=_monthly([0.0]),
    )
    assert not any("胜率" in reason for reason in few["triggered"])


def test_stop_conditions_trailing_drawdown_floor() -> None:
    equity = [{"value": 100.0}, {"value": 50.0}]
    outcome = assess_stop_conditions(equity_curve=equity, trades=[], monthly=_monthly([0.0]))
    assert any("滚动回撤" in reason for reason in outcome["triggered"])
