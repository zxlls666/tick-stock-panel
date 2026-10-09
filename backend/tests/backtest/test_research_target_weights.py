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
    amounts: list[list[float]] | None = None,
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
                "amount": (
                    amounts[time_id][asset_id]
                    if amounts is not None
                    else price * volumes[time_id][asset_id]
                ),
                "signal_limit_up": (time_id, asset_id) in limit_up,
                "signal_limit_down": (time_id, asset_id) in limit_down,
            })
    return build_market_data_matrix(pl.DataFrame(rows), field_columns={"amount"})


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


def test_queue_proxy_scenarios_are_monotonic_and_base_preserves_current_behavior():
    one_price = _market([[10.0]], limit_up={(0, 0)})
    target = np.array([[1.0]])
    fills = {}
    for queue_proxy in ("optimistic", "base", "conservative"):
        result = simulate_target_weights(
            one_price,
            target,
            config=_config(),
            queue_proxy=queue_proxy,
        )
        fills[queue_proxy] = result.daily["buy_notional"][0]

    assert fills["conservative"] <= fills["base"] <= fills["optimistic"]
    assert fills == {
        "optimistic": pytest.approx(100_000.0),
        "base": 0.0,
        "conservative": 0.0,
    }

    close_locked = build_market_data_matrix(pl.DataFrame([{
        "symbol": "000001.SZ",
        "date": date(2024, 1, 2),
        "open": 10.0,
        "high": 11.0,
        "low": 10.0,
        "close": 11.0,
        "volume": 100_000.0,
        "signal_limit_up": True,
    }]))
    base = simulate_target_weights(close_locked, target, config=_config(), queue_proxy="base")
    conservative = simulate_target_weights(
        close_locked,
        target,
        config=_config(),
        queue_proxy="conservative",
    )
    assert base.daily["positions"][0] == 1
    assert conservative.daily["positions"][0] == 0


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


