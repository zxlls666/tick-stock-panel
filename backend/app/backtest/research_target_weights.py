"""研究专用目标权重回测器。"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from app.backtest.engine import MatcherConfig
from app.backtest.matrix import MarketDataMatrix


@dataclass(frozen=True)
class ResearchPortfolioResult:
    daily: pl.DataFrame
    yearly_returns: pl.DataFrame
    stats: dict[str, object]
    execution: dict[str, int | float]


def simulate_target_weights(
    market: MarketDataMatrix,
    target_weights: np.ndarray,
    *,
    config: MatcherConfig,
    lot_size: int = 100,
) -> ResearchPortfolioResult:
    """Rebalance to target weights at each open and mark the account at close."""
    target = np.asarray(target_weights, dtype=np.float64)
    _validate_inputs(market, target, lot_size, config.initial_capital)

    cash = float(config.initial_capital)
    shares = np.zeros(market.shape[1], dtype=np.float64)
    last_price = np.full(market.shape[1], np.nan, dtype=np.float64)
    buy_cost_pct = float(config.buy_cost_pct())
    sell_cost_pct = float(config.sell_cost_pct())
    total_cost = 0.0
    execution = {
        "blocked_buy_count": 0,
        "blocked_sell_count": 0,
        "trading_cost": 0.0,
    }
    rows: list[dict[str, object]] = []
    previous_equity = float(config.initial_capital)

    for time_id, label in enumerate(market.timestamp_labels):
        open_prices = np.asarray(market.open[time_id], dtype=np.float64)
        valid_open = np.isfinite(open_prices) & (open_prices > 0)
        valuation = np.where(valid_open, open_prices, last_price)
        pre_trade_equity = cash + _position_value(shares, valuation)
        target_notional = pre_trade_equity * target[time_id]
        desired = _target_shares(target_notional, open_prices, buy_cost_pct, lot_size)
        delta = desired - shares

        tradable = np.asarray(market.tradable[time_id], dtype=bool)
        one_price = _one_price_mask(market, time_id)
        blocked_buy = (delta > 0) & (~tradable | (one_price & np.asarray(market.limit_up_locked[time_id], dtype=bool)))
        blocked_sell = (delta < 0) & (~tradable | (one_price & np.asarray(market.limit_down_locked[time_id], dtype=bool)))
        buyable = (delta > 0) & ~blocked_buy & valid_open
        sellable = (delta < 0) & ~blocked_sell & valid_open

        blocked_buy_count = int(np.count_nonzero(blocked_buy))
        blocked_sell_count = int(np.count_nonzero(blocked_sell))
        blocked_buy_notional = float(target_notional[blocked_buy].sum())
        blocked_sell_notional = float((-delta[blocked_sell] * open_prices[blocked_sell]).sum())
        execution["blocked_buy_count"] += blocked_buy_count
        execution["blocked_sell_count"] += blocked_sell_count

        sell_quantity = np.where(sellable, np.minimum(-delta, shares), 0.0)
        sell_notional = float(np.dot(sell_quantity[sellable], open_prices[sellable]))
        sell_cost = sell_notional * sell_cost_pct
        cash += sell_notional - sell_cost
        shares -= sell_quantity

        requested_buy = np.where(buyable, delta, 0.0)
        requested_cash = float(np.dot(
            requested_buy[buyable],
            open_prices[buyable] * (1.0 + buy_cost_pct),
        ))
        if requested_cash > cash and requested_cash > 0:
            scale = cash / requested_cash
            requested_buy = np.floor(requested_buy * scale / lot_size) * lot_size
        buy_notional = float(np.dot(requested_buy[buyable], open_prices[buyable]))
        buy_cost = buy_notional * buy_cost_pct
        cash -= buy_notional + buy_cost
        shares += requested_buy
        if -1e-7 < cash < 0:
            cash = 0.0
        if cash < -1e-7:
            raise ValueError("target-weight execution produced negative cash")

        trading_cost = buy_cost + sell_cost
        total_cost += trading_cost
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
        rows.append({
            "date": label[:10],
            "equity": equity,
            "daily_return": daily_return,
            "cash": cash,
            "cash_weight": cash / equity if equity > 0 else 0.0,
            "target_exposure": float(target[time_id].sum()),
            "realized_exposure": market_value / equity if equity > 0 else 0.0,
            "tracking_error_l1": float(np.abs(actual_weights - target[time_id]).sum()),
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
        })

    execution["trading_cost"] = total_cost
    daily = _with_drawdown(pl.DataFrame(rows))
    yearly = _yearly_returns(daily)
    stats = _portfolio_stats(daily, total_cost, config, shares, last_price)
    return ResearchPortfolioResult(
        daily=daily,
        yearly_returns=yearly,
        stats=stats,
        execution=execution,
    )


def _validate_inputs(
    market: MarketDataMatrix,
    target: np.ndarray,
    lot_size: int,
    initial_capital: float,
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
