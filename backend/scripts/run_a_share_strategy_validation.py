"""Run research-only A-share target-weight portfolio studies."""
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import (
    MarketDataMatrix,
    matrix_feature,
    slice_market_data_matrix,
    valid_rolling_mean,
)
from app.backtest.research_target_weights import ResearchPortfolioResult, simulate_target_weights
from app.backtest.strategy import ResolvedFeaturePlan
from app.strategy.builtin._research_portfolio import (
    cross_sectional_residual,
    month_end_holding_weights,
    month_end_mask,
    overlapping_sleeve_weights,
    select_low_bucket,
)
from app.tickflow.repository import DataStore, KlineRepository

BIAS_WARNING = (
    "当前股票池缺少退市样本和历史 ST 状态, 部分换手率使用当前流通股本回退。"
    "本结果存在幸存者偏差及非完整 PIT 偏差, 仅用于研究, 不具备发布条件。"
)
STRATEGIES = (
    "reversal_five_sleeves",
    "reversal_monthly",
    "abnormal_turnover_monthly_raw",
    "abnormal_turnover_monthly_size_neutral",
)


@dataclass(frozen=True)
class CostScenario:
    name: str
    commission_pct: float
    stamp_tax_pct: float
    slippage_bps: float


@dataclass(frozen=True)
class ParameterScenario:
    name: str
    top_n: int = 20
    reversal_lookback: int = 20
    turnover_short: int = 20
    turnover_long: int = 250


@dataclass(frozen=True)
class RunSpec:
    strategy: str
    analysis: str
    scenario: str
    period: str
    top_n: int
    reversal_lookback: int
    turnover_short: int
    turnover_long: int
    cost: str


def _cost_scenarios() -> tuple[CostScenario, ...]:
    return (
        CostScenario("gross", 0.0, 0.0, 0.0),
        CostScenario("base", 0.0002, 0.0005, 5.0),
        CostScenario("10bps", 0.0, 0.0, 10.0),
        CostScenario("20bps", 0.0, 0.0, 20.0),
        CostScenario("40bps", 0.0, 0.0, 40.0),
    )


def _parameter_scenarios(strategy: str) -> tuple[ParameterScenario, ...]:
    if strategy.startswith("reversal_"):
        return (
            ParameterScenario("top_n_10", top_n=10),
            ParameterScenario("top_n_30", top_n=30),
            ParameterScenario("lookback_10", reversal_lookback=10),
            ParameterScenario("lookback_30", reversal_lookback=30),
        )
    return (
        ParameterScenario("top_n_10", top_n=10),
        ParameterScenario("top_n_30", top_n=30),
        ParameterScenario("windows_15_200", turnover_short=15, turnover_long=200),
        ParameterScenario("windows_30_300", turnover_short=30, turnover_long=300),
    )


def _date_arg(value: str) -> date:
    return date.fromisoformat(value)


def _feature_plan() -> ResolvedFeaturePlan:
    fields = frozenset({
        "symbol", "date", "open", "high", "low", "close", "volume",
        "raw_close", "raw_high", "turnover_rate",
    })
    return ResolvedFeaturePlan(
        base_columns=fields,
        intermediate_columns=frozenset(),
        indicator_columns=frozenset(),
        signal_columns=frozenset(),
        matrix_columns=fields,
        instrument_columns=frozenset({"name"}),
        warmup_bars=301,
        full_feature_fallback=False,
        execution_backend="matrix_native",
    )


def _abnormal_turnover(
    market: MarketDataMatrix,
    short_window: int,
    long_window: int,
) -> np.ndarray:
    turnover = market.field("turnover_rate")
    valid = np.isfinite(turnover)
    short = valid_rolling_mean(turnover, valid, short_window)
    long = valid_rolling_mean(turnover, valid, long_window)
    result = np.full(market.shape, np.nan, dtype=np.float64)
    denominator_ok = np.isfinite(long) & (long > 0)
    np.divide(short, long, out=result, where=np.isfinite(short) & denominator_ok)
    return result


def _weights(
    market: MarketDataMatrix,
    strategy: str,
    params: ParameterScenario,
) -> np.ndarray:
    if strategy.startswith("reversal_"):
        selected = select_low_bucket(
            matrix_feature(market, f"momentum_{params.reversal_lookback}d"),
            top_n=params.top_n,
        )
        if strategy == "reversal_five_sleeves":
            return overlapping_sleeve_weights(selected, n_sleeves=5, execution_lag=1)
        return month_end_holding_weights(selected, month_end_mask(market.timestamp_labels))

    values = _abnormal_turnover(market, params.turnover_short, params.turnover_long)
    if strategy == "abnormal_turnover_monthly_size_neutral":
        values = cross_sectional_residual(values, matrix_feature(market, "log_float_mv"))
    selected = select_low_bucket(values, top_n=params.top_n)
    return month_end_holding_weights(selected, month_end_mask(market.timestamp_labels))