@pytest.mark.parametrize(
    ("recovery_rate", "expected_equity"),
    [(1.0, 100_000.0), (0.7, 70_000.0), (0.0, 0.0)],
)
def test_delisted_position_is_terminated_once_in_each_recovery_scenario(
    recovery_rate,
    expected_equity,
):
    market = build_market_data_matrix(pl.DataFrame([
        {"symbol": "000001.SZ", "date": date(2024, 1, 2), "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100_000.0},
        {"symbol": "000001.SZ", "date": date(2024, 1, 3), "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100_000.0},
    ]))
    target = np.array([[1.0], [1.0]])
    delistings = np.array([[False], [True]])

    result = simulate_target_weights(
        market,
        target,
        config=_config(),
        delistings=delistings,
        delisting_recovery_rate=recovery_rate,
    )

    assert result.daily["positions"].to_list() == [1, 0]
    assert result.daily["delisted_positions"].to_list() == [0, 1]
    assert result.daily["equity"][-1] == pytest.approx(expected_equity)
    assert result.execution["delisted_position_count"] == 1


def test_delisting_inputs_fail_closed():
    market = _market([[10.0], [10.0]])
    target = np.array([[1.0], [1.0]])

    with pytest.raises(ValueError, match="delistings shape"):
        simulate_target_weights(
            market,
            target,
            config=_config(),
            delistings=np.array([[True]]),
        )
    with pytest.raises(ValueError, match="recovery"):
        simulate_target_weights(
            market,
            target,
            config=_config(),
            delistings=np.zeros((2, 1), dtype=bool),
            delisting_recovery_rate=1.1,
        )


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


def test_adv_capacity_uses_only_amount_available_before_the_open():
    prices = [[10.0]] * 21
    amounts = [[100_000.0]] * 20 + [[1_000_000_000.0]]
    market = _market(prices, amounts=amounts)
    target = np.zeros((21, 1), dtype=np.float64)
    target[-1, 0] = 1.0

    result = simulate_target_weights(
        market,
        target,
        config=_config(),
        max_adv_participation=0.1,
    )

    last = result.daily.row(-1, named=True)
    assert last["buy_notional"] == pytest.approx(10_000.0)
    assert last["capacity_unfilled_buy_notional"] == pytest.approx(90_000.0)
    assert last["realized_exposure"] == pytest.approx(0.1)


def test_capacity_fails_closed_without_twenty_prior_amount_observations():
    market = _market([[10.0]] * 20, amounts=[[100_000.0]] * 20)
    target = np.zeros((20, 1), dtype=np.float64)
    target[-1, 0] = 1.0

    result = simulate_target_weights(
        market,
        target,
        config=_config(),
        max_adv_participation=0.1,
    )

    last = result.daily.row(-1, named=True)
    assert last["buy_notional"] == 0.0
    assert last["capacity_unfilled_buy_notional"] == pytest.approx(100_000.0)


def test_impact_trade_fails_closed_when_lagged_volatility_is_unavailable():
    market = _market([[10.0]] * 21, amounts=[[100_000.0]] * 21)
    target = np.zeros((21, 1), dtype=np.float64)
    target[-1, 0] = 1.0

    result = simulate_target_weights(
        market,
        target,
        config=_config(),
        max_adv_participation=0.1,
        impact_eta=0.5,
    )

    last = result.daily.row(-1, named=True)
    assert last["buy_notional"] == 0.0
    assert last["capacity_unfilled_buy_notional"] == pytest.approx(100_000.0)


def test_square_root_impact_is_zero_at_eta_zero_and_rises_with_participation():
    prices = [[10.0 + (day % 2)] for day in range(23)]
    market = _market(prices, amounts=[[1_000_000.0]] * 23)
    small = np.zeros((23, 1), dtype=np.float64)
    large = np.zeros((23, 1), dtype=np.float64)
    small[-1, 0] = 0.1
    large[-1, 0] = 0.8

    zero = simulate_target_weights(
        market,
        large,
        config=_config(),
        max_adv_participation=1.0,
        impact_eta=0.0,
    )
    small_impact = simulate_target_weights(
        market,
        small,
        config=_config(),
        max_adv_participation=1.0,
        impact_eta=0.5,
    )
    large_impact = simulate_target_weights(
        market,
        large,
        config=_config(),
        max_adv_participation=1.0,
        impact_eta=0.5,
    )

    assert zero.daily["impact_cost"][-1] == 0.0
    assert small_impact.daily["impact_cost"][-1] > 0.0
    assert (
        large_impact.daily["impact_cost"][-1] / large_impact.daily["buy_notional"][-1]
        > small_impact.daily["impact_cost"][-1] / small_impact.daily["buy_notional"][-1]
    )
    assert large_impact.daily["equity"][-1] < zero.daily["equity"][-1]


def test_invalid_capacity_and_impact_inputs_fail_closed():
    market = _market([[10.0]] * 3)
    target = np.zeros((3, 1), dtype=np.float64)

    with pytest.raises(ValueError, match="participation"):
        simulate_target_weights(
            market,
            target,
            config=_config(),
            max_adv_participation=0.0,
        )
    with pytest.raises(ValueError, match="impact_eta"):
        simulate_target_weights(
            market,
            target,
            config=_config(),
            impact_eta=-0.1,
        )
    with pytest.raises(ValueError, match="queue_proxy"):
        simulate_target_weights(
            market,
            target,
            config=_config(),
            queue_proxy="random",
        )


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


def test_trades_record_one_price_block_reason():
    market = _market([[10.0]], limit_up={(0, 0)})
    target = np.array([[1.0]])

    result = simulate_target_weights(market, target, config=_config())

    row = result.trades.filter(pl.col("date") == "2024-01-02").to_dicts()[0]
    assert row["side"] == "buy"
    assert row["reason"] == "queue_block"
    assert row["filled_shares"] == 0.0
    assert result.daily["blocked_buy_count"][0] == 1
    assert result.execution["skipped_buy_count"] == 1


def test_trades_surface_silent_invalid_open_skip():
    """无效开盘价的标的既不成交、也不计入 blocked_buy_count —— 轨迹必须留痕。"""
    nan = float("nan")
    market = _market([[10.0, nan], [10.0, 10.0]])
    target = np.array([[0.5, 0.5], [0.5, 0.5]])

    result = simulate_target_weights(market, target, config=_config())

    broken = result.trades.filter(
        (pl.col("date") == "2024-01-02") & (pl.col("symbol") == "000002.SZ")
    ).to_dicts()[0]
    assert broken["reason"] == "invalid_open"
    assert broken["filled_shares"] == 0.0
    # 已知盲点: 未成交但 blocked_buy_count 为 0 (旧口径无法发现), 由轨迹与 skip 计数补上
    assert result.daily["blocked_buy_count"][0] == 0
    assert result.execution["skipped_buy_count"] == 1
    # 次日恢复可交易后正常成交
    assert result.trades.filter(
        (pl.col("date") == "2024-01-03") & (pl.col("symbol") == "000002.SZ")
    )["reason"][0] == "filled"
