"""盘后雷达 V2 的冻结统计口径。

信号日组合、同日市值匹配超额和两套门槛。
全市场计算由雷达报告调用，不在导入时运行。官方状态在历史确认报告落盘前保持 draft。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Sequence

import polars as pl

from app.backtest.stats_v2 import newey_west_t

NW_LAG = 4
MIN_SIGNAL_DAYS = 100
MIN_YEAR_DAYS = 30
MIN_YEARS = 3
YEAR_RATIO = 2 / 3
LONG_T = 1.96
RISK_T = -1.96
SIZE_BUCKETS = ("小市值", "中市值", "大市值")
HISTORICAL_REPORT_PATH = Path(__file__).resolve().parents[3] / "docs" / "flow-radar-v2-historical-20260927.json"
_HISTORICAL_STATUSES = {"historical_rejected", "historical_candidate"}

_CLOSE_QUALITY = [
    {"left": "close_position", "op": ">=", "right": "0.75", "leftDays": 0, "rightDays": 0},
    {"left": "change_pct", "op": ">=", "right": "0.01", "leftDays": 0, "rightDays": 0},
]


def _v1_conditions(spec_id: str) -> list[dict[str, Any]]:
    from app.services.flow_radar import RADAR_SPECS

    for spec in RADAR_SPECS:
        if spec["id"] == spec_id:
            return [dict(item) for item in spec["conditions"]]
    raise KeyError(spec_id)


V2_SPECS: list[dict[str, Any]] = [
    {
        "id": "squeeze_expand_strong_close_v2",
        "name": "缩量后强收盘扩张V2",
        "kind": "entry",
        "direction": "long",
        "risk_label": "做多候选。在缩量后扩张上要求收盘位于当日区间上部，且收盘涨幅至少 1%。",
        "formula": (
            "10 个交易日前 amplitude_trend_20_60 <= -0.15，"
            "10 个交易日前 vol_trend_5_60 <= -0.10，"
            "当日量比 >= 1.5，振幅 >= 0.03，波动状态 >= 1.2，"
            "收盘位置 >= 0.75，涨跌幅 >= 0.01。"
        ),
        "warmup_bars": 70,
        "invalid_when": "任一条件为空则不触发。收盘质量条件用来留下强收盘的扩张日。",
        "evidence": [
            {"column": "amplitude_trend_20_60_lag10", "label": "10日前振幅趋势"},
            {"column": "vol_trend_5_60_lag10", "label": "10日前量能趋势"},
            {"column": "vol_ratio_5d", "label": "量比"},
            {"column": "amplitude", "label": "振幅"},
            {"column": "vol_regime_5_60", "label": "波动状态"},
            {"column": "close_position", "label": "收盘位置"},
            {"column": "change_pct", "label": "涨跌幅"},
        ],
        "score_parts": [
            {"column": "vol_ratio_5d", "threshold": 1.5},
            {"column": "close_position", "threshold": 0.75},
            {"column": "change_pct", "threshold": 0.01},
        ],
        "conditions": _v1_conditions("squeeze_expand_proxy") + _CLOSE_QUALITY,
    },
    {
        "id": "high_turnover_weak_close_risk_v2",
        "name": "高位换手弱收盘风险V2",
        "kind": "exit",
        "direction": "risk",
        "risk_label": "风险过滤候选。触发公式与 V1 高位换手风险相同，评价的是相对同日同市值可成交股票的超额收益。",
        "formula": (
            "position_240d >= 0.85，换手 z >= 2，收盘位置 <= 0.35，"
            "振幅 >= 0.04，成交额增幅 >= 0.5。"
        ),
        "warmup_bars": 240,
        "invalid_when": "任一条件为空则不触发。没有可匹配的同日同市值对照时，该股票不进入信号日超额。",
        "evidence": [
            {"column": "position_240d", "label": "一年位置"},
            {"column": "turnover_z_60d", "label": "换手z"},
            {"column": "close_position", "label": "收盘位置"},
            {"column": "amplitude", "label": "振幅"},
            {"column": "amount_ratio_5d", "label": "成交额增幅"},
        ],
        "score_parts": [
            {"column": "position_240d", "threshold": 0.85},
            {"column": "turnover_z_60d", "threshold": 2.0},
            {"column": "amplitude", "threshold": 0.04},
        ],
        "conditions": _v1_conditions("high_turnover_risk"),
    },
]


def spec_hash() -> str:
    """冻结规格的哈希。历史确认只在哈希一致时覆盖页面状态。"""
    payload = json.dumps(V2_SPECS, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def historical_status_fields(passed: bool) -> dict[str, str]:
    """一次历史确认的状态。通过只升到历史候选，不进入执行。"""
    if passed:
        return {
            "status": "historical_candidate",
            "status_label": "历史候选",
            "status_note": "历史主门槛全部通过。这是研究候选，不发布为策略。假设来自已看过的 V1，前向样本仍要单独累计。",
        }
    return {
        "status": "historical_rejected",
        "status_label": "历史未通过",
        "status_note": "历史主门槛未通过。报告已落盘。公式、成本、5 日持有期和门槛保持冻结；新假设另建 V2.1。",
    }


def overlay_historical_status(rows: list[dict[str, Any]], path: Path | None = None) -> list[dict[str, Any]]:
    """规格哈希与落盘报告一致时，用该次确认的状态替换研究中。"""
    path = HISTORICAL_REPORT_PATH if path is None else path
    if not path.is_file():
        return rows
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return rows
    if record.get("spec_hash") != spec_hash():
        return rows
    saved = {
        item["id"]: item
        for item in record.get("signals") or []
        if isinstance(item, dict) and item.get("id")
    }
    updated: list[dict[str, Any]] = []
    for row in rows:
        hit = saved.get(row.get("id"))
        if hit is None or hit.get("status") not in _HISTORICAL_STATUSES:
            updated.append(row)
            continue
        updated.append({
            **row,
            "status": hit["status"],
            "status_label": hit.get("status_label") or row.get("status_label"),
            "status_note": hit.get("status_note") or row.get("status_note"),
        })
    return updated


def signal_day_newey_west(values: Sequence[float]) -> tuple[float, float, float] | None:
    """信号日收益的 Newey-West t。lag 固定为 4，对应 5 日持有的重叠。"""
    return newey_west_t(values, NW_LAG)


def aggregate_signal_days(frame: pl.DataFrame) -> pl.DataFrame:
    """同一交易日的可成交触发股票等权合成一个信号日。主样本按日计数。"""
    if frame.is_empty():
        return _daily_schema("portfolio_ret")
    usable = frame.filter(
        (pl.col("entry_state") == "tradable")
        & pl.col("net_ret").is_not_null()
        & pl.col("net_ret").is_finite()
    )
    if usable.is_empty():
        return _daily_schema("portfolio_ret")
    return (
        usable.group_by("date")
        .agg(
            pl.len().alias("n_names"),
            pl.col("net_ret").mean().alias("portfolio_ret"),
        )
        .with_columns(pl.col("date").dt.year().cast(pl.Int64).alias("year"))
        .select(["date", "year", "n_names", "portfolio_ret"])
        .sort("date")
    )


def risk_excess_by_signal_day(frame: pl.DataFrame) -> pl.DataFrame:
    """触发股票相对同日、同市值三分位、未触发且可成交股票的费后收益中位数。

    匹配键只有日期和市值桶。行业列即使存在也不参与。
    没有对照样本的触发股票不进入该日超额。没有任何可用触发时，这一天不是信号日。
    """
    if frame.is_empty() or "triggered" not in frame.columns:
        return _daily_schema("excess")
    usable = frame.filter(
        (pl.col("entry_state") == "tradable")
        & pl.col("net_ret").is_not_null()
        & pl.col("net_ret").is_finite()
        & pl.col("size_bucket").is_in(list(SIZE_BUCKETS))
    )
    controls = usable.filter(~pl.col("triggered"))
    triggers = usable.filter(pl.col("triggered"))
    if controls.is_empty() or triggers.is_empty():
        return _daily_schema("excess")
    peers = controls.group_by(["date", "size_bucket"]).agg(
        pl.col("net_ret").median().alias("peer_median"),
    )
    matched = triggers.join(peers, on=["date", "size_bucket"], how="inner").with_columns(
        (pl.col("net_ret") - pl.col("peer_median")).alias("stock_excess"),
    )
    if matched.is_empty():
        return _daily_schema("excess")
    return (
        matched.group_by("date")
        .agg(
            pl.len().alias("n_names"),
            pl.col("stock_excess").mean().alias("excess"),
        )
        .with_columns(pl.col("date").dt.year().cast(pl.Int64).alias("year"))
        .select(["date", "year", "n_names", "excess"])
        .sort("date")
    )


def long_gate(frame: pl.DataFrame) -> dict[str, Any]:
    """做多门槛。中位数必须大于 0，避免右尾均值单独通过。"""
    return _evaluate_gate(
        frame,
        "portfolio_ret",
        positive=True,
        rate_key="win_rate",
        rate_failure="win_rate",
    )


def risk_gate(frame: pl.DataFrame) -> dict[str, Any]:
    """风险门槛。通过只表示回避或降权候选。"""
    return _evaluate_gate(
        frame,
        "excess",
        positive=False,
        rate_key="negative_rate",
        rate_failure="negative_rate",
    )


def _evaluate_gate(
    frame: pl.DataFrame,
    column: str,
    *,
    positive: bool,
    rate_key: str,
    rate_failure: str,
) -> dict[str, Any]:
    rows = [] if frame.is_empty() else frame.select(["date", column]).iter_rows(named=True)
    grouped: dict[int, list[float]] = {}
    values: list[float] = []
    for row in rows:
        value = float(row[column])
        values.append(value)
        year = int(row["date"].year)
        grouped.setdefault(year, []).append(value)

    mean = _mean(values)
    median = _median(values)
    if positive:
        rate = _share(values, lambda item: item > 0)
        t_limit = LONG_T
        mean_ok = mean is not None and mean > 0
        median_ok = median is not None and median > 0
        year_ok = lambda item: item > 0
    else:
        rate = _share(values, lambda item: item < 0)
        t_limit = RISK_T
        mean_ok = mean is not None and mean < 0
        median_ok = median is not None and median < 0
        year_ok = lambda item: item < 0
    rate_ok = rate is not None and rate > 0.5

    pack = signal_day_newey_west(values)
    t_stat = None if pack is None else float(pack[0])
    if positive:
        t_ok = t_stat is not None and t_stat > t_limit
    else:
        t_ok = t_stat is not None and t_stat < t_limit

    eligible = []
    for year, year_values in sorted(grouped.items()):
        if len(year_values) < MIN_YEAR_DAYS:
            continue
        year_mean = _mean(year_values)
        eligible.append({"year": str(year), "n": len(year_values), "mean": year_mean})
    directional = [item for item in eligible if item["mean"] is not None and year_ok(item["mean"])]
    year_ratio = (len(directional) / len(eligible)) if eligible else 0.0
    years_ok = len(eligible) >= MIN_YEARS
    year_sign_ok = years_ok and year_ratio >= YEAR_RATIO

    failures: list[str] = []
    if len(values) < MIN_SIGNAL_DAYS:
        failures.append("signal_days")
    if not mean_ok:
        failures.append("mean")
    if not median_ok:
        failures.append("median")
    if not rate_ok:
        failures.append(rate_failure)
    if not t_ok:
        failures.append("newey_west")
    if not years_ok:
        failures.append("years")
    if not year_sign_ok:
        failures.append("year_sign")

    return {
        "passed": not failures,
        "failures": failures,
        "observed": {
            "signal_days": len(values),
            "mean": mean,
            "median": median,
            rate_key: rate,
            "newey_west_t": t_stat,
            "newey_west_lag": NW_LAG,
            "eligible_years": len(eligible),
            "directional_year_ratio": year_ratio,
            "years": eligible,
        },
    }


def _daily_schema(value_name: str) -> pl.DataFrame:
    return pl.DataFrame(schema={
        "date": pl.Date,
        "year": pl.Int64,
        "n_names": pl.UInt32,
        "value_name": pl.Float64,
    }).rename({"value_name": value_name})


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2)


def _share(values: Sequence[float], predicate) -> float | None:
    if not values:
        return None
    return float(sum(1 for item in values if predicate(item)) / len(values))


def ensure_v2_signals(data_dir: Path) -> list[dict[str, Any]]:
    """缺文件时写入两个 V2 规格。已有文件不覆盖，V1 文件不改。"""
    from app.services.flow_radar import signal_document
    from app.strategy import custom_signals

    loaded: list[dict[str, Any]] = []
    for spec in V2_SPECS:
        document = signal_document(spec)
        custom_signals.validate(document)
        path = data_dir / "user_data" / "custom_signals" / f"{spec['id']}.json"
        if not path.exists():
            custom_signals.save_one(data_dir, document)
        raw = json.loads(path.read_text(encoding="utf-8"))
        custom_signals.validate(raw)
        loaded.append(raw)
    return loaded


def summarize_v2(
    frame: pl.DataFrame,
    *,
    cost: float,
    study_start: date,
    as_of: date | None = None,
    names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """在已经注入 csg_ 列和成交路径的面板上，计算两个 V2 的门槛证据。"""
    names = names or {}
    long_spec = V2_SPECS[0]
    risk_spec = V2_SPECS[1]
    return [
        _long_report(frame, long_spec, cost=cost, study_start=study_start, as_of=as_of, names=names),
        _risk_report(frame, risk_spec, cost=cost, study_start=study_start, as_of=as_of, names=names),
    ]


def _trigger_expr(spec: dict[str, Any], study_start: date) -> pl.Expr:
    flag = f"csg_{spec['id']}"
    return (
        pl.col(flag).fill_null(False)
        & (pl.col("_bar") >= int(spec["warmup_bars"]) - 1)
        & (pl.col("date") >= study_start)
    )


def _net_expr(horizon: int, cost: float) -> pl.Expr:
    return (
        pl.when(
            (pl.col("entry_state") == "tradable")
            & pl.col(f"path_ok_{horizon}").fill_null(False)
            & ~pl.col(f"exit_blocked_{horizon}").fill_null(False)
            & pl.col(f"trade_ret_{horizon}").is_not_null()
            & pl.col(f"trade_ret_{horizon}").is_finite()
        )
        .then(pl.col(f"trade_ret_{horizon}") - cost)
        .otherwise(None)
    )


def _horizon_ready(frame: pl.DataFrame, horizon: int) -> bool:
    return {f"trade_ret_{horizon}", f"path_ok_{horizon}", f"exit_blocked_{horizon}"}.issubset(frame.columns)


def _long_report(
    frame: pl.DataFrame,
    spec: dict[str, Any],
    *,
    cost: float,
    study_start: date,
    as_of: date | None,
    names: dict[str, str],
) -> dict[str, Any]:
    triggers = _triggered(frame, spec, study_start)
    if _horizon_ready(frame, 5):
        events = triggers.with_columns(_net_expr(5, cost).alias("net_ret"))
        daily = aggregate_signal_days(events)
    else:
        events = pl.DataFrame()
        daily = aggregate_signal_days(events)
    gate = long_gate(daily.select(["date", "portfolio_ret"])) if not daily.is_empty() else long_gate(pl.DataFrame({"date": [], "portfolio_ret": []}))
    horizons: dict[str, dict[str, Any]] = {}
    for horizon in (1, 5, 10, 20):
        if not _horizon_ready(frame, horizon):
            continue
        cohort = aggregate_signal_days(triggers.with_columns(_net_expr(horizon, cost).alias("net_ret")))
        horizons[str(horizon)] = _cohort_stats(cohort, "portfolio_ret")
    return _identity(spec) | {
        "events": triggers.height,
        "signal_days": daily.height,
        "names_per_day": _name_span(daily),
        "gate": _public_gate(gate, "win_rate"),
        "disclosure": {
            "horizons": horizons,
            "by_size": _sized_cohorts(events),
            "by_regime": _regime_cohorts(events, daily, "portfolio_ret"),
        },
        "candidates": _candidates(frame, spec, study_start, as_of, names),
    }


def _risk_report(
    frame: pl.DataFrame,
    spec: dict[str, Any],
    *,
    cost: float,
    study_start: date,
    as_of: date | None,
    names: dict[str, str],
) -> dict[str, Any]:
    if frame.is_empty() or not _horizon_ready(frame, 5):
        daily = risk_excess_by_signal_day(pl.DataFrame())
        triggers_n = 0
    else:
        window = frame.filter(pl.col("date") >= study_start)
        events = window.with_columns([
            _trigger_expr(spec, study_start).alias("triggered"),
            _net_expr(5, cost).alias("net_ret"),
        ])
        daily = risk_excess_by_signal_day(events)
        triggers_n = window.filter(_trigger_expr(spec, study_start)).height
    gate = risk_gate(daily.select(["date", "excess"])) if not daily.is_empty() else risk_gate(pl.DataFrame({"date": [], "excess": []}))
    return _identity(spec) | {
        "events": triggers_n,
        "signal_days": daily.height,
        "names_per_day": _name_span(daily),
        "gate": _public_gate(gate, "negative_rate"),
        "disclosure": {
            "by_size": _risk_by_size(frame, spec, cost, study_start),
            "by_regime": _risk_by_regime(frame, spec, study_start, daily),
        },
        "candidates": _candidates(frame, spec, study_start, as_of, names),
    }


def _triggered(frame: pl.DataFrame, spec: dict[str, Any], study_start: date) -> pl.DataFrame:
    flag = f"csg_{spec['id']}"
    if frame.is_empty() or flag not in frame.columns:
        return frame.clear() if not frame.is_empty() else frame
    return frame.filter(_trigger_expr(spec, study_start))


def _identity(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": spec["id"],
        "name": spec["name"],
        "direction": spec["direction"],
        "status": "draft",
        "status_label": "研究中",
        "status_note": "历史确认报告尚未落盘。门槛可以按冻结规则现算，状态仍停在研究中。",
        "risk_label": spec["risk_label"],
        "formula": spec["formula"],
        "warmup_bars": spec["warmup_bars"],
        "invalid_when": spec["invalid_when"],
        "column": f"csg_{spec['id']}",
        "evidence": spec["evidence"],
    }


def _public_gate(gate: dict[str, Any], rate_key: str) -> dict[str, Any]:
    observed = gate["observed"]
    years = [
        {"year": item["year"], "n": item["n"], "mean": _round(item["mean"])}
        for item in observed.get("years") or []
    ]
    return {
        "passed": bool(gate["passed"]),
        "failures": list(gate["failures"]),
        "observed": {
            "signal_days": int(observed["signal_days"]),
            "mean": _round(observed["mean"]),
            "median": _round(observed["median"]),
            rate_key: _round(observed.get(rate_key)),
            "newey_west_t": _round(observed["newey_west_t"]),
            "newey_west_lag": NW_LAG,
            "eligible_years": int(observed["eligible_years"]),
            "directional_year_ratio": _round(observed["directional_year_ratio"], 4),
            "years": years,
        },
    }


def _cohort_stats(daily: pl.DataFrame, column: str) -> dict[str, Any]:
    if daily.is_empty():
        return {"signal_days": 0, "mean": None, "median": None, "win_rate": None}
    values = [float(item) for item in daily[column].to_list()]
    return {
        "signal_days": len(values),
        "mean": _round(_mean(values)),
        "median": _round(_median(values)),
        "win_rate": _round(_share(values, lambda item: item > 0)),
    }


def _name_span(daily: pl.DataFrame) -> dict[str, int | None]:
    if daily.is_empty() or "n_names" not in daily.columns:
        return {"min": None, "median": None, "max": None}
    counts = [int(item) for item in daily["n_names"].to_list()]
    return {
        "min": min(counts),
        "median": int(round(_median(counts) or 0)),
        "max": max(counts),
    }


def _sized_cohorts(events: pl.DataFrame) -> list[dict[str, Any]]:
    if events.is_empty() or "size_bucket" not in events.columns:
        return []
    rows = []
    for bucket in SIZE_BUCKETS:
        cohort = aggregate_signal_days(events.filter(pl.col("size_bucket") == bucket))
        if cohort.is_empty():
            continue
        stats = _cohort_stats(cohort, "portfolio_ret")
        rows.append({"key": bucket, **stats})
    return rows


def _regime_cohorts(events: pl.DataFrame, daily: pl.DataFrame, column: str) -> list[dict[str, Any]]:
    if events.is_empty() or daily.is_empty() or "regime" not in events.columns:
        return []
    labeled = events.filter(
        (pl.col("entry_state") == "tradable") & pl.col("net_ret").is_not_null()
    )
    if labeled.is_empty():
        return []
    regimes = labeled.group_by("date").agg(pl.col("regime").drop_nulls().first().alias("regime"))
    joined = daily.join(regimes, on="date", how="left")
    rows = []
    for regime in joined.get_column("regime").unique().to_list():
        key = "未知" if regime is None else str(regime)
        cohort = joined.filter(pl.col("regime").is_null() if regime is None else pl.col("regime") == regime)
        stats = _cohort_stats(cohort, column)
        rows.append({"key": key, **stats})
    return sorted(rows, key=lambda item: item["key"])


def _risk_by_size(frame: pl.DataFrame, spec: dict[str, Any], cost: float, study_start: date) -> list[dict[str, Any]]:
    if frame.is_empty() or not _horizon_ready(frame, 5) or "size_bucket" not in frame.columns:
        return []
    rows = []
    window = frame.filter(pl.col("date") >= study_start)
    for bucket in SIZE_BUCKETS:
        scoped = window.filter(pl.col("size_bucket") == bucket).with_columns([
            _trigger_expr(spec, study_start).alias("triggered"),
            _net_expr(5, cost).alias("net_ret"),
        ])
        cohort = risk_excess_by_signal_day(scoped)
        if cohort.is_empty():
            continue
        values = [float(item) for item in cohort["excess"].to_list()]
        rows.append({
            "key": bucket,
            "signal_days": len(values),
            "mean": _round(_mean(values)),
            "median": _round(_median(values)),
        })
    return rows


def _risk_by_regime(
    frame: pl.DataFrame,
    spec: dict[str, Any],
    study_start: date,
    daily: pl.DataFrame,
) -> list[dict[str, Any]]:
    if daily.is_empty() or frame.is_empty() or "regime" not in frame.columns:
        return []
    triggers = _triggered(frame, spec, study_start)
    if triggers.is_empty():
        return []
    regimes = triggers.group_by("date").agg(pl.col("regime").drop_nulls().first().alias("regime"))
    joined = daily.join(regimes, on="date", how="left")
    rows = []
    for regime in joined.get_column("regime").unique().to_list():
        key = "未知" if regime is None else str(regime)
        cohort = joined.filter(pl.col("regime").is_null() if regime is None else pl.col("regime") == regime)
        values = [float(item) for item in cohort["excess"].to_list()]
        rows.append({
            "key": key,
            "signal_days": len(values),
            "mean": _round(_mean(values)),
            "median": _round(_median(values)),
        })
    return sorted(rows, key=lambda item: item["key"])


def _candidates(
    frame: pl.DataFrame,
    spec: dict[str, Any],
    study_start: date,
    as_of: date | None,
    names: dict[str, str],
) -> list[dict[str, Any]]:
    if as_of is None or frame.is_empty():
        return []
    flag = f"csg_{spec['id']}"
    if flag not in frame.columns:
        return []
    hits = frame.filter((pl.col("date") == as_of) & _trigger_expr(spec, study_start))
    if hits.is_empty():
        return []
    parts = [
        (pl.col(part["column"]) / float(part["threshold"])).clip(0.0, 3.0)
        for part in spec["score_parts"]
        if part["column"] in hits.columns
    ]
    if parts:
        hits = hits.with_columns(pl.mean_horizontal(parts).alias("_score"))
    else:
        hits = hits.with_columns(pl.lit(None).cast(pl.Float64).alias("_score"))
    hits = hits.sort("_score", descending=True, nulls_last=True).head(20)
    evidence_cols = [item["column"] for item in spec["evidence"] if item["column"] in hits.columns]
    rows = []
    for row in hits.iter_rows(named=True):
        symbol = str(row["symbol"])
        rows.append({
            "symbol": symbol,
            "name": names.get(symbol, ""),
            "date": as_of.isoformat(),
            "score": _round(row.get("_score"), 4),
            "evidence": {column: _round(row.get(column), 4) for column in evidence_cols},
        })
    return rows


def _round(value: Any, ndigits: int = 6) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, ndigits)