def _matcher(cost: CostScenario, initial_capital: float) -> MatcherConfig:
    return MatcherConfig(
        initial_capital=initial_capital,
        commission_pct=cost.commission_pct,
        stamp_tax_pct=cost.stamp_tax_pct,
        slippage_bps=cost.slippage_bps,
    )


def _first_invested(weights: np.ndarray) -> int:
    rows = np.flatnonzero(weights.sum(axis=1) > 0)
    if rows.size == 0:
        raise ValueError("target weights never invest")
    return int(rows[0])


def _date_index(labels: tuple[str, ...], boundary: date, *, end: bool = False) -> int:
    text = boundary.isoformat()
    if end:
        matches = [index for index, label in enumerate(labels) if label[:10] <= text]
        if not matches:
            raise ValueError(f"period ends before market data: {boundary}")
        return matches[-1] + 1
    match = next((index for index, label in enumerate(labels) if label[:10] >= text), None)
    if match is None:
        raise ValueError(f"period starts after market data: {boundary}")
    return match


def _run_range(
    market: MarketDataMatrix,
    weights: np.ndarray,
    start_id: int,
    stop_id: int,
    *,
    cost: CostScenario,
    initial_capital: float,
) -> ResearchPortfolioResult:
    if start_id >= stop_id:
        raise ValueError("empty study period")
    result = simulate_target_weights(
        slice_market_data_matrix(market, start_id, stop_id),
        weights[start_id:stop_id],
        config=_matcher(cost, initial_capital),
    )
    return result.__class__(
        daily=result.daily.with_columns((pl.col("equity") / initial_capital).alias("nav")),
        yearly_returns=result.yearly_returns,
        stats=result.stats,
        execution=result.execution,
    )


def _collect(
    spec: RunSpec,
    result: ResearchPortfolioResult,
    summaries: list[dict[str, object]],
    daily_frames: list[pl.DataFrame],
    yearly_frames: list[pl.DataFrame],
) -> None:
    identity = asdict(spec)
    summaries.append({
        **identity,
        "start": str(result.daily["date"][0]),
        "end": str(result.daily["date"][-1]),
        "trading_days": result.daily.height,
        **result.stats,
        **result.execution,
    })
    columns = [pl.lit(value).alias(key) for key, value in identity.items()]
    daily_frames.append(result.daily.with_columns(columns))
    yearly_frames.append(result.yearly_returns.with_columns(columns))


def _subperiods(start: date, end: date) -> tuple[tuple[str, date, date], ...]:
    candidates = (
        ("early", start, min(end, date(2022, 12, 31))),
        ("2023_2024", max(start, date(2023, 1, 1)), min(end, date(2024, 12, 31))),
        ("2025_latest", max(start, date(2025, 1, 1)), end),
    )
    return tuple(item for item in candidates if item[1] <= item[2])


def _run_full_study(
    market: MarketDataMatrix,
    *,
    output: Path,
    load_start: date,
    requested_end: date,
    generation: str | None,
    initial_capital: float,
) -> None:
    base_params = ParameterScenario("baseline")
    base_cost = next(item for item in _cost_scenarios() if item.name == "base")
    summaries: list[dict[str, object]] = []
    daily_frames: list[pl.DataFrame] = []
    yearly_frames: list[pl.DataFrame] = []
    strategy_starts: dict[str, str] = {}

    for strategy in STRATEGIES:
        baseline_weights = _weights(market, strategy, base_params)
        baseline_start = _first_invested(baseline_weights)
        strategy_starts[strategy] = market.timestamp_labels[baseline_start][:10]

        for cost in _cost_scenarios():
            result = _run_range(
                market, baseline_weights, baseline_start, market.shape[0],
                cost=cost, initial_capital=initial_capital,
            )
            _collect(
                RunSpec(strategy, "cost", cost.name, "full", 20, 20, 20, 250, cost.name),
                result, summaries, daily_frames, yearly_frames,
            )

        full_start = date.fromisoformat(strategy_starts[strategy])
        for period, period_start, period_end in _subperiods(full_start, requested_end):
            start_id = _date_index(market.timestamp_labels, period_start)
            stop_id = _date_index(market.timestamp_labels, period_end, end=True)
            result = _run_range(
                market, baseline_weights, start_id, stop_id,
                cost=base_cost, initial_capital=initial_capital,
            )
            _collect(
                RunSpec(strategy, "subperiod", period, period, 20, 20, 20, 250, "base"),
                result, summaries, daily_frames, yearly_frames,
            )

        sensitivity = (base_params, *_parameter_scenarios(strategy))
        sensitivity_weights = [(params, _weights(market, strategy, params)) for params in sensitivity]
        common_start = max(_first_invested(weights) for _, weights in sensitivity_weights)
        for params, weights in sensitivity_weights:
            result = _run_range(
                market, weights, common_start, market.shape[0],
                cost=base_cost, initial_capital=initial_capital,
            )
            _collect(
                RunSpec(
                    strategy, "parameter", params.name, "common_full",
                    params.top_n, params.reversal_lookback,
                    params.turnover_short, params.turnover_long, "base",
                ),
                result, summaries, daily_frames, yearly_frames,
            )

    output.mkdir(parents=True, exist_ok=False)
    summary = pl.DataFrame(summaries)
    daily = pl.concat(daily_frames, how="diagonal_relaxed")
    yearly = pl.concat(yearly_frames, how="diagonal_relaxed")
    summary.write_parquet(output / "summary.parquet")
    daily.write_parquet(output / "daily.parquet")
    yearly.write_parquet(output / "yearly_returns.parquet")
    manifest = {
        "warning": BIAS_WARNING,
        "run_type": "full_history_cost_subperiod_parameter",
        "created_at": datetime.now(UTC).isoformat(),
        "load_start": load_start.isoformat(),
        "data_start": market.timestamp_labels[0][:10],
        "data_end": market.timestamp_labels[-1][:10],
        "strategy_starts": strategy_starts,
        "cost_scenarios": [asdict(item) for item in _cost_scenarios()],
        "subperiod_rule": ["strategy_start..2022-12-31", "2023-01-01..2024-12-31", "2025-01-01..data_end"],
        "parameter_rule": "one-variable-at-a-time; baseline and variants share the latest maturity start",
        "data_generation": generation,
        "market_shape": list(market.shape),
        "market_cache_status": market.cache_status,
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "report.md").write_text(_report(summary, manifest), encoding="utf-8")


