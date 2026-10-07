"""图表指标参数化 (opt-in)。

用户可在日 K 图上改指标周期 (MA/EMA/RSI/MACD/BOLL/KDJ)。为避免改写规范列语义
(``ma20``、``macd_dif`` 等被 screener / 自定义信号 / 回测共同引用), 自定义参数一律产出
**别名列** ``<kind>_<params>_c`` / ``<kind>_<params>_<output>_c``, 绝不覆盖
``ENRICHED_COLUMNS`` 中的规范列。默认参数产出的别名列与规范列数值逐位一致
(``pipeline.compute_indicators`` 是唯一权威口径)。

请求编码 (GET query, 逗号分隔条目, 冒号分隔参数)::

    ma:5:13          -> ma_5_c, ma_13_c
    ema:10           -> ema_10_c
    rsi:7            -> rsi_7_c
    macd:8:17:9      -> macd_8_17_9_dif_c / _dea_c / _hist_c
    boll:20:2        -> boll_20_2_upper_c / _lower_c
    kdj:9            -> kdj_9_k_c / _d_c / _j_c

非法 kind 或参数越界 → ``ValueError`` (调用方 fail-closed, 返回 400), 不静默截断。
"""
from __future__ import annotations

from typing import NamedTuple

import polars as pl

# 周期上限。读取路径的预热窗口会按 max_period 自适应加宽, 故上限只约束合理性。
MAX_PERIOD = 500

# 每类指标的参数个数约束 (min, max); macd/boll/kdj 要求定长, 单独校验。
_KINDS = frozenset({"ma", "ema", "rsi", "macd", "boll", "kdj"})


class IndicatorSpec(NamedTuple):
    """一个自定义指标变体: 类型 + 参数元组。"""

    kind: str
    params: tuple[int, ...]


def _ema_alpha(span: int) -> float:
    return 2.0 / (span + 1)


def _check_period(p: int, raw: str) -> None:
    if not (2 <= p <= MAX_PERIOD):
        raise ValueError(f"indicator period out of range [2,{MAX_PERIOD}]: {raw!r}")


def _validate(kind: str, params: tuple[int, ...], raw: str) -> None:
    if kind in ("ma", "ema"):
        if not (1 <= len(params) <= 6):
            raise ValueError(f"{kind} expects 1..6 periods: {raw!r}")
        for p in params:
            _check_period(p, raw)
    elif kind == "rsi":
        if not (1 <= len(params) <= 4):
            raise ValueError(f"rsi expects 1..4 periods: {raw!r}")
        for p in params:
            _check_period(p, raw)
    elif kind == "macd":
        if len(params) != 3:
            raise ValueError(f"macd expects exactly 3 params (fast:slow:signal): {raw!r}")
        fast, slow, _signal = params
        for p in params:
            _check_period(p, raw)
        if fast >= slow:
            raise ValueError(f"macd fast must be < slow: {raw!r}")
    elif kind == "boll":
        if len(params) != 2:
            raise ValueError(f"boll expects exactly 2 params (period:std): {raw!r}")
        period, std = params
        _check_period(period, raw)
        if not (1 <= std <= 10):
            raise ValueError(f"boll std out of range [1,10]: {raw!r}")
    elif kind == "kdj":
        if len(params) != 1:
            raise ValueError(f"kdj expects exactly 1 param (n): {raw!r}")
        _check_period(params[0], raw)


def parse_indicator_specs(raw: str | None) -> list[IndicatorSpec]:
    """解析 "ma:5:13,macd:8:17:9" → specs; 空串/None → []。非法项抛 ValueError。"""
    if not raw or not raw.strip():
        return []
    specs: list[IndicatorSpec] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(":")]
        kind = parts[0].lower()
        if kind not in _KINDS:
            raise ValueError(f"unknown indicator kind: {entry!r}")
        try:
            params = tuple(int(p) for p in parts[1:])
        except ValueError as exc:
            raise ValueError(f"indicator params must be integers: {entry!r}") from exc
        if not params:
            raise ValueError(f"indicator requires at least one param: {entry!r}")
        _validate(kind, params, entry)
        key = (kind, params)
        if key not in seen:
            seen.add(key)
            specs.append(IndicatorSpec(kind, params))
    return specs


def max_period(specs: list[IndicatorSpec]) -> int:
    """所有 spec 中最大周期, 供读取路径自适应加宽预热窗口。空 → 0。"""
    return max((p for s in specs for p in s.params), default=0)


def _alias(kind: str, params: tuple[int, ...], output: str | None = None) -> str:
    suffix = f"_{output}" if output else ""
    return f"{kind}_{'_'.join(str(p) for p in params)}{suffix}_c"


