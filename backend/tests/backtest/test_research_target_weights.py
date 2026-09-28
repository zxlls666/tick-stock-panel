"""研究目标权重回测器。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import MatcherConfig
from app.backtest.matrix import build_market_data_matrix
from app.backtest.research_target_weights import simulate_target_weights


def _market(
    prices: list[list[float]],
    *,
    volumes: list[list[float]] | None = None,
    limit_up: set[tuple[int, int]] | None = None,
    limit_down: set[tuple[int, int]] | None = None,
):
    volumes = volumes or [[100_000.0] * len(prices[0]) for _ in prices]
    limit_up = limit_up or set()
    limit_down = limit_down or set()
    start = date(2024, 1, 2)
    rows = []
    for time_id, row in enumerate(prices):
        for asset_id, price in enumerate(row):
            rows.append({
                "symbol": f"{asset_id + 1:06d}.SZ",
                "name": f"stock-{asset_id + 1}",
                "date": start + timedelta(days=time_id),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volumes[time_id][asset_id],
                "signal_limit_up": (time_id, asset_id) in limit_up,
                "signal_limit_down": (time_id, asset_id) in limit_down,
            })
    return build_market_data_matrix(pl.DataFrame(rows))


def _config(**overrides) -> MatcherConfig:
    values = {
        "initial_capital": 100_000.0,
        "commission_pct": 0.0,
        "stamp_tax_pct": 0.0,
        "slippage_bps": 0.0,
    }
    values.update(overrides)
    return MatcherConfig(**values)


def test_constant_price_zero_cost_preserves_equity():
    market = _market([[10.0, 20.0]] * 3)
    target = np.array([
        [0.5, 0.0],
        [0.5, 0.0],
        [0.0, 0.0],
    ])

    result = simulate_target_weights(market, target, config=_config())

    assert result.daily["equity"].to_list() == pytest.approx([100_000.0] * 3)
    assert result.daily["cash"].to_list() == pytest.approx([50_000.0, 50_000.0, 100_000.0])
    assert result.daily["positions"].to_list() == [1, 1, 0]
    assert result.stats["total_return"] == pytest.approx(0.0)


def test_costs_are_charged_on_actual_buy_and_sell_notional():
    market = _market([[10.0], [10.0]])
    target = np.array([[0.5], [0.0]])

    result = simulate_target_weights(
        market,
        target,
        config=_config(commission_pct=0.001, stamp_tax_pct=0.002, slippage_bps=0.0),
    )

    first, second = result.daily.iter_rows(named=True)
    assert first["buy_notional"] == pytest.approx(49_000.0)
    assert first["trading_cost"] == pytest.approx(49.0)
    assert second["sell_notional"] == pytest.approx(49_000.0)
    assert second["trading_cost"] == pytest.approx(147.0)
    assert result.execution["trading_cost"] == pytest.approx(196.0)
    assert result.daily["equity"][-1] == pytest.approx(99_804.0)


def test_one_price_limit_blocks_buy_and_retries_if_target_remains():
    market = _market(
        [[10.0], [10.0]],
        limit_up={(0, 0)},
    )
    target = np.array([[1.0], [1.0]])

    result = simulate_target_weights(market, target, config=_config())

    assert result.daily["positions"].to_list() == [0, 1]
    assert result.daily["blocked_buy_count"].to_list() == [1, 0]
    assert result.daily["blocked_buy_notional"][0] == pytest.approx(100_000.0)
    assert result.daily["cash"].to_list() == pytest.approx([100_000.0, 0.0])


def test_one_price_limit_blocks_sell_until_a_tradable_day():
    market = _market(
        [[10.0], [10.0], [10.0]],
        limit_down={(1, 0)},
    )
    target = np.array([[1.0], [0.0], [0.0]])

    result = simulate_target_weights(market, target, config=_config())

    assert result.daily["positions"].to_list() == [1, 1, 0]
    assert result.daily["blocked_sell_count"].to_list() == [0, 1, 0]
    assert result.daily["blocked_sell_notional"][1] == pytest.approx(100_000.0)


def test_suspension_keeps_position_at_last_close_and_blocks_sale():
    market = _market(
        [[10.0], [10.0], [10.0]],
        volumes=[[100_000.0], [0.0], [100_000.0]],
    )
    target = np.array([[1.0], [0.0], [0.0]])

    result = simulate_target_weights(market, target, config=_config())

    assert result.daily["positions"].to_list() == [1, 1, 0]
    assert result.daily["stale_positions"].to_list() == [0, 1, 0]
    assert result.daily["blocked_sell_count"].to_list() == [0, 1, 0]
    assert result.daily["equity"].to_list() == pytest.approx([100_000.0] * 3)


def test_buying_scales_proportionally_when_costs_make_targets_unaffordable():
    market = _market([[10.0, 10.0]])
    target = np.array([[0.5, 0.5]])

    result = simulate_target_weights(
        market,
        target,
        config=_config(commission_pct=0.01),
    )

    assert result.daily["positions"][0] == 2
    assert result.daily["cash"][0] >= 0.0
    assert result.daily["realized_exposure"][0] == pytest.approx(0.98, abs=0.02)
    assert result.daily["tracking_error_l1"][0] < 0.03


def test_sparse_market_rows_do_not_poison_cash_or_equity_with_nan():
    market = build_market_data_matrix(pl.DataFrame([
        {"symbol": "000001.SZ", "date": date(2024, 1, 2), "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100_000.0},
        {"symbol": "000002.SZ", "date": date(2024, 1, 2), "open": 20.0, "high": 20.0, "low": 20.0, "close": 20.0, "volume": 100_000.0},
        {"symbol": "000001.SZ", "date": date(2024, 1, 3), "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 100_000.0},
    ]))
    target = np.array([[0.5, 0.5], [1.0, 0.0]])

    result = simulate_target_weights(market, target, config=_config())

    assert result.daily["equity"].is_finite().all()
    assert result.daily["cash"].is_finite().all()


def test_daily_nav_statistics_use_the_account_curve():
    market = _market([[10.0], [11.0], [9.9]])
    target = np.array([[1.0], [1.0], [1.0]])

    result = simulate_target_weights(market, target, config=_config())

    assert result.daily["daily_return"].to_list() == pytest.approx([0.0, 0.1, -0.1])
    assert result.stats["total_return"] == pytest.approx(-0.01, abs=1e-6)
    assert result.stats["max_drawdown"] == pytest.approx(-0.1, abs=1e-6)
    assert result.stats["annual_volatility"] > 0.0
    assert result.yearly_returns["year"].to_list() == [2024]
    assert result.yearly_returns["return"][0] == pytest.approx(-0.01, abs=1e-6)


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (np.array([[1.0, 0.0]]), "shape"),
        (np.array([[np.nan]] * 3), "finite"),
        (np.array([[-0.1]] * 3), "non-negative"),
        (np.array([[1.01]] * 3), "100%"),
    ],
)
def test_invalid_target_weights_fail_closed(target, message):
    market = _market([[10.0]] * 3)

    with pytest.raises(ValueError, match=message):
        simulate_target_weights(market, target, config=_config())