def _report(summary: pl.DataFrame, manifest: dict[str, object]) -> str:
    lines = [
        "# A 股研究策略完整历史验证", "", f"> {BIAS_WARNING}", "",
        f"- 数据区间: {manifest['data_start']} 至 {manifest['data_end']}",
        "- base 成本: 双边佣金 2 bps + 卖出印花税 5 bps + 双边滑点 5 bps",
        "- 参数敏感性: 每次只改变一个参数, 同策略所有参数共享最晚成熟起点",
        "- 判断边界: 未加入无偏股票池、历史 ST 和基准比较, internally_stable 不等于可发布",
        "", "## 稳定性判断", "", _verdict_table(summary), "",
    ]
    for analysis in ("cost", "subperiod", "parameter"):
        lines.extend([f"## {analysis}", "", _markdown_table(summary.filter(pl.col("analysis") == analysis)), ""])
    return "\n".join(lines)


def _verdict_table(summary: pl.DataFrame) -> str:
    rows = [
        "| strategy | verdict | 20bps positive | 40bps positive | all subperiods positive | all adjacent params positive |",
        "|---|---|---|---|---|---|",
    ]
    for strategy in STRATEGIES:
        selected = summary.filter(pl.col("strategy") == strategy)
        costs = selected.filter(pl.col("analysis") == "cost")
        subperiods = selected.filter(pl.col("analysis") == "subperiod")
        params = selected.filter(pl.col("analysis") == "parameter")
        positive_20 = float(costs.filter(pl.col("scenario") == "20bps")["total_return"][0]) > 0
        positive_40 = float(costs.filter(pl.col("scenario") == "40bps")["total_return"][0]) > 0
        all_subperiods = bool((subperiods["total_return"] > 0).all())
        all_params = bool((params["total_return"] > 0).all())
        verdict = "internally_stable" if positive_20 and positive_40 and all_subperiods and all_params else "pause"
        rows.append(
            f"| {strategy} | {verdict} | {positive_20} | {positive_40} | "
            f"{all_subperiods} | {all_params} |"
        )
    return "\n".join(rows)


def _markdown_table(frame: pl.DataFrame) -> str:
    columns = [
        "strategy", "scenario", "period", "start", "end", "total_return",
        "annual_return", "sharpe", "max_drawdown", "annualized_turnover",
        "average_cash_weight", "trading_cost",
    ]
    header = "| " + " | ".join(columns) + " |"
    separator = "|" + "|".join("---" for _ in columns) + "|"
    rows = [header, separator]
    for row in frame.select(columns).iter_rows(named=True):
        values = []
        for column in columns:
            value = row[column]
            if isinstance(value, float):
                value = f"{value:.6f}"
            values.append(str(value))
        rows.append("| " + " | ".join(values) + " |")
    return "\n".join(rows)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=False, capture_output=True, text=True,
    ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=_date_arg, required=True)
    parser.add_argument("--end", type=_date_arg, required=True)
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.start > args.end:
        raise ValueError("start must not exceed end")
    if args.initial_capital <= 0:
        raise ValueError("initial-capital must be positive")

    output = args.output or Path("data/research/a_share_strategy_validation") / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    load_start = args.start - timedelta(days=500)
    repo = KlineRepository(DataStore())
    engine = BacktestEngine(repo)
    generation = engine.data_generation_await("stock")
    market = engine.load_market_data_matrix_for_backtest(
        None, load_start, args.end, _feature_plan(), asset_type="stock",
        expected_generation=generation,
    )
    _run_full_study(
        market,
        output=output,
        load_start=load_start,
        requested_end=args.end,
        generation=generation,
        initial_capital=args.initial_capital,
    )
    print(output)


if __name__ == "__main__":
    main()
