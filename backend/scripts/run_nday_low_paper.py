#!/usr/bin/env python3
"""n_day_low_reversal 的固定参数 paper-only 观察器 (一次跑一个观察, 不覆盖)。

口径对齐 Phase 10 的 a-share OOS observer: 手动与外部调度共用同一条一次性 CLI,
永不真实下单、不安装调度、不覆盖既有 observation; 每次从冻结起点重放到 --as-of。

与 Phase 10 的差别: 目标不是研究组合的目标权重, 而是产品内置信号策略
`n_day_low_reversal` 的影子成交。因此:
- 冻结起点 PAPER_START, 固定默认参数 (禁止逐折调参; walk-forward 已证伪);
- 用既有、已审计的回测引擎 (open_t+1, T+1, 涨跌停阻塞, 佣金/印花税/滑点) 做影子里放;
- 逐自然月落脚 (月度收益 + 已平仓盈亏);
- regime 归因 (按 T-1 环境分组入场交易的胜率/盈亏);
- 预注册停止条件 (连续两月负收益 / 滚动 126 日回撤超历史 OOS 上限 / 实际胜率跌破 40% /
  累计收益跌破 -20%), 触发即要求停止 paper。

用法:
    uv run --project backend python backend/scripts/run_nday_low_paper.py --as-of 2026-10-31
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path

import polars as pl

from app.backtest.engine import BacktestEngine
from app.backtest.research_run_manifest import (
    build_research_run_manifest,
    file_fingerprint,
    read_git_identity,
    write_research_run_manifest,
)
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.config import settings
from app.strategy import config as strategy_config
from app.strategy.engine import StrategyEngine
from app.tickflow.repository import DataStore, KlineRepository

STRATEGY_ID = "n_day_low_reversal"
# 冻结起点: 数据当前止于 2026-09-30, paper 从 2026-10 起前瞻观察。
PAPER_START = date(2026, 10, 1)
# 固定默认参数, 禁止调参 (walk-forward IS 选参 OOS 反而更差)。
FIXED_PARAMS: dict = {}
BACKTEST_KWARGS: dict = {
    "matching": "open_t+1",
    "mode": "position",
    "commission_pct": 0.0002,
    "stamp_tax_pct": 0.0005,
    "slippage_bps": 5.0,
    "max_positions": 10,
    "position_sizing": "equal",
    "initial_capital": 1_000_000.0,
}
# 历史基准 (2023-01..2026-09 固定参数滚动 OOS 与全期限): 用于对照, 不作为目标。
HISTORICAL_REFERENCE = {
    "full_period_return": 1.0566,
    "rolling_oos_compounded": 0.5541,
    "rolling_oos_positive_fold_ratio": 0.5294,
    "win_rate": 0.4711,
    "max_drawdown": -0.2705,
}
# 预注册停止条件 (常量即契约, 改动需 bump).
STOP_RULES = {
    "consecutive_negative_months": 2,
    "trailing_drawdown_window": 126,
    "trailing_drawdown_floor": round(HISTORICAL_REFERENCE["max_drawdown"] * 1.5, 4),  # ≈ -0.406
    "win_rate_floor": 0.40,
    "min_closed_trades_for_win_rate": 30,
    "cum_return_floor": -0.20,
}


# ────────────────────────── 纯函数 (可单测) ──────────────────────────

def observation_path(root: Path, as_of: date) -> Path:
    """观察目录; 已存在则拒绝 (不可覆盖)。"""
    path = root / f"as_of={as_of.isoformat()}"
    if path.exists():
        raise FileExistsError(f"observation already exists: {path}")
    return path


def regime_point_at_prev(
    regime_rows: Sequence[tuple[date, str, float]],
    boundary: date,
) -> tuple[str, float] | None:
    """返回严格早于 boundary 的最后一个 regime 点 (T-1 语义); 无则 None。"""
    hit: tuple[str, float] | None = None
    for day, state, score in regime_rows:
        if day < boundary:
            hit = (state, score)
        else:
            break
    return hit


def trailing_max_drawdown(values: Iterable[float], window: int) -> float:
    """最近 window 个净值点的最大回撤 (<=0)。不足两点返回 0。"""
    arr = [float(v) for v in values][-window:]
    if len(arr) < 2:
        return 0.0
    peak = arr[0]
    worst = 0.0
    for v in arr:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return worst


def monthly_settlement(
    equity_curve: Sequence[dict],
    trades: Sequence[dict],
) -> pl.DataFrame:
    """逐自然月落脚: 月末净值/月度收益 + 已平仓笔数/已实现盈亏/胜率。"""
    eq = [
        {"date": str(r["date"])[:10], "equity": float(r["value"])}
        for r in equity_curve
        if r.get("date") is not None and r.get("value") is not None
    ]
    if not eq:
        return pl.DataFrame(schema={
            "month": pl.String, "equity_end": pl.Float64, "monthly_return": pl.Float64,
            "closed_trades": pl.Int64, "realized_pnl": pl.Float64, "win_rate": pl.Float64,
        })
    frame = pl.DataFrame(eq).with_columns(pl.col("date").str.slice(0, 7).alias("month"))
    month_eq = frame.group_by("month").agg(pl.col("equity").last().alias("equity_end")).sort("month")
    month_eq = month_eq.with_columns(
        (pl.col("equity_end") / pl.col("equity_end").shift(1) - 1.0).alias("monthly_return")
    )
    if trades:
        tr = pl.DataFrame([
            {
                "month": str(t["exit_date"])[:7],
                "pnl_amount": float(t.get("pnl_amount") or 0.0),
                "win": 1 if float(t.get("pnl_amount") or 0.0) > 0 else 0,
            }
            for t in trades
            if t.get("exit_date") is not None
        ])
        tr_agg = tr.group_by("month").agg(
            pl.len().alias("closed_trades"),
            pl.col("pnl_amount").sum().alias("realized_pnl"),
            pl.col("win").mean().alias("win_rate"),
        )
        month_eq = month_eq.join(tr_agg, on="month", how="left")
    else:
        month_eq = month_eq.with_columns(
            pl.lit(0, dtype=pl.Int64).alias("closed_trades"),
            pl.lit(0.0).alias("realized_pnl"),
            pl.lit(None, dtype=pl.Float64).alias("win_rate"),
        )
    return month_eq.with_columns(
        pl.col("closed_trades").fill_null(0),
        pl.col("realized_pnl").fill_null(0.0),
    ).select("month", "equity_end", "monthly_return", "closed_trades", "realized_pnl", "win_rate")


def regime_attribution(
    trades: Sequence[dict],
    regime_rows: Sequence[tuple[date, str, float]],
) -> pl.DataFrame:
    """按入场 T-1 regime 分组统计交易 (归因, 不过滤)。"""
    buckets: dict[str, list[dict]] = {}
    for t in trades:
        raw = t.get("entry_signal_date") or t.get("entry_date")
        if not raw:
            continue
        boundary = date.fromisoformat(str(raw)[:10])
        point = regime_point_at_prev(regime_rows, boundary)
        state = point[0] if point else "unknown"
        buckets.setdefault(state, []).append({
            "pnl_amount": float(t.get("pnl_amount") or 0.0),
            "pnl_pct": float(t.get("pnl_pct") or 0.0),
            "win": 1 if float(t.get("pnl_amount") or 0.0) > 0 else 0,
        })
    rows = []
    for state, items in sorted(buckets.items()):
        frame = pl.DataFrame(items)
        rows.append({
            "regime_state": state,
            "trades": frame.height,
            "win_rate": float(frame["win"].mean()),
            "avg_pnl_pct": float(frame["pnl_pct"].mean()),
            "sum_pnl_amount": float(frame["pnl_amount"].sum()),
        })
    return pl.DataFrame(rows, schema={
        "regime_state": pl.String, "trades": pl.Int64, "win_rate": pl.Float64,
        "avg_pnl_pct": pl.Float64, "sum_pnl_amount": pl.Float64,
    })


def assess_stop_conditions(
    equity_curve: Sequence[dict],
    trades: Sequence[dict],
    monthly: pl.DataFrame,
) -> dict:
    """预注册停止条件评估。任一触发 → status=stop。"""
    equity = [float(r["value"]) for r in equity_curve if r.get("value") is not None]
    cum_return = (equity[-1] / equity[0] - 1.0) if len(equity) >= 2 else 0.0
    trailing_dd = trailing_max_drawdown(equity, STOP_RULES["trailing_drawdown_window"])

    closed = [t for t in trades if float(t.get("pnl_amount") or 0.0) != 0]
    win_rate = (
        sum(1 for t in closed if float(t.get("pnl_amount") or 0.0) > 0) / len(closed)
        if closed else None
    )

    negative_run = 0
    if "monthly_return" in monthly.columns and monthly.height:
        for value in monthly["monthly_return"].to_list():
            if value is not None and value < 0:
                negative_run += 1
            else:
                negative_run = 0

    triggered: list[str] = []
    if negative_run >= STOP_RULES["consecutive_negative_months"]:
        triggered.append(
            f"连续 {negative_run} 个自然月负收益 >= {STOP_RULES['consecutive_negative_months']}"
        )
    if trailing_dd <= STOP_RULES["trailing_drawdown_floor"]:
        triggered.append(
            f"滚动回撤 {trailing_dd:.4f} <= 阈值 {STOP_RULES['trailing_drawdown_floor']}"
        )
    if (
        win_rate is not None
        and len(closed) >= STOP_RULES["min_closed_trades_for_win_rate"]
        and win_rate < STOP_RULES["win_rate_floor"]
    ):
        triggered.append(f"实际胜率 {win_rate:.4f} < {STOP_RULES['win_rate_floor']}")
    if cum_return <= STOP_RULES["cum_return_floor"]:
        triggered.append(f"累计收益 {cum_return:.4f} <= {STOP_RULES['cum_return_floor']}")

    return {
        "status": "stop" if triggered else "continue",
        "triggered": triggered,
        "cum_return": round(cum_return, 4),
        "trailing_max_drawdown": round(trailing_dd, 4),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "closed_trades": len(closed),
        "consecutive_negative_months": negative_run,
        "rules": dict(STOP_RULES),
    }


def build_report_markdown(
    *,
    as_of: date,
    paper_start: date,
    summary: dict,
    stop: dict,
    regime: pl.DataFrame,
) -> str:
    lines = [
        f"# n_day_low_reversal paper 观察 @ {as_of.isoformat()}",
        "",
        f"- 冻结起点: {paper_start.isoformat()}; 固定参数: {FIXED_PARAMS or '默认'}; "
        f"成交: open_t+1 + 佣金2bps/印花税5bps/滑点5bps, Top10 等权",
        f"- 影子累计收益: {summary.get('cum_return')}; 已平仓 {summary.get('n_closed_trades')} 笔, "
        f"胜率 {summary.get('win_rate')}; 最大回撤 {summary.get('max_drawdown')}",
        f"- 历史对照 (滚动 OOS): 复利 {HISTORICAL_REFERENCE['rolling_oos_compounded']}, "
        f"正折占比 {HISTORICAL_REFERENCE['rolling_oos_positive_fold_ratio']}, "
        f"胜率 {HISTORICAL_REFERENCE['win_rate']}",
        "",
        f"## 停止条件: **{stop['status']}**",
        "",
    ]
    if stop["triggered"]:
        lines += [f"- {reason}" for reason in stop["triggered"]]
    else:
        lines.append("- 未触发")
    lines += [
        "",
        "## regime 归因 (按入场 T-1 环境, 描述性, 不过滤)",
        "",
        "| state | trades | win_rate | avg_pnl_pct | sum_pnl |",
        "|---|---|---|---|---|",
    ]
    for row in regime.iter_rows(named=True):
        lines.append(
            f"| {row['regime_state']} | {row['trades']} | {row['win_rate']:.3f} | "
            f"{row['avg_pnl_pct']:.4f} | {row['sum_pnl_amount']:.0f} |"
        )
    lines += [
        "",
        "> paper-only: 不构成实盘建议; 该策略历史滚动 OOS 仅约半数季度为正, "
        "预期波动大且在强市段会跑平/跑输。",
        "",
    ]
    return "\n".join(lines)


# ────────────────────────── 数据门 (fail-closed) ──────────────────────────

def _enriched_dates(data_dir: Path) -> list[date]:
    base = data_dir / "kline_daily_enriched"
    days = [
        date.fromisoformat(p.name.removeprefix("date="))
        for p in base.glob("date=*")
    ]
    return sorted(days)


def require_enriched_covers_as_of(
    data_dir: Path, as_of: date, *, staleness_days: int = 7
) -> tuple[list[date], date]:
    """enriched 必须覆盖到 as-of 之前最后一个交易日, 且该交易日不能距 as-of 超过 staleness_days。"""
    days = _enriched_dates(data_dir)
    if not days:
        raise RuntimeError("enriched 无任何日期分区")
    within = [d for d in days if d <= as_of]
    if not within:
        raise RuntimeError(f"enriched 最早分区 {days[0]} 晚于 as-of {as_of}")
    last_trading = within[-1]
    if (as_of - last_trading).days > staleness_days:
        raise RuntimeError(
            f"enriched 最新交易日 {last_trading} 距 as-of {as_of} 超过 {staleness_days} 天, 数据未就绪"
        )
    return days, last_trading


def require_market_completeness(
    data_dir: Path, days: list[date], last_trading: date, *, ratio: float = 0.95
) -> int:
    """最新分区有效 symbol 数不得低于近 20 日中位数 * ratio (防不完整同步)。"""
    def _count(day: date) -> int:
        part = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}" / "part.parquet"
        if not part.exists():
            return 0
        return pl.read_parquet(part, columns=["symbol"]).height

    latest = _count(last_trading)
    baseline_days = [d for d in days if d <= last_trading][-21:-1]
    if not baseline_days:
        return latest
    baseline = sorted(_count(d) for d in baseline_days)[len(baseline_days) // 2]
    if baseline > 0 and latest < baseline * ratio:
        raise RuntimeError(f"latest market coverage is incomplete: latest={latest}, baseline={baseline}")
    return latest


def require_regime_covers_as_of(data_dir: Path, last_trading: date) -> pl.DataFrame:
    from app.services import regime_builder

    regime = regime_builder.load_regime_history(data_dir)
    if regime.is_empty() or "date" not in regime.columns:
        raise RuntimeError("regime 历史为空, 无法归因")
    covered = regime.filter(pl.col("date") <= last_trading)
    if covered.is_empty() or covered["date"].max() < last_trading:
        have = covered["date"].max().isoformat() if not covered.is_empty() else "none"
        raise RuntimeError(f"regime 只覆盖到 {have}, 早于最后交易日 {last_trading}")
    return regime.sort("date")


def _build_service(data_dir: Path) -> tuple[StrategyBacktestService, StrategyEngine]:
    import app as app_pkg

    repo = KlineRepository(DataStore(data_dir))
    try:
        from app.factors.store import load_into_registry

        load_into_registry(data_dir)
    except Exception:
        pass
    builtin = Path(app_pkg.__file__).resolve().parent / "strategy" / "builtin"
    engine = StrategyEngine(
        strategy_dirs=[
            builtin,
            data_dir / "strategies" / "custom",
            data_dir / "strategies" / "ai",
            data_dir / "strategies" / "composite",
        ],
        override_loader=lambda sid: strategy_config.load_override(data_dir, sid),
    )
    return StrategyBacktestService(BacktestEngine(repo), engine), engine


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="n_day_low_reversal paper-only 观察 (一次性 CLI)")
    parser.add_argument("--as-of", required=True, help="观察截止日 YYYY-MM-DD")
    parser.add_argument("--data-dir", default=str(settings.data_dir))
    parser.add_argument("--root", default=None, help="观察根目录, 默认 <data>/research/n_day_low_reversal_paper")
    parser.add_argument("--repo", default=None, help="git 仓库根 (默认当前工作目录)")
    parser.add_argument("--paper-start", default=None, help="仅用于回溯/验证; 生产必须用冻结起点")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    as_of = date.fromisoformat(args.as_of)
    data_dir = Path(args.data_dir).resolve()
    paper_start = date.fromisoformat(args.paper_start) if args.paper_start else PAPER_START
    root = Path(args.root) if args.root else data_dir / "research" / "n_day_low_reversal_paper"
    repo = Path(args.repo).resolve() if args.repo else Path.cwd()

    if as_of < paper_start:
        raise RuntimeError(f"as-of {as_of} 早于冻结起点 {paper_start}")
    output = observation_path(root, as_of)

    days, last_trading = require_enriched_covers_as_of(data_dir, as_of)
    latest_symbols = require_market_completeness(data_dir, days, last_trading)
    regime = require_regime_covers_as_of(data_dir, last_trading)

    service, engine = _build_service(data_dir)
    engine.get(STRATEGY_ID)

    result = service.run(StrategyBacktestConfig(
        strategy_id=STRATEGY_ID,
        symbols=None,
        start=paper_start,
        end=as_of,
        params=FIXED_PARAMS,
        **BACKTEST_KWARGS,
    ))
    if result.error:
        raise RuntimeError(f"shadow backtest failed: {result.error}")

    regime_rows = [
        (r["date"], str(r["state"]), float(r["score"] or 0.0))
        for r in regime.iter_rows(named=True)
        if r.get("date") is not None
    ]
    monthly = monthly_settlement(result.equity_curve, result.trades)
    regime_tbl = regime_attribution(result.trades, regime_rows)
    stop = assess_stop_conditions(result.equity_curve, result.trades, monthly)

    equity = [float(r["value"]) for r in result.equity_curve if r.get("value") is not None]
    cum_return = round(equity[-1] / equity[0] - 1.0, 4) if len(equity) >= 2 else 0.0
    closed = [t for t in result.trades if float(t.get("pnl_amount") or 0.0) != 0]
    win_rate = round(sum(1 for t in closed if float(t["pnl_amount"]) > 0) / len(closed), 4) if closed else None
    summary = {
        "as_of": as_of.isoformat(),
        "paper_start": paper_start.isoformat(),
        "strategy_id": STRATEGY_ID,
        "params": FIXED_PARAMS,
        "cum_return": cum_return,
        "n_closed_trades": len(closed),
        "win_rate": win_rate,
        "max_drawdown": round(min((float(r["value"]) for r in result.drawdown_curve), default=0.0), 4),
        "trading_days": len(equity),
        "selection": result.stats.get("selection"),
        "historical_reference": HISTORICAL_REFERENCE,
    }

    daily = pl.DataFrame([
        {"date": str(r["date"])[:10], "equity": float(r["value"])}
        for r in result.equity_curve
        if r.get("date") is not None and r.get("value") is not None
    ])
    trades_df = pl.DataFrame(result.trades) if result.trades else pl.DataFrame()
    quality = {
        "generation": _read_generation(data_dir),
        "latest_enriched_date": last_trading.isoformat(),
        "latest_partition_symbols": latest_symbols,
        "regime_through": regime["date"].max().isoformat(),
    }

    output.mkdir(parents=True, exist_ok=False)
    daily.write_parquet(output / "daily.parquet")
    trades_df.write_parquet(output / "trades.parquet")
    monthly.write_parquet(output / "monthly.parquet")
    regime_tbl.write_parquet(output / "regime_attribution.parquet")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "stop_conditions.json").write_text(json.dumps(stop, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "data_quality.json").write_text(json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "report.md").write_text(
        build_report_markdown(as_of=as_of, paper_start=paper_start, summary=summary,
                              stop=stop, regime=regime_tbl),
        encoding="utf-8",
    )

    manifest = build_research_run_manifest(
        run_id=f"n-day-low-paper-{as_of.isoformat()}",
        run_type="n_day_low_reversal_paper_observation",
        code={**read_git_identity(repo), "script": "backend/scripts/run_nday_low_paper.py"},
        data={
            "generation": quality["generation"],
            "latest_enriched_date": quality["latest_enriched_date"],
            "latest_partition_symbols": latest_symbols,
            "regime_through": quality["regime_through"],
            "regime": file_fingerprint(data_dir / "regime_history" / "part.parquet"),
        },
        parameters={
            "strategy_id": STRATEGY_ID,
            "paper_start": paper_start.isoformat(),
            "as_of": as_of.isoformat(),
            "params": FIXED_PARAMS,
            "backtest_kwargs": BACKTEST_KWARGS,
            "stop_rules": STOP_RULES,
            "historical_reference": HISTORICAL_REFERENCE,
        },
        degradations=[
            "paper-only: 无真实成交, queue/冲击未建模, 全市场用当前上市池(幸存者偏差)",
            "策略历史滚动 OOS 仅约 53% 季度为正, 强市段预期跑平/跑输",
        ],
        sources=["enriched:kline_daily_enriched", "regime:regime_history", "report.md"],
    )
    write_research_run_manifest(output / "manifest.json", manifest)

    print(json.dumps({"output": str(output), "stop": stop["status"], "summary": summary},
                     ensure_ascii=False, indent=2))
    return 0


def _read_generation(data_dir: Path) -> str | None:
    path = data_dir / ".matrix_generation_stock.json"
    if not path.exists():
        return None
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("generation"))
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