def compute_custom_indicators(df: pl.DataFrame, specs: list[IndicatorSpec]) -> pl.DataFrame:
    """按 specs 追加自定义周期指标的别名列 (opt-in)。空 specs 或空帧 → 原样返回。"""
    if df.is_empty() or not specs:
        return df
    df = df.sort(["symbol", "date"])
    for spec in specs:
        df = _apply_spec(df, spec)
    return df


def _apply_spec(df: pl.DataFrame, spec: IndicatorSpec) -> pl.DataFrame:
    kind, params = spec.kind, spec.params

    if kind == "ma":
        return df.with_columns([
            pl.col("close").rolling_mean(p).over("symbol").alias(_alias(kind, (p,)))
            for p in params
        ])

    if kind == "ema":
        return df.with_columns([
            pl.col("close").ewm_mean(alpha=_ema_alpha(p), adjust=False).over("symbol").alias(_alias(kind, (p,)))
            for p in params
        ])

    if kind == "rsi":
        df = df.with_columns(
            pl.col("close").diff().over("symbol").alias("_ci_delta"),
        ).with_columns([
            pl.when(pl.col("_ci_delta") > 0).then(pl.col("_ci_delta")).otherwise(0.0).alias("_ci_gain"),
            pl.when(pl.col("_ci_delta") < 0).then(-pl.col("_ci_delta")).otherwise(0.0).alias("_ci_loss"),
        ])
        for p in params:
            a = 1.0 / p
            ag, al = f"_ci_ag_{p}", f"_ci_al_{p}"
            df = df.with_columns([
                pl.col("_ci_gain").ewm_mean(alpha=a, adjust=False).over("symbol").alias(ag),
                pl.col("_ci_loss").ewm_mean(alpha=a, adjust=False).over("symbol").alias(al),
            ]).with_columns(
                (100 - 100 / (1 + pl.col(ag)
                              / pl.when(pl.col(al) == 0).then(1e-12).otherwise(pl.col(al)))
                 ).alias(_alias(kind, (p,))),
            )
        return df.drop([c for c in df.columns if c.startswith("_ci_")])

    if kind == "macd":
        fast, slow, signal = params
        df = df.with_columns([
            pl.col("close").ewm_mean(alpha=_ema_alpha(fast), adjust=False).over("symbol").alias("_ci_ef"),
            pl.col("close").ewm_mean(alpha=_ema_alpha(slow), adjust=False).over("symbol").alias("_ci_es"),
        ]).with_columns(
            (pl.col("_ci_ef") - pl.col("_ci_es")).alias("_ci_dif"),
        ).with_columns(
            pl.col("_ci_dif").ewm_mean(alpha=_ema_alpha(signal), adjust=False).over("symbol").alias("_ci_dea"),
        ).with_columns([
            pl.col("_ci_dif").alias(_alias(kind, params, "dif")),
            pl.col("_ci_dea").alias(_alias(kind, params, "dea")),
            ((pl.col("_ci_dif") - pl.col("_ci_dea")) * 2).alias(_alias(kind, params, "hist")),
        ])
        return df.drop([c for c in ("_ci_ef", "_ci_es", "_ci_dif", "_ci_dea") if c in df.columns])

    if kind == "boll":
        period, std = params
        mult = float(std)
        return df.with_columns([
            (pl.col("close").rolling_mean(period).over("symbol")
             + mult * pl.col("close").rolling_std(period).over("symbol")).alias(_alias(kind, params, "upper")),
            (pl.col("close").rolling_mean(period).over("symbol")
             - mult * pl.col("close").rolling_std(period).over("symbol")).alias(_alias(kind, params, "lower")),
        ])

    if kind == "kdj":
        (n,) = params
        df = df.with_columns([
            pl.col("low").rolling_min(n).over("symbol").alias("_ci_ln"),
            pl.col("high").rolling_max(n).over("symbol").alias("_ci_hn"),
        ]).with_columns(
            (100 * (pl.col("close") - pl.col("_ci_ln"))
             / (pl.col("_ci_hn") - pl.col("_ci_ln")).fill_null(1e-12)).alias("_ci_rsv"),
        ).with_columns(
            pl.col("_ci_rsv").ewm_mean(alpha=1.0 / 3, adjust=False).over("symbol").alias("_ci_k"),
        ).with_columns(
            pl.col("_ci_k").ewm_mean(alpha=1.0 / 3, adjust=False).over("symbol").alias("_ci_d"),
        ).with_columns([
            pl.col("_ci_k").alias(_alias(kind, params, "k")),
            pl.col("_ci_d").alias(_alias(kind, params, "d")),
            (3 * pl.col("_ci_k") - 2 * pl.col("_ci_d")).alias(_alias(kind, params, "j")),
        ])
        return df.drop([c for c in ("_ci_ln", "_ci_hn", "_ci_rsv", "_ci_k", "_ci_d") if c in df.columns])

    raise ValueError(f"unsupported indicator kind: {kind!r}")  # pragma: no cover - parse 已拦截
