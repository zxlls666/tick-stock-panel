"""研究专用目标权重回测器。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import polars as pl

from app.backtest.engine import MatcherConfig
from app.backtest.matrix import (
    MarketDataMatrix,
    valid_rolling_mean,
    valid_rolling_std,
    valid_shift,
)


@dataclass(frozen=True)
class ResearchPortfolioResult:
    daily: pl.DataFrame
    yearly_returns: pl.DataFrame
    stats: dict[str, object]
    execution: dict[str, int | float]
    # 逐名执行轨迹: 每个"有意图"的标的每天一行 (含被静默跳过的), reason 见 _trade_reason
    trades: pl.DataFrame


def simulate_target_weights(
    market: MarketDataMatrix,
    target_weights: np.ndarray,
    *,
    config: MatcherConfig,
    lot_size: int = 100,
    delistings: np.ndarray | None = None,
    delisting_recovery_rate: float = 1.0,
    max_adv_participation: float | None = None,
    impact_eta: float = 0.0,
    queue_proxy: Literal["optimistic", "base", "conservative"] = "base",
) -> ResearchPortfolioResult:
    """Rebalance to target weights at each open and mark the account at close."""
    target = np.asarray(target_weights, dtype=np.float64)
    delisting_mask = (
        np.zeros(market.shape, dtype=bool)
        if delistings is None
        else np.asarray(delistings, dtype=bool)
    )
    _validate_inputs(
        market,
        target,
        lot_size,
        config.initial_capital,
        delisting_mask,
        delisting_recovery_rate,
        max_adv_participation,
        impact_eta,
        queue_proxy,
    )
    execution_model_enabled = max_adv_participation is not None or impact_eta > 0
    lagged_adv, lagged_volatility = _execution_inputs(
        market,
        enabled=execution_model_enabled,
    )

    cash = float(config.initial_capital)
    shares = np.zeros(market.shape[1], dtype=np.float64)
    last_price = np.full(market.shape[1], np.nan, dtype=np.float64)
    buy_cost_pct = float(config.buy_cost_pct())
    sell_cost_pct = float(config.sell_cost_pct())
    total_cost = 0.0
    execution = {
        "blocked_buy_count": 0,
        "blocked_sell_count": 0,
        "skipped_buy_count": 0,
        "skipped_sell_count": 0,
        "delisted_position_count": 0,
        "trading_cost": 0.0,
    }
    if execution_model_enabled:
        execution.update({
            "capacity_unfilled_buy_notional": 0.0,
            "capacity_unfilled_sell_notional": 0.0,
            "impact_cost": 0.0,
        })
    rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []
    previous_equity = float(config.initial_capital)

    for time_id, label in enumerate(market.timestamp_labels):
        open_prices = np.asarray(market.open[time_id], dtype=np.float64)
        valid_open = np.isfinite(open_prices) & (open_prices > 0)
        valuation = np.where(valid_open, open_prices, last_price)
        delisted = delisting_mask[time_id] & (shares > 0)
        delisted_positions = int(np.count_nonzero(delisted))
        delisted_notional = float(np.dot(
            shares[delisted],
            last_price[delisted] * delisting_recovery_rate,
        ))
        cash += delisted_notional
        shares[delisted] = 0.0
        last_price[delisted] = np.nan
        execution["delisted_position_count"] += delisted_positions
        pre_trade_equity = cash + _position_value(shares, valuation)
        effective_target = np.where(delisting_mask[time_id], 0.0, target[time_id])
        target_notional = pre_trade_equity * effective_target
        desired = _target_shares(target_notional, open_prices, buy_cost_pct, lot_size)
        delta = desired - shares

        tradable = np.asarray(market.tradable[time_id], dtype=bool)
        one_price = _one_price_mask(market, time_id)
        limit_up = np.asarray(market.limit_up_locked[time_id], dtype=bool)
        limit_down = np.asarray(market.limit_down_locked[time_id], dtype=bool)
        queue_block_buy, queue_block_sell = _queue_blocks(
            queue_proxy,
            one_price,
            limit_up,
            limit_down,
        )
        blocked_buy = (delta > 0) & (~tradable | queue_block_buy)
        blocked_sell = (delta < 0) & (~tradable | queue_block_sell)
        buyable = (delta > 0) & ~blocked_buy & valid_open
        sellable = (delta < 0) & ~blocked_sell & valid_open

        blocked_buy_count = int(np.count_nonzero(blocked_buy))
        blocked_sell_count = int(np.count_nonzero(blocked_sell))
        blocked_buy_notional = float(target_notional[blocked_buy].sum())
        blocked_sell_notional = float((-delta[blocked_sell] * open_prices[blocked_sell]).sum())
        execution["blocked_buy_count"] += blocked_buy_count
        execution["blocked_sell_count"] += blocked_sell_count

        adv = lagged_adv[time_id]
        volatility = lagged_volatility[time_id]
        if impact_eta > 0:
            adv = np.where(np.isfinite(volatility) & (volatility >= 0), adv, np.nan)
        sell_requested = np.where(sellable, np.minimum(-delta, shares), 0.0)
        sell_quantity = _capacity_limited_quantity(
            sell_requested,
            open_prices,
            adv,
            max_adv_participation,
            lot_size,
        )
        capacity_unfilled_sell = float(np.dot(
            (sell_requested - sell_quantity)[sellable],
            open_prices[sellable],
        ))
        sell_notional = float(np.dot(sell_quantity[sellable], open_prices[sellable]))
        sell_impact = _impact_cost(
            sell_quantity,
            open_prices,
            adv,
            volatility,
            impact_eta,
        )
        sell_cost = sell_notional * sell_cost_pct + sell_impact
        cash += sell_notional - sell_cost
        shares -= sell_quantity

        buy_requested = np.where(buyable, delta, 0.0)
        requested_buy = _capacity_limited_quantity(
            buy_requested,
            open_prices,
            adv,
            max_adv_participation,
            lot_size,
        )
        capacity_unfilled_buy = float(np.dot(
            (buy_requested - requested_buy)[buyable],
            open_prices[buyable],
        ))
        buy_impact = _impact_cost(
            requested_buy,
            open_prices,
            adv,
            volatility,
            impact_eta,
        )
        buy_notional = float(np.dot(requested_buy[buyable], open_prices[buyable]))
        requested_cash = buy_notional * (1.0 + buy_cost_pct) + buy_impact
        if requested_cash > cash and requested_cash > 0:
            scale = cash / requested_cash
            requested_buy = np.floor(requested_buy * scale / lot_size) * lot_size
            buy_impact = _impact_cost(
                requested_buy,
                open_prices,
                adv,
                volatility,
                impact_eta,
            )
            buy_notional = float(np.dot(requested_buy[buyable], open_prices[buyable]))
        buy_cost = buy_notional * buy_cost_pct + buy_impact
        cash -= buy_notional + buy_cost
        shares += requested_buy
        if -1e-7 < cash < 0:
            cash = 0.0
        if cash < -1e-7:
            raise ValueError("target-weight execution produced negative cash")

        impact_cost = buy_impact + sell_impact
        trading_cost = buy_cost + sell_cost
        total_cost += trading_cost
        if execution_model_enabled:
            execution["capacity_unfilled_buy_notional"] += capacity_unfilled_buy
            execution["capacity_unfilled_sell_notional"] += capacity_unfilled_sell
            execution["impact_cost"] += impact_cost
        close_prices = np.asarray(market.close[time_id], dtype=np.float64)
        valid_close = np.isfinite(close_prices) & (close_prices > 0) & tradable
        last_price[valid_close] = close_prices[valid_close]
        close_valuation = np.where(valid_close, close_prices, last_price)
        stale_positions = int(np.count_nonzero((shares > 0) & ~valid_close & np.isfinite(last_price)))
        market_value = _position_value(shares, close_valuation)
        equity = cash + market_value
        daily_return = equity / previous_equity - 1.0 if previous_equity > 0 else 0.0
        previous_equity = equity
        actual_weights = np.zeros(market.shape[1], dtype=np.float64)
        valid_value = (shares > 0) & np.isfinite(close_valuation) & (close_valuation > 0)
        if equity > 0:
            actual_weights[valid_value] = shares[valid_value] * close_valuation[valid_value] / equity
        turnover = 0.5 * (buy_notional + sell_notional) / pre_trade_equity if pre_trade_equity > 0 else 0.0
        participation = np.divide(
            (requested_buy + sell_quantity) * open_prices,
            adv,
            out=np.zeros(market.shape[1], dtype=np.float64),
            where=np.isfinite(adv) & (adv > 0),
        )
        requested_participation = np.divide(
            (buy_requested + sell_requested) * open_prices,
            adv,
            out=np.zeros(market.shape[1], dtype=np.float64),
            where=np.isfinite(adv) & (adv > 0),
        )
        positive_participation = participation[participation > 0]
        liquidation_days = _liquidation_days(
            sell_requested,
            open_prices,
            adv,
            max_adv_participation,
        )
        # 逐名执行轨迹: buy 意图按 effective_target 判定 (即使无法形成请求也留痕),
        # 使"未成交且未计入 blocked"的静默跳过可被复核; 已在目标仓位的无请求不留痕。
        for asset_id in np.flatnonzero((effective_target > 0) | (delta < 0)):
            is_buy = bool(effective_target[asset_id] > 0)
            desired_shares = float(np.nan_to_num(desired[asset_id], nan=0.0))
            requested = float(np.nan_to_num(
                delta[asset_id] if is_buy else -delta[asset_id], nan=0.0
            ))
            filled = float(np.nan_to_num(
                requested_buy[asset_id] if is_buy else sell_quantity[asset_id], nan=0.0
            ))
            open_ok = bool(valid_open[asset_id])
            # 已达到目标仓位 (无请求且无数据问题) 不算一次执行
            if is_buy and requested <= 0 and open_ok and desired_shares > 0:
                continue
            reason = _trade_reason(
                open_ok=open_ok,
                desired=desired_shares if is_buy else requested,
                tradable=bool(tradable[asset_id]),
                queue_blocked=bool(queue_block_buy[asset_id] if is_buy else queue_block_sell[asset_id]),
                requested=requested,
                filled=filled,
            )
            if filled <= 0:
                execution["skipped_buy_count" if is_buy else "skipped_sell_count"] += 1
            trade_rows.append({
                "date": label[:10],
                "symbol": market.symbols[asset_id],
                "side": "buy" if is_buy else "sell",
                "target_weight": float(effective_target[asset_id]),
                "requested_shares": requested,
                "filled_shares": filled,
                "reason": reason,
            })

        row = {
            "date": label[:10],
            "equity": equity,
            "daily_return": daily_return,
            "cash": cash,
            "cash_weight": cash / equity if equity > 0 else 0.0,
            "target_exposure": float(effective_target.sum()),
            "realized_exposure": market_value / equity if equity > 0 else 0.0,
            "tracking_error_l1": float(np.abs(actual_weights - effective_target).sum()),
            "buy_notional": buy_notional,
            "sell_notional": sell_notional,
            "one_way_turnover": turnover,
            "trading_cost": trading_cost,
            "positions": int(np.count_nonzero(shares)),
            "blocked_buy_count": blocked_buy_count,
            "blocked_sell_count": blocked_sell_count,
            "blocked_buy_notional": blocked_buy_notional,
            "blocked_sell_notional": blocked_sell_notional,
            "stale_positions": stale_positions,
            "delisted_positions": delisted_positions,
            "delisted_recovery_notional": delisted_notional,
        }
        if execution_model_enabled:
            row.update({
                "impact_cost": impact_cost,
                "capacity_unfilled_buy_notional": capacity_unfilled_buy,
                "capacity_unfilled_sell_notional": capacity_unfilled_sell,
                "max_adv_participation": float(participation.max(initial=0.0)),
                "p95_adv_participation": (
                    float(np.quantile(positive_participation, 0.95))
                    if positive_participation.size else 0.0
                ),
                "max_requested_adv_participation": float(
                    requested_participation.max(initial=0.0)
                ),
                "max_liquidation_days": liquidation_days,
            })
        rows.append(row)

    execution["trading_cost"] = total_cost
    daily = _with_drawdown(pl.DataFrame(rows))
    yearly = _yearly_returns(daily)
    stats = _portfolio_stats(daily, total_cost, config, shares, last_price)
    trades = pl.DataFrame(trade_rows, schema=_TRADES_SCHEMA) if trade_rows else _empty_trades()
    return ResearchPortfolioResult(
        daily=daily,
        yearly_returns=yearly,
        stats=stats,
        execution=execution,
        trades=trades,
    )


_TRADES_SCHEMA = {
    "date": pl.String,
    "symbol": pl.String,
    "side": pl.String,
    "target_weight": pl.Float64,
    "requested_shares": pl.Float64,
    "filled_shares": pl.Float64,
    "reason": pl.String,
}


def _empty_trades() -> pl.DataFrame:
    return pl.DataFrame(schema=_TRADES_SCHEMA)


def _trade_reason(
    *,
    open_ok: bool,
    desired: float,
    tradable: bool,
    queue_blocked: bool,
    requested: float,
    filled: float,
) -> str:
    """成交/跳过原因 (从数据缺失到撮合约束, 最后才是成交)。"""
    if not open_ok:
        return "invalid_open"
    if desired <= 0:
        return "zero_desired"
    if not tradable:
        return "untradable"
    if queue_blocked:
        return "queue_block"
    if filled <= 0:
        return "zero_fill"
    if filled + 1e-9 < requested:
        return "partial"
    return "filled"


def _validate_inputs(
    market: MarketDataMatrix,
    target: np.ndarray,
    lot_size: int,
    initial_capital: float,
    delistings: np.ndarray,
    delisting_recovery_rate: float,
    max_adv_participation: float | None,
    impact_eta: float,
    queue_proxy: str,
) -> None:
    if target.shape != market.shape:
        raise ValueError("target_weights shape must match market")
    if not np.isfinite(target).all():
        raise ValueError("target_weights must be finite")
    if (target < 0).any():
        raise ValueError("target_weights must be non-negative")
    if (target.sum(axis=1) > 1.0 + 1e-9).any():
        raise ValueError("target_weights cannot exceed 100% exposure")
    if lot_size < 1:
        raise ValueError("lot_size must be positive")
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    if delistings.shape != market.shape:
        raise ValueError("delistings shape must match market")
    if not 0.0 <= delisting_recovery_rate <= 1.0:
        raise ValueError("delisting recovery rate must be between 0 and 1")
    if max_adv_participation is not None and not 0.0 < max_adv_participation <= 1.0:
        raise ValueError("max ADV participation must be in (0, 1]")
    if not np.isfinite(impact_eta) or impact_eta < 0:
        raise ValueError("impact_eta must be finite and non-negative")
    if queue_proxy not in {"optimistic", "base", "conservative"}:
        raise ValueError("queue_proxy must be optimistic, base, or conservative")


def _queue_blocks(
    queue_proxy: str,
    one_price: np.ndarray,
    limit_up: np.ndarray,
    limit_down: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    if queue_proxy == "optimistic":
        return np.zeros(one_price.shape, dtype=bool), np.zeros(one_price.shape, dtype=bool)
    if queue_proxy == "conservative":
        return limit_up, limit_down
    return one_price & limit_up, one_price & limit_down


def _execution_inputs(
    market: MarketDataMatrix,
    *,
    enabled: bool,
) -> tuple[np.ndarray, np.ndarray]:
    empty = np.full(market.shape, np.nan, dtype=np.float64)
    if not enabled:
        return empty, empty
    amount = np.asarray(market.field("amount"), dtype=np.float64)
    valid_amount = np.isfinite(amount) & (amount > 0)
    adv20 = valid_rolling_mean(amount, valid_amount, 20)
    lagged_adv = valid_shift(adv20, 1, np.isfinite(adv20)).astype(np.float64)

    close = np.asarray(market.close, dtype=np.float64)
    valid_close = np.isfinite(close) & (close > 0)
    previous = valid_shift(close, 1, valid_close)
    returns = np.full(market.shape, np.nan, dtype=np.float64)
    np.divide(close, previous, out=returns, where=valid_close & np.isfinite(previous) & (previous > 0))
    returns -= 1.0
    vol20 = valid_rolling_std(returns, np.isfinite(returns), 20)
    lagged_volatility = valid_shift(vol20, 1, np.isfinite(vol20)).astype(np.float64)
    return lagged_adv, lagged_volatility


def _capacity_limited_quantity(
    requested: np.ndarray,
    prices: np.ndarray,
    adv: np.ndarray,
    max_participation: float | None,
    lot_size: int,
) -> np.ndarray:
    if max_participation is None:
        return np.array(requested, dtype=np.float64, copy=True)
    capacity = np.zeros(requested.shape, dtype=np.float64)
    valid = np.isfinite(adv) & (adv > 0) & np.isfinite(prices) & (prices > 0)
    capacity[valid] = np.floor(
        adv[valid] * max_participation / prices[valid] / lot_size
    ) * lot_size
    return np.minimum(requested, capacity)


def _liquidation_days(
    requested: np.ndarray,
    prices: np.ndarray,
    adv: np.ndarray,
    max_participation: float | None,
) -> int:
    if max_participation is None:
        return 0
    capacity = adv * max_participation
    notional = requested * prices
    valid = (notional > 0) & np.isfinite(capacity) & (capacity > 0)
    if np.any((notional > 0) & ~valid):
        return 2**31 - 1
    return int(np.ceil(notional[valid] / capacity[valid]).max(initial=0.0))


def _impact_cost(
    quantity: np.ndarray,
    prices: np.ndarray,
    adv: np.ndarray,
    volatility: np.ndarray,
    eta: float,
) -> float:
    if eta == 0:
        return 0.0
    notional = quantity * prices
    valid = (
        (quantity > 0)
        & np.isfinite(notional)
        & np.isfinite(adv)
        & (adv > 0)
        & np.isfinite(volatility)
        & (volatility >= 0)
    )
    if np.any((quantity > 0) & ~valid):
        raise ValueError("impact inputs unavailable for requested trade")
    impact_pct = np.zeros(quantity.shape, dtype=np.float64)
    impact_pct[valid] = eta * volatility[valid] * np.sqrt(notional[valid] / adv[valid])
    return float(np.dot(notional[valid], impact_pct[valid]))


def _target_shares(
    notionals: np.ndarray,
    prices: np.ndarray,
    buy_cost_pct: float,
    lot_size: int,
) -> np.ndarray:
    valid = np.isfinite(prices) & (prices > 0)
    result = np.zeros(notionals.shape, dtype=np.float64)
    result[valid] = np.floor(
        notionals[valid] / (prices[valid] * (1.0 + buy_cost_pct)) / lot_size
    ) * lot_size
    return result


def _one_price_mask(market: MarketDataMatrix, time_id: int) -> np.ndarray:
    prices = np.stack((
        market.open[time_id],
        market.high[time_id],
        market.low[time_id],
        market.close[time_id],
    ))
    valid = np.all(np.isfinite(prices) & (prices > 0), axis=0)
    tolerance = np.maximum(np.abs(prices[3]) * 1e-4, 0.01)
    return valid & ((np.max(prices, axis=0) - np.min(prices, axis=0)) <= tolerance)


def _position_value(shares: np.ndarray, prices: np.ndarray) -> float:
    valid = (shares > 0) & np.isfinite(prices) & (prices > 0)
    return float(np.dot(shares[valid], prices[valid]))


def _with_drawdown(daily: pl.DataFrame) -> pl.DataFrame:
    equity = daily["equity"].to_numpy()
    peak = np.maximum.accumulate(equity)
    drawdown = np.divide(equity, peak, out=np.ones_like(equity), where=peak > 0) - 1.0
    return daily.with_columns(pl.Series("drawdown", drawdown))


def _yearly_returns(daily: pl.DataFrame) -> pl.DataFrame:
    by_year: dict[int, list[float]] = {}
    for row in daily.select("date", "daily_return").iter_rows(named=True):
        by_year.setdefault(int(str(row["date"])[:4]), []).append(float(row["daily_return"]))
    return pl.DataFrame({
        "year": list(by_year),
        "return": [float(np.prod(1.0 + np.asarray(values)) - 1.0) for values in by_year.values()],
    }, schema={"year": pl.Int32, "return": pl.Float64})


def _portfolio_stats(
    daily: pl.DataFrame,
    total_cost: float,
    config: MatcherConfig,
    shares: np.ndarray,
    last_price: np.ndarray,
) -> dict[str, object]:
    equity = daily["equity"].to_numpy()
    returns = daily["daily_return"].to_numpy()
    total_return = float(equity[-1] / config.initial_capital - 1.0)
    periods = max(len(returns), 1)
    annual_return = (1.0 + total_return) ** (252.0 / periods) - 1.0 if total_return > -1 else total_return
    volatility = float(np.std(returns) * np.sqrt(252.0))
    sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(252.0)) if np.std(returns) > 0 else 0.0
    downside = np.minimum(returns, 0.0)
    downside_dev = float(np.sqrt(np.mean(downside ** 2)))
    sortino = float(np.mean(returns) / downside_dev * np.sqrt(252.0)) if downside_dev > 0 else None
    max_drawdown = float(daily["drawdown"].min())
    calmar = annual_return / abs(max_drawdown) if abs(max_drawdown) > 1e-12 else 0.0
    monthly_returns: dict[str, list[float]] = {}
    for row in daily.select("date", "daily_return").iter_rows(named=True):
        monthly_returns.setdefault(str(row["date"])[:7], []).append(float(row["daily_return"]))
    month_values = [float(np.prod(1.0 + np.asarray(values)) - 1.0) for values in monthly_returns.values()]
    annualized_turnover = float(daily["one_way_turnover"].mean() * 252.0)
    liquidation_cost = _position_value(shares, last_price) * config.sell_cost_pct()
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "monthly_win_rate": float(np.mean(np.asarray(month_values) > 0)) if month_values else 0.0,
        "annualized_turnover": annualized_turnover,
        "trading_cost": total_cost,
        "average_positions": float(daily["positions"].mean()),
        "average_cash_weight": float(daily["cash_weight"].mean()),
        "estimated_liquidation_cost": liquidation_cost,
    }
