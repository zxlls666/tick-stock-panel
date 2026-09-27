"""日频盘后资金异动雷达。

信号定义走 custom_signals（AND 条件，列名 csg_*）。
触发、证据和事件统计在读取时从行情重算，不另写触发表。
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.strategy import custom_signals

logger = logging.getLogger(__name__)

STUDY_START = date(2021, 1, 4)
LOAD_START = date(2020, 9, 9)
HORIZONS = (1, 5, 10, 20)
GATE_HORIZON = 5
ROUND_TRIP_COST = 0.0002 * 2 + 0.0005 + 0.0005 * 2  # 佣金万二双边 + 印花税万五 + 滑点 5bp 双边
MIN_YEAR_N = 30
MIN_GATE_YEARS = 3
MIN_GATE_N = 100
CANDIDATE_LIMIT = 40

REGIME_LABEL = {
    "strong": "强",
    "lean_strong": "偏强",
    "range": "震荡",
    "lean_weak": "偏弱",
    "weak": "弱",
}

# 研究门槛在看结果之前写死：费后 5 日、可成交样本、年份符号。
# 过不了就记「未显示稳定的费后优势」，不改阈值。

RADAR_SPECS: list[dict[str, Any]] = [
    {
        "id": "abnormal_turnover",
        "name": "异常换手代理",
        "kind": "both",
        "risk_label": "换手异常代理。证据是换手率相对自身前 60 日的 z 分数。",
        "formula": "(当日换手率 - 前60日均值) / 前60日标准差 >= 2。标准差为 0 或样本不足时因子为空。",
        "warmup_bars": 61,
        "invalid_when": "换手率缺失，或前 60 日标准差为 0、样本不足时，因子为空，条件不成立。",
        "evidence": [
            {"column": "turnover_z_60d", "label": "换手z"},
            {"column": "turnover_rate", "label": "换手率"},
        ],
        "score_parts": [{"column": "turnover_z_60d", "threshold": 2.0}],
        "conditions": [
            {"left": "turnover_z_60d", "op": ">=", "right": "2", "leftDays": 0, "rightDays": 0},
        ],
    },
    {
        "id": "volume_breakout_proxy",
        "name": "放量突破代理",
        "kind": "both",
        "risk_label": "放量且收盘贴近 60 日收盘高点的日线代理。",
        "formula": (
            "成交额相对前5日均额的增幅 >= 0.5（至少 1.5 倍），"
            "量比 vol_ratio_5d >= 1.5（量比本身是倍数，不减 1），"
            "换手 z >= 1，收盘涨跌幅 >= 0，"
            "distance_to_high_60d >= -0.001。"
            "high_60d 是含当日的 60 日最高收盘，所以该条件表示收盘处在这 60 根收盘高点的 0.1% 以内。"
        ),
        "warmup_bars": 61,
        "invalid_when": "任一组成列为空则不触发。预热不足 61 根的行不进入统计。",
        "evidence": [
            {"column": "amount_ratio_5d", "label": "成交额增幅"},
            {"column": "vol_ratio_5d", "label": "量比"},
            {"column": "turnover_z_60d", "label": "换手z"},
            {"column": "distance_to_high_60d", "label": "距60日高点"},
            {"column": "change_pct", "label": "涨跌幅"},
        ],
        "score_parts": [
            {"column": "amount_ratio_5d", "threshold": 0.5},
            {"column": "vol_ratio_5d", "threshold": 1.5},
            {"column": "turnover_z_60d", "threshold": 1.0},
        ],
        "conditions": [
            {"left": "amount_ratio_5d", "op": ">=", "right": "0.5", "leftDays": 0, "rightDays": 0},
            {"left": "vol_ratio_5d", "op": ">=", "right": "1.5", "leftDays": 0, "rightDays": 0},
            {"left": "turnover_z_60d", "op": ">=", "right": "1", "leftDays": 0, "rightDays": 0},
            {"left": "distance_to_high_60d", "op": ">=", "right": "-0.001", "leftDays": 0, "rightDays": 0},
            {"left": "change_pct", "op": ">=", "right": "0", "leftDays": 0, "rightDays": 0},
        ],
    },
    {
        "id": "volume_stall_risk",
        "name": "放量滞涨风险",
        "kind": "exit",
        "risk_label": "高成交但收盘涨跌幅停在 ±1% 以内，且收盘不在日内高位。风险标签。",
        "formula": (
            "成交额增幅 >= 1（至少 2 倍于前5日均额），换手 z >= 1.5，"
            "-0.01 <= 收盘涨跌幅 <= 0.01，收盘位置 close_position <= 0.55。"
            "涨跌幅和振幅都是比值，0.01 表示 1%。"
        ),
        "warmup_bars": 61,
        "invalid_when": "任一组成列为空则不触发。收盘涨跌停的单日涨跌幅通常超出 ±1%，不会进入本规则。",
        "evidence": [
            {"column": "amount_ratio_5d", "label": "成交额增幅"},
            {"column": "turnover_z_60d", "label": "换手z"},
            {"column": "change_pct", "label": "涨跌幅"},
            {"column": "close_position", "label": "收盘位置"},
        ],
        "score_parts": [
            {"column": "amount_ratio_5d", "threshold": 1.0},
            {"column": "turnover_z_60d", "threshold": 1.5},
        ],
        "conditions": [
            {"left": "amount_ratio_5d", "op": ">=", "right": "1", "leftDays": 0, "rightDays": 0},
            {"left": "turnover_z_60d", "op": ">=", "right": "1.5", "leftDays": 0, "rightDays": 0},
            {"left": "change_pct", "op": "<=", "right": "0.01", "leftDays": 0, "rightDays": 0},
            {"left": "change_pct", "op": ">=", "right": "-0.01", "leftDays": 0, "rightDays": 0},
            {"left": "close_position", "op": "<=", "right": "0.55", "leftDays": 0, "rightDays": 0},
        ],
    },
    {
        "id": "high_turnover_risk",
        "name": "高位换手风险",
        "kind": "exit",
        "risk_label": "一年价格位置高、换手异常、当日振幅不窄且收盘靠近最低价。长上影没有独立列，这里用收盘位置作日线代理，影线和实体没有分开。风险标签。",
        "formula": (
            "position_240d >= 0.85，换手 z >= 2，振幅 >= 0.04，"
            "成交额增幅 >= 0.5，收盘位置 <= 0.35。"
            "position_240d 用近 240 根收盘的最低到最高。"
        ),
        "warmup_bars": 240,
        "invalid_when": "240 根收盘不足时 position_240d 为空，不触发。最高价等于最低价时收盘位置为空，不触发。",
        "evidence": [
            {"column": "position_240d", "label": "一年位置"},
            {"column": "turnover_z_60d", "label": "换手z"},
            {"column": "amplitude", "label": "振幅"},
            {"column": "close_position", "label": "收盘位置"},
            {"column": "amount_ratio_5d", "label": "成交额增幅"},
        ],
        "score_parts": [
            {"column": "position_240d", "threshold": 0.85},
            {"column": "turnover_z_60d", "threshold": 2.0},
            {"column": "amplitude", "threshold": 0.04},
        ],
        "conditions": [
            {"left": "position_240d", "op": ">=", "right": "0.85", "leftDays": 0, "rightDays": 0},
            {"left": "turnover_z_60d", "op": ">=", "right": "2", "leftDays": 0, "rightDays": 0},
            {"left": "close_position", "op": "<=", "right": "0.35", "leftDays": 0, "rightDays": 0},
            {"left": "amplitude", "op": ">=", "right": "0.04", "leftDays": 0, "rightDays": 0},
            {"left": "amount_ratio_5d", "op": ">=", "right": "0.5", "leftDays": 0, "rightDays": 0},
        ],
    },
    {
        "id": "close_absorption_proxy",
        "name": "异常承接代理",
        "kind": "both",
        "risk_label": "日线粗代理：成交额放大，最低价低于开盘，收盘位于当日区间上沿且不低于昨收。日线看不到下探和收回的先后。",
        "formula": (
            "成交额增幅 >= 0.5，振幅 >= 0.03，收盘位置 >= 0.75，"
            "收盘涨跌幅 >= 0，最低价 < 开盘价。"
        ),
        "warmup_bars": 6,
        "invalid_when": "最高价等于最低价时收盘位置为空，不触发。成交额窗口不足时不触发。",
        "evidence": [
            {"column": "amount_ratio_5d", "label": "成交额增幅"},
            {"column": "amplitude", "label": "振幅"},
            {"column": "close_position", "label": "收盘位置"},
            {"column": "change_pct", "label": "涨跌幅"},
        ],
        "score_parts": [
            {"column": "amount_ratio_5d", "threshold": 0.5},
            {"column": "close_position", "threshold": 0.75},
            {"column": "amplitude", "threshold": 0.03},
        ],
        "conditions": [
            {"left": "amount_ratio_5d", "op": ">=", "right": "0.5", "leftDays": 0, "rightDays": 0},
            {"left": "amplitude", "op": ">=", "right": "0.03", "leftDays": 0, "rightDays": 0},
            {"left": "close_position", "op": ">=", "right": "0.75", "leftDays": 0, "rightDays": 0},
            {"left": "change_pct", "op": ">=", "right": "0", "leftDays": 0, "rightDays": 0},
            {"left": "low", "op": "<", "right": "field:open", "leftDays": 0, "rightDays": 0},
        ],
    },
    {
        "id": "squeeze_expand_proxy",
        "name": "缩量后扩张代理",
        "kind": "both",
        "risk_label": "固定提前 10 个交易日看收缩，再看当日放量放大。这是规则偏移，不是识别出来的整理形态。",
        "formula": (
            "10 个交易日前 amplitude_trend_20_60 <= -0.15 且 vol_trend_5_60 <= -0.10，"
            "当日量比 >= 1.5，振幅 >= 0.03，vol_regime_5_60 >= 1.2。"
        ),
        "warmup_bars": 70,
        "invalid_when": "60 日窗口或 10 日偏移不足时条件为空，不触发。收缩窗口和放大日可能重叠。",
        "evidence": [
            {"column": "amplitude_trend_20_60_lag10", "label": "10日前振幅趋势"},
            {"column": "vol_trend_5_60_lag10", "label": "10日前量能趋势"},
            {"column": "vol_ratio_5d", "label": "量比"},
            {"column": "amplitude", "label": "振幅"},
            {"column": "vol_regime_5_60", "label": "波动状态"},
        ],
        "score_parts": [
            {"column": "vol_ratio_5d", "threshold": 1.5},
            {"column": "amplitude", "threshold": 0.03},
            {"column": "vol_regime_5_60", "threshold": 1.2},
        ],
        "conditions": [
            {"left": "amplitude_trend_20_60", "op": "<=", "right": "-0.15", "leftDays": 10, "rightDays": 0},
            {"left": "vol_trend_5_60", "op": "<=", "right": "-0.10", "leftDays": 10, "rightDays": 0},
            {"left": "vol_ratio_5d", "op": ">=", "right": "1.5", "leftDays": 0, "rightDays": 0},
            {"left": "amplitude", "op": ">=", "right": "0.03", "leftDays": 0, "rightDays": 0},
            {"left": "vol_regime_5_60", "op": ">=", "right": "1.2", "leftDays": 0, "rightDays": 0},
        ],
    },
]

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, dict[str, Any]] = {}


def spec_by_id(signal_id: str) -> dict[str, Any] | None:
    for spec in RADAR_SPECS:
        if spec["id"] == signal_id:
            return spec
    return None


def signal_document(spec: dict[str, Any]) -> dict[str, Any]:
    """落成 custom_signals 能校验的 JSON，并带上雷达说明。"""
    return {
        "id": spec["id"],
        "name": spec["name"],
        "kind": spec["kind"],
        "timeframe": "daily",
        "enabled": True,
        "conditions": spec["conditions"],
        "radar": {
            "risk_label": spec["risk_label"],
            "formula": spec["formula"],
            "warmup_bars": spec["warmup_bars"],
            "invalid_when": spec["invalid_when"],
            "evidence": spec["evidence"],
            "score_parts": spec["score_parts"],
        },
    }


def ensure_radar_signals(data_dir: Path) -> list[dict[str, Any]]:
    """缺文件时写入规格。已有文件不覆盖，避免冲掉人工修改。"""
    loaded: list[dict[str, Any]] = []
    for spec in RADAR_SPECS:
        custom_signals.validate(signal_document(spec))
        path = data_dir / "user_data" / "custom_signals" / f"{spec['id']}.json"
        if not path.exists():
            custom_signals.save_one(data_dir, signal_document(spec))
        raw = json.loads(path.read_text(encoding="utf-8"))
        custom_signals.validate(raw)
        loaded.append(raw)
    return loaded


def _conditions_match(spec: dict[str, Any], raw: dict[str, Any]) -> bool:
    def norm(cond: dict) -> tuple:
        return (
            cond.get("left"),
            cond.get("op"),
            str(cond.get("right")),
            int(cond.get("leftDays") or 0),
            int(cond.get("rightDays") or 0),
        )

    left = [norm(c) for c in spec["conditions"]]
    right = [norm(c) for c in raw.get("conditions") or []]
    return left == right


def research_gate(summary: dict[str, Any]) -> dict[str, Any]:
    """费后 5 日研究门槛。门槛写在本函数里，调用方不按结果改。"""
    horizon = (summary.get("horizons") or {}).get(str(GATE_HORIZON)) or {}
    years = [
        row for row in summary.get("by_year") or []
        if int(row.get("n") or 0) >= MIN_YEAR_N and row.get("mean_net_ret") is not None
    ]
    positive = sum(1 for row in years if float(row["mean_net_ret"]) > 0)
    ratio = positive / len(years) if years else 0.0
    n = int(horizon.get("n") or 0)
    mean_net = horizon.get("mean_net_ret")
    win = horizon.get("win_rate_net")
    passed = (
        n >= MIN_GATE_N
        and mean_net is not None
        and float(mean_net) > 0
        and win is not None
        and float(win) > 0.5
        and len(years) >= MIN_GATE_YEARS
        and ratio >= 2 / 3
    )
    return {
        "passed": passed,
        "label": "费后5日研究门槛通过" if passed else "未显示稳定的费后优势",
        "horizon": GATE_HORIZON,
        "rules": {
            "min_tradable_n": MIN_GATE_N,
            "mean_net_ret_gt": 0,
            "win_rate_net_gt": 0.5,
            "min_years": MIN_GATE_YEARS,
            "min_year_n": MIN_YEAR_N,
            "positive_year_ratio_gte": 2 / 3,
            "round_trip_cost": ROUND_TRIP_COST,
        },
        "observed": {
            "n": n,
            "mean_net_ret": mean_net,
            "win_rate_net": win,
            "years": len(years),
            "positive_year_ratio": round(ratio, 4),
        },
    }


def _num(value: Any, ndigits: int = 6) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, ndigits)


def _group_note(rows: list[dict[str, Any]], key: str) -> str:
    usable = [
        row for row in rows
        if int(row.get("n") or 0) >= MIN_YEAR_N and row.get("mean_net_ret") is not None
    ]
    if len(usable) < 2:
        return "分组样本不足"
    signs = {float(row["mean_net_ret"]) > 0 for row in usable}
    if len(signs) > 1:
        return f"{key}分组的费后5日收益符号不一致"
    return f"{key}分组的费后5日收益符号一致"


def summarize_triggers(frame: pl.DataFrame, spec: dict[str, Any]) -> dict[str, Any]:
    """对已经带好路径列的触发行做事件统计。frame 只含本信号的触发。"""
    counts = {state: 0 for state in ("no_next_bar", "suspended", "limit_up_locked", "invalid_price", "tradable")}
    if frame.is_empty():
        horizons = {str(h): _empty_horizon() for h in HORIZONS}
        summary = {
            "triggers": 0,
            "entry": counts,
            "horizons": horizons,
            "by_year": [],
            "by_size": [],
            "by_regime": [],
        }
        summary["gate"] = research_gate(summary)
        summary["size_note"] = "分组样本不足"
        summary["regime_note"] = "分组样本不足"
        return summary

    grouped = frame.group_by("entry_state").len()
    for row in grouped.iter_rows(named=True):
        state = str(row["entry_state"])
        if state in counts:
            counts[state] = int(row["len"])

    horizons: dict[str, dict[str, Any]] = {}
    for horizon in HORIZONS:
        horizons[str(horizon)] = _horizon_block(frame, horizon)

    tradable = _tradable_exits(frame, GATE_HORIZON)
    by_year = _group_stats(tradable, "year")
    by_size = _group_stats(tradable, "size_bucket")
    by_regime = _group_stats(tradable, "regime")
    summary = {
        "triggers": frame.height,
        "entry": counts,
        "horizons": horizons,
        "by_year": by_year,
        "by_size": by_size,
        "by_regime": by_regime,
    }
    summary["gate"] = research_gate(summary)
    summary["size_note"] = _group_note(by_size, "市值")
    summary["regime_note"] = _group_note(by_regime, "市场状态")
    return summary


def _empty_horizon() -> dict[str, Any]:
    return {
        "n": 0,
        "n_price": 0,
        "n_exit_blocked": 0,
        "n_path_gap": 0,
        "mean_trade_ret": None,
        "mean_net_ret": None,
        "median_net_ret": None,
        "win_rate_net": None,
        "mean_mfe": None,
        "mean_mae": None,
        "mean_price_ret": None,
    }


def _tradable_exits(frame: pl.DataFrame, horizon: int) -> pl.DataFrame:
    return frame.filter(
        (pl.col("entry_state") == "tradable")
        & pl.col(f"path_ok_{horizon}")
        & ~pl.col(f"exit_blocked_{horizon}")
        & pl.col(f"trade_ret_{horizon}").is_not_null()
        & pl.col(f"trade_ret_{horizon}").is_finite()
    )


def _horizon_block(frame: pl.DataFrame, horizon: int) -> dict[str, Any]:
    entry = frame.filter(pl.col("entry_state") == "tradable")
    path = entry.filter(pl.col(f"path_ok_{horizon}"))
    blocked = path.filter(pl.col(f"exit_blocked_{horizon}"))
    tradable = _tradable_exits(frame, horizon)
    price = frame.filter(pl.col(f"path_ok_{horizon}") & pl.col(f"price_ret_{horizon}").is_not_null())
    block = _empty_horizon()
    block["n_path_gap"] = int(entry.height - path.height)
    block["n_exit_blocked"] = blocked.height
    block["n_price"] = price.height
    if price.height:
        block["mean_price_ret"] = _num(price.select(pl.col(f"price_ret_{horizon}").mean()).item())
    if tradable.height:
        net = pl.col(f"trade_ret_{horizon}") - ROUND_TRIP_COST
        stats = tradable.select(
            pl.len().alias("n"),
            pl.col(f"trade_ret_{horizon}").mean().alias("mean_trade_ret"),
            net.mean().alias("mean_net_ret"),
            net.median().alias("median_net_ret"),
            (net > 0).mean().alias("win_rate_net"),
            pl.col(f"mfe_{horizon}").mean().alias("mean_mfe"),
            pl.col(f"mae_{horizon}").mean().alias("mean_mae"),
        ).row(0, named=True)
        block["n"] = int(stats["n"])
        for key in ("mean_trade_ret", "mean_net_ret", "median_net_ret", "win_rate_net", "mean_mfe", "mean_mae"):
            block[key] = _num(stats[key])
    return block


def _group_stats(frame: pl.DataFrame, column: str) -> list[dict[str, Any]]:
    if frame.is_empty() or column not in frame.columns:
        return []
    net = pl.col(f"trade_ret_{GATE_HORIZON}") - ROUND_TRIP_COST
    rows = (
        frame.group_by(column)
        .agg(
            pl.len().alias("n"),
            net.mean().alias("mean_net_ret"),
            (net > 0).mean().alias("win_rate_net"),
        )
        .sort(column)
    )
    out: list[dict[str, Any]] = []
    for row in rows.iter_rows(named=True):
        label = row[column]
        out.append({
            "key": "" if label is None else str(label),
            "n": int(row["n"]),
            "mean_net_ret": _num(row["mean_net_ret"]),
            "win_rate_net": _num(row["win_rate_net"]),
        })
    return out


def attach_event_columns(frame: pl.DataFrame, horizons: tuple[int, ...] = HORIZONS) -> pl.DataFrame:
    """用下一交易日开盘做买入，用之后第 h 个交易日收盘做卖出。路径必须落在市场交易日序号上。"""
    if frame.is_empty():
        return frame
    work = frame.sort(["symbol", "date"])
    calendar = work.select("date").unique().sort("date").with_columns(pl.int_range(pl.len()).alias("td"))
    work = work.join(calendar, on="date", how="left")
    work = work.with_columns(pl.int_range(pl.len()).over("symbol").alias("_bar"))
    work = work.with_columns(_bar_flags())
    forward: list[pl.Expr] = []
    for horizon in horizons:
        future_high = [pl.col("high").shift(-step).over("symbol") for step in range(1, horizon + 1)]
        future_low = [pl.col("low").shift(-step).over("symbol") for step in range(1, horizon + 1)]
        forward.extend([
            pl.col("close").shift(-horizon).over("symbol").alias(f"_exit_close_{horizon}"),
            pl.col("td").shift(-horizon).over("symbol").alias(f"_exit_td_{horizon}"),
            pl.col("one_price_down").shift(-horizon).over("symbol").alias(f"_exit_down_{horizon}"),
            pl.max_horizontal(future_high).alias(f"_max_high_{horizon}"),
            pl.min_horizontal(future_low).alias(f"_min_low_{horizon}"),
        ])
    work = work.with_columns(forward)
    work = work.with_columns([
        pl.col("open").shift(-1).over("symbol").alias("_entry_open"),
        pl.col("td").shift(-1).over("symbol").alias("_entry_td"),
        pl.col("suspended").shift(-1).over("symbol").fill_null(False).alias("_entry_suspended"),
        pl.col("one_price_up").shift(-1).over("symbol").fill_null(False).alias("_entry_limit_up"),
    ])
    exprs: list[pl.Expr] = [
        _entry_state_expr().alias("entry_state"),
        pl.col("date").dt.year().cast(pl.Utf8).alias("year"),
    ]
    for horizon in horizons:
        path_ok = pl.col(f"_exit_td_{horizon}").is_not_null() & (pl.col(f"_exit_td_{horizon}") == pl.col("td") + horizon)
        entry_open = pl.col("_entry_open")
        exprs.extend([
            path_ok.alias(f"path_ok_{horizon}"),
            (path_ok & pl.col(f"_exit_down_{horizon}").fill_null(False)).alias(f"exit_blocked_{horizon}"),
            pl.when(path_ok & entry_open.is_not_null() & (entry_open > 0))
            .then(pl.col(f"_exit_close_{horizon}") / entry_open - 1.0)
            .otherwise(None)
            .alias(f"trade_ret_{horizon}"),
            pl.when(path_ok & (pl.col("close") > 0))
            .then(pl.col(f"_exit_close_{horizon}") / pl.col("close") - 1.0)
            .otherwise(None)
            .alias(f"price_ret_{horizon}"),
            pl.when(path_ok & entry_open.is_not_null() & (entry_open > 0))
            .then(pl.col(f"_max_high_{horizon}") / entry_open - 1.0)
            .otherwise(None)
            .alias(f"mfe_{horizon}"),
            pl.when(path_ok & entry_open.is_not_null() & (entry_open > 0))
            .then(pl.col(f"_min_low_{horizon}") / entry_open - 1.0)
            .otherwise(None)
            .alias(f"mae_{horizon}"),
        ])
    work = work.with_columns(exprs)
    drop = [name for name in work.columns if name.startswith("_exit_") or name.startswith("_fwd_") or name.startswith("_max_") or name.startswith("_min_") or name.startswith("_entry_")]
    return work.drop(drop)


def _bar_flags() -> list[pl.Expr]:
    """停牌和一字板口径与 BacktestEngine 的日线撮合一致。"""
    prices = [pl.col(name) for name in ("open", "high", "low", "close")]
    valid = [col.is_not_null() & col.is_finite() & (col > 0) for col in prices]
    any_valid = valid[0]
    for item in valid[1:]:
        any_valid = any_valid | item
    all_valid = valid[0]
    for item in valid[1:]:
        all_valid = all_valid & item
    span = pl.max_horizontal("open", "high", "low", "close") - pl.min_horizontal("open", "high", "low", "close")
    tolerance = pl.max_horizontal(pl.col("close").abs() * 1e-4, pl.lit(0.01))
    same = all_valid & (span <= tolerance)
    suspended = ((~any_valid) | ((pl.col("volume").fill_null(0) <= 0) & same)).fill_null(True)
    limit_up = pl.col("signal_limit_up").fill_null(False)
    return [
        suspended.alias("suspended"),
        (same & ~suspended & limit_up).fill_null(False).alias("one_price_up"),
        (same & ~suspended & pl.col("signal_limit_down").fill_null(False)).fill_null(False).alias("one_price_down"),
    ]


def _entry_state_expr() -> pl.Expr:
    next_session = pl.col("_entry_td").is_not_null() & (pl.col("_entry_td") == pl.col("td") + 1)
    return (
        pl.when(~next_session).then(pl.lit("no_next_bar"))
        .when(pl.col("_entry_suspended")).then(pl.lit("suspended"))
        .when(pl.col("_entry_open").is_null() | ~pl.col("_entry_open").is_finite() | (pl.col("_entry_open") <= 0))
        .then(pl.lit("invalid_price"))
        .when(pl.col("_entry_limit_up")).then(pl.lit("limit_up_locked"))
        .otherwise(pl.lit("tradable"))
    )


def _score_expr(spec: dict[str, Any]) -> pl.Expr:
    parts = [
        (pl.col(part["column"]) / float(part["threshold"])).clip(0.0, 3.0)
        for part in spec["score_parts"]
    ]
    return pl.mean_horizontal(parts)


def _size_bucket_expr() -> pl.Expr:
    present = pl.col("log_float_mv").is_not_null() & pl.col("log_float_mv").is_finite()
    n = present.sum().over("date")
    rank = pl.col("log_float_mv").rank(method="average").over("date")
    pct = (rank - 1.0) / (n - 1.0)
    return (
        pl.when(~present).then(pl.lit("未知"))
        .when(n < 30).then(pl.lit("样本不足"))
        .when(pct <= 1.0 / 3.0).then(pl.lit("小市值"))
        .when(pct <= 2.0 / 3.0).then(pl.lit("中市值"))
        .otherwise(pl.lit("大市值"))
        .alias("size_bucket")
    )


def _regime_expr() -> pl.Expr:
    label = pl.col("state").replace_strict(REGIME_LABEL, default=None)
    return (
        pl.when(pl.col("state").is_null()).then(pl.lit("未知"))
        .when(label.is_null()).then(pl.col("state"))
        .otherwise(label)
        .alias("regime")
    )


def _with_evidence_lags(frame: pl.DataFrame) -> pl.DataFrame:
    """把条件里的日期偏移展开成证据列，方便和触发日的原始值对照。"""
    exprs: list[pl.Expr] = []
    for spec in RADAR_SPECS:
        for cond in spec["conditions"]:
            days = int(cond.get("leftDays") or 0)
            column = cond.get("left")
            if days <= 0 or not isinstance(column, str) or column not in frame.columns:
                continue
            alias = f"{column}_lag{days}"
            if alias not in frame.columns:
                exprs.append(pl.col(column).shift(days).over("symbol").alias(alias))
    return frame.with_columns(exprs) if exprs else frame


def prepare_study_frame(frame: pl.DataFrame, definitions: list[dict[str, Any]]) -> pl.DataFrame:
    """在已经算好因子和涨跌停标记的面板上注入信号、分数和事件列。"""
    work = frame.sort(["symbol", "date"])
    work = _with_evidence_lags(work)
    # 雷达页要能重算已停用的规格；策略是否采用仍看文件里的 enabled。
    compiling = [{**item, "enabled": True} for item in definitions]
    exprs = custom_signals.build_expressions(compiling)
    work = custom_signals.inject(work, exprs)
    missing = [custom_signals.column_name(item["id"]) for item in definitions if custom_signals.column_name(item["id"]) not in work.columns]
    if missing:
        raise RuntimeError(f"信号列没有注入: {missing}")
    score_exprs = []
    for spec in RADAR_SPECS:
        flag = custom_signals.column_name(spec["id"])
        if flag in work.columns:
            score_exprs.append(_score_expr(spec).alias(f"_score_{spec['id']}"))
    if score_exprs:
        work = work.with_columns(score_exprs)
    if "signal_limit_up" not in work.columns:
        work = work.with_columns(pl.lit(False).alias("signal_limit_up"))
    if "signal_limit_down" not in work.columns:
        work = work.with_columns(pl.lit(False).alias("signal_limit_down"))
    work = attach_event_columns(work)
    work = work.with_columns(_size_bucket_expr())
    if "state" not in work.columns:
        work = work.with_columns(pl.lit(None).cast(pl.Utf8).alias("state"))
    return work.with_columns(_regime_expr())


def _candidates(frame: pl.DataFrame, spec: dict[str, Any], as_of: date, names: dict[str, str]) -> list[dict[str, Any]]:
    flag = custom_signals.column_name(spec["id"])
    score_col = f"_score_{spec['id']}"
    hits = frame.filter(
        (pl.col("date") == as_of)
        & pl.col(flag).fill_null(False)
        & (pl.col("_bar") >= int(spec["warmup_bars"]) - 1)
    )
    if hits.is_empty():
        return []
    hits = hits.sort(score_col, descending=True, nulls_last=True).head(CANDIDATE_LIMIT)
    rows: list[dict[str, Any]] = []
    evidence_cols = [item["column"] for item in spec["evidence"]]
    for row in hits.iter_rows(named=True):
        symbol = str(row["symbol"])
        rows.append({
            "symbol": symbol,
            "name": names.get(symbol, ""),
            "date": as_of.isoformat(),
            "score": _num(row.get(score_col), 4),
            "evidence": {column: _num(row.get(column), 4) for column in evidence_cols},
        })
    return rows


def _trigger_slice(frame: pl.DataFrame, spec: dict[str, Any]) -> pl.DataFrame:
    flag = custom_signals.column_name(spec["id"])
    return frame.filter(
        pl.col(flag).fill_null(False)
        & (pl.col("_bar") >= int(spec["warmup_bars"]) - 1)
        & (pl.col("date") >= STUDY_START)
    )


def build_radar_report(data_dir: Path, *, refresh: bool = False, end: date | None = None) -> dict[str, Any]:
    """从行情重算 V1 六条和 V2 两条。同一进程里按信号文件和最新分区缓存。"""
    from app.services.flow_radar_v2 import ensure_v2_signals

    data_dir = Path(data_dir)
    v1_definitions = ensure_radar_signals(data_dir)
    v2_definitions = ensure_v2_signals(data_dir)
    token = _cache_token(data_dir)
    if not refresh:
        with _CACHE_LOCK:
            cached = _CACHE.get(token)
        if cached is not None:
            return cached
    with _CACHE_LOCK:
        if not refresh and token in _CACHE:
            return _CACHE[token]
        report = _compute_report(data_dir, v1_definitions, v2_definitions, end=end)
        _CACHE[token] = report
        return report


def _cache_token(data_dir: Path) -> tuple:
    sig_dir = data_dir / "user_data" / "custom_signals"
    mtimes = tuple(sorted(
        (path.name, path.stat().st_mtime_ns)
        for path in sig_dir.glob("*.json")
    )) if sig_dir.exists() else ()
    enriched = data_dir / "kline_daily_enriched"
    latest = ""
    if enriched.exists():
        parts = sorted(path.name for path in enriched.glob("date=*"))
        latest = parts[-1] if parts else ""
    from app.services.flow_radar_v2 import V2_SPECS

    return (str(data_dir), latest, mtimes, json.dumps(
        {"v1": RADAR_SPECS, "v2": V2_SPECS},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ))


def _compute_report(
    data_dir: Path,
    v1_definitions: list[dict[str, Any]],
    v2_definitions: list[dict[str, Any]],
    *,
    end: date | None,
) -> dict[str, Any]:
    from app.services.flow_radar_v2 import V2_SPECS, overlay_historical_status, summarize_v2

    started = datetime.now()
    frame, as_of, generation, names = _load_market_frame(
        data_dir,
        v1_definitions + v2_definitions,
        end=end,
    )
    signals: list[dict[str, Any]] = []
    for spec, raw in zip(RADAR_SPECS, v1_definitions, strict=True):
        stats = summarize_triggers(_trigger_slice(frame, spec), spec)
        signals.append({
            "id": spec["id"],
            "name": raw.get("name") or spec["name"],
            "kind": raw.get("kind") or spec["kind"],
            "column": custom_signals.column_name(spec["id"]),
            "risk_label": spec["risk_label"],
            "formula": spec["formula"],
            "warmup_bars": spec["warmup_bars"],
            "invalid_when": spec["invalid_when"],
            "evidence": spec["evidence"],
            "conditions": raw.get("conditions") or spec["conditions"],
            "enabled": raw.get("enabled") is not False,
            "definition_matches_spec": _conditions_match(spec, raw),
            "gate": stats["gate"],
            "triggers": stats["triggers"],
            "entry": stats["entry"],
            "horizons": stats["horizons"],
            "by_year": stats["by_year"],
            "by_size": stats["by_size"],
            "by_regime": stats["by_regime"],
            "size_note": stats["size_note"],
            "regime_note": stats["regime_note"],
            "candidates": _candidates(frame, spec, as_of, names) if as_of is not None else [],
        })
    v2_rows = summarize_v2(
        frame,
        cost=ROUND_TRIP_COST,
        study_start=STUDY_START,
        as_of=as_of,
        names=names,
    )
    v2_signals = []
    for spec, raw, row in zip(V2_SPECS, v2_definitions, v2_rows, strict=True):
        v2_signals.append({
            **row,
            "name": raw.get("name") or spec["name"],
            "conditions": raw.get("conditions") or spec["conditions"],
            "definition_matches_spec": _conditions_match(spec, raw),
            "enabled": raw.get("enabled") is not False,
        })
    v2_signals = overlay_historical_status(v2_signals)
    elapsed = (datetime.now() - started).total_seconds()
    return {
        "as_of": as_of.isoformat() if as_of is not None else None,
        "study_start": STUDY_START.isoformat(),
        "study_end": as_of.isoformat() if as_of is not None else None,
        "load_start": LOAD_START.isoformat(),
        "generation": generation,
        "elapsed_s": round(elapsed, 1),
        "persisted_triggers": False,
        "industry": {
            "status": "证据不足/未知",
            "detail": "ext_hy_ths 没有行业分区，instruments 也没有历史行业列，本次不做行业分组。",
        },
        "cost": {
            "commission_pct": 0.0002,
            "stamp_tax_pct": 0.0005,
            "slippage_bps": 5.0,
            "round_trip": ROUND_TRIP_COST,
            "detail": "买入次日开盘、卖出持有期末收盘。往返成本 = 佣金万二双边 + 卖出印花税万五 + 滑点 5bp 双边 = 19bp。",
        },
        "method": [
            "信号是 custom_signals 的 AND 条件，计算用同一套表达式编译。",
            "盘后可见信号日收盘。成交假设为下一交易日开盘买入，第 h 个交易日收盘卖出。",
            "未来 1/5/10/20 日都按这个持有期。价格路径另给收盘到收盘，不把它当成可成交收益。",
            "最大有利波动取持有期内最高价相对买入开盘，最大不利波动取最低价相对买入开盘。",
            "次日没有连续交易日行情，记为不可成交。成交量为 0 且价格不动，按正式回测的停牌口径。一字涨停不买，一字跌停不卖。",
            "涨跌停价用现有 compute_limit_signals。ST 标记来自当前证券名称，历史 ST 区间未知。",
            "市值用信号日 log_float_mv 在全市场横截面分三档。缺失记为未知。",
            "市场状态用信号日 regime_history.state，标签为强、偏强、震荡、偏弱、弱。缺失记为未知。",
            "可解释分数是主条件相对阈值的倍数，每一列截断在 3 后再平均。分数到 3 之后，更极端的样本不再拉开。",
            "V2 主样本是信号日组合。做多门槛含费后中位数。风险候选用同日同市值、未触发且可成交股票的费后收益中位数作对照。官方状态来自已落盘的历史确认；规格哈希一致才从研究中迁出。页面上的门槛数字仍按当前行情重算。",
            "样本来自当前股票 enriched。退市是否完整保留未在本次单独复核。",
        ],
        "signals": signals,
        "v2": v2_signals,
    }


def _load_market_frame(
    data_dir: Path,
    definitions: list[dict[str, Any]],
    *,
    end: date | None,
) -> tuple[pl.DataFrame, date | None, str | None, dict[str, str]]:
    from app.backtest.engine import BacktestEngine
    from app.indicators.pipeline import compute_indicators, compute_limit_signals
    from app.services.regime_builder import load_regime_history
    from app.strategy.scoring import materialize_scoring_columns
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(data_dir))
    engine = BacktestEngine(repo)
    stop = end or date.today()
    columns = [
        "symbol", "date", "open", "high", "low", "close", "volume", "amount",
        "turnover_rate", "raw_close", "raw_high", "raw_low",
    ]
    logger.info("flow radar loading panel %s .. %s", LOAD_START, stop)
    panel = engine.load_panel(None, LOAD_START, stop, columns=columns, asset_type="stock")
    if panel.is_empty():
        raise RuntimeError("enriched 日线为空，雷达无法计算")
    logger.info("flow radar panel rows=%d", panel.height)
    if panel["date"].dtype != pl.Date:
        panel = panel.with_columns(pl.col("date").cast(pl.Date))
    panel = panel.sort(["symbol", "date"])
    panel = compute_indicators(
        panel,
        {"high_60d", "vol_ratio_5d", "amplitude", "change_pct"},
        assume_sorted=True,
    )
    factors = [
        "turnover_z_60d", "amount_ratio_5d", "distance_to_high_60d", "close_position",
        "position_240d", "amplitude_trend_20_60", "vol_trend_5_60", "vol_regime_5_60",
        "log_float_mv",
    ]
    panel = materialize_scoring_columns(panel, factors)
    missing_factors = [name for name in factors if name not in panel.columns]
    if missing_factors:
        raise RuntimeError(f"雷达依赖的因子没有算出来: {missing_factors}")
    instruments = repo.get_instruments()
    if instruments.is_empty():
        panel = panel.with_columns([
            pl.lit(False).alias("signal_limit_up"),
            pl.lit(False).alias("signal_limit_down"),
        ])
        names: dict[str, str] = {}
    else:
        panel = compute_limit_signals(
            panel,
            instruments,
            needed={"signal_limit_up", "signal_limit_down"},
        )
        names = {
            str(row["symbol"]): str(row["name"] or "")
            for row in instruments.select(["symbol", "name"]).iter_rows(named=True)
            if row.get("symbol")
        }
    regime = load_regime_history(data_dir)
    if not regime.is_empty() and {"date", "state"}.issubset(regime.columns):
        if regime["date"].dtype != pl.Date:
            regime = regime.with_columns(pl.col("date").cast(pl.Date))
        panel = panel.join(regime.select(["date", "state"]), on="date", how="left")
    else:
        panel = panel.with_columns(pl.lit(None).cast(pl.Utf8).alias("state"))
    prepared = prepare_study_frame(panel, definitions)
    logger.info("flow radar study frame rows=%d cols=%d", prepared.height, len(prepared.columns))
    as_of = prepared.select(pl.col("date").max()).item()
    generation = None
    try:
        generation = engine.data_generation("stock")
    except Exception:
        generation = None
    keep = {
        "symbol", "date", "close", "state", "log_float_mv", "_bar", "entry_state",
        "year", "size_bucket", "regime",
    }
    for spec in RADAR_SPECS:
        keep.add(custom_signals.column_name(spec["id"]))
        keep.add(f"_score_{spec['id']}")
        for item in spec["evidence"]:
            keep.add(item["column"])
    from app.services.flow_radar_v2 import V2_SPECS

    for spec in V2_SPECS:
        keep.add(custom_signals.column_name(spec["id"]))
        for item in spec["evidence"]:
            keep.add(item["column"])
    for horizon in HORIZONS:
        keep.update({
            f"path_ok_{horizon}", f"exit_blocked_{horizon}", f"trade_ret_{horizon}",
            f"price_ret_{horizon}", f"mfe_{horizon}", f"mae_{horizon}",
        })
    prepared = prepared.select([column for column in prepared.columns if column in keep])
    return prepared, as_of, generation, names


def clear_radar_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
