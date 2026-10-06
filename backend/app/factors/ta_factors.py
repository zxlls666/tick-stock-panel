"""第三方 TA 指标适配层 (polars-talib) — 补缺指标 + parity 对照。

依赖为可选 extra `ta` (`uv sync --extra ta`)。未安装 (或插件 ABI 加载失败) 时本模块
全量 no-op, 主流程与既有因子不受影响 (对齐 CONTRIBUTING §2.1 的 vectorbt 模式)。

两个消费方:

- 因子体系: 新指标以 kind="base" 因子注册 (id 即列名), 经 registry.all_factors() 的
  惰性同步出现在因子库列表、信号条件字段白名单、评分/回测物化与 AI 提示词中;
  指标列在帧组装时由 attach_ta_columns 注入 (与 ext_factors 同一原语)。
- 对照基线: parity_report 用第三方实现校验内置指标 (RSI/MACD/ATR/BOLL/MA/EMA...),
  作为重构与第三方库升级的回归基线。

口径与边界 (金融契约, 见 CONTRIBUTING §3):

- 输入为 enriched 的前复权 open/high/low/close/volume, 与内置指标同一价格口径;
- TA-Lib 预热期返回 NaN, attach 时统一归一为 null (评分/IC 期望 null, 不容忍 NaN);
- 多输出指标 (stoch/aroon/supertrend/ha) 的 struct 字段摊平为独立标量列;
- 第三方列只作新增独立列 + 对照, 不回灌内置因子列 —— 内置 pipeline 仍是既定因子的
  唯一权威, 避免"双重真相";

启用边界: 历史帧 (compute_signals) 与回测补算路径 (_compute_missing_factors) 接入
attach_ta_columns; 当日单日增量帧 (compute_enriched_today) 每个 symbol 仅一根K线,
无预热窗口, TA 指标无从计算 —— 故不在该热路径接入 (不产出全 null 列、不引入每秒级开销)。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import polars as pl

from app.factors.registry import FactorSpec, get_factor, register_factor

logger = logging.getLogger(__name__)

TA_PREFIX = "ta_"
TA_GROUP = "第三方TA"

try:  # 可选依赖: 编译型 polars 插件, 缺失或 ABI 不兼容时整体降级
    import polars_talib as _plta

    _AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失/加载失败的降级分支
    _plta = None
    _AVAILABLE = False


def available() -> bool:
    """polars-talib 是否可用 (安装且能加载)。所有对外函数据此降级。"""
    return _AVAILABLE


@dataclass(frozen=True)
class TaIndicator:
    """一条第三方指标列声明 (静态表, 非配置驱动)。"""

    column: str  # 因子 id 与帧列名 (ta_ 前缀)
    label: str
    desc: str
    source: str  # _sources() 的键, 决定计算表达式
    field: str | None  # 多输出 struct 字段; None = 单标量
    warmup_bars: int
    scale_free: bool


_INDICATORS: tuple[TaIndicator, ...] = (
    TaIndicator("ta_adx_14", "ADX(14)", "14日平均趋向指数 (0~100)", "adx", None, 28, True),
    TaIndicator("ta_cci_14", "CCI(14)", "14日顺势指标", "cci", None, 14, True),
    TaIndicator("ta_willr_14", "Williams %R(14)", "14日威廉指标 (-100~0)", "willr", None, 14, True),
    TaIndicator(
        "ta_obv", "OBV量能潮(经典)", "经典能量潮: 涨日累加成交量、跌日累减 (非归一化)",
        "obv", None, 1, False,
    ),
    TaIndicator("ta_stoch_k", "Stoch K(14,3,3)", "随机指标慢线K (0~100)", "stoch", "slowk", 20, True),
    TaIndicator("ta_stoch_d", "Stoch D(14,3,3)", "随机指标慢线D (0~100)", "stoch", "slowd", 20, True),
    TaIndicator("ta_aroon_up", "Aroon Up 14", "14日Aroon上行 (0~100)", "aroon", "aroonup", 14, True),
    TaIndicator("ta_aroon_down", "Aroon Down 14", "14日Aroon下行 (0~100)", "aroon", "aroondown", 14, True),
    TaIndicator(
        "ta_supertrend", "Supertrend 10/3", "ATR通道趋势线 (价格尺度)",
        "supertrend", "supertrend", 12, False,
    ),
    TaIndicator("ta_hma_20", "HMA(20)", "20日Hull均线 (价格尺度)", "hma", None, 20, False),
    TaIndicator("ta_ha_open", "Heikin-Ashi开盘", "平均K线开盘 (价格尺度)", "ha", "haopen", 1, False),
    TaIndicator("ta_ha_high", "Heikin-Ashi最高", "平均K线最高 (价格尺度)", "ha", "hahigh", 1, False),
    TaIndicator("ta_ha_low", "Heikin-Ashi最低", "平均K线最低 (价格尺度)", "ha", "halow", 1, False),
    TaIndicator("ta_ha_close", "Heikin-Ashi收盘", "平均K线收盘 (价格尺度)", "ha", "haclose", 1, False),
)

_BY_COLUMN: dict[str, TaIndicator] = {ind.column: ind for ind in _INDICATORS}


def _sources() -> dict[str, Callable[[], pl.Expr]]:
    """指标源表达式工厂。惰性引用 _plta, 模块导入期不触碰第三方库。"""
    return {
        "adx": lambda: _plta.adx(pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=14),
        "cci": lambda: _plta.cci(pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=14),
        "willr": lambda: _plta.willr(pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=14),
        "obv": lambda: _plta.obv(pl.col("close"), pl.col("volume")),
        "stoch": lambda: _plta.stoch(
            pl.col("high"),
            pl.col("low"),
            pl.col("close"),
            fastk_period=14,
            slowk_period=3,
            slowd_period=3,
        ),
        "aroon": lambda: _plta.aroon(pl.col("high"), pl.col("low"), timeperiod=14),
        "supertrend": lambda: _plta.supertrend(
            pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=10, multiplier=3.0,
        ),
        "hma": lambda: _plta.hma(pl.col("close"), timeperiod=20),
        "ha": lambda: _plta.ha(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close")),
    }


def ta_factor_specs() -> list[FactorSpec]:
    """TA 指标列的 base 因子条目 (列自身即值, 空依赖); 不可用时为空。"""
    if not available():
        return []
    return [
        FactorSpec(
            id=ind.column,
            label=ind.label,
            group=TA_GROUP,
            formula_text=ind.desc,
            kind="base",
            warmup_bars=ind.warmup_bars,
            scale_free=ind.scale_free,
            tags=("ta",),
        )
        for ind in _INDICATORS
    ]


def ta_factor_ids() -> frozenset[str]:
    """TA 因子 id 集合 (纯函数, 不依赖注册状态; 供 worker 子进程拦截判断)。"""
    return frozenset(_BY_COLUMN) if available() else frozenset()


def referenced_ta_columns(sql_text: str) -> frozenset[str]:
    """从 SQL 片段中识别引用的 TA 列 (词边界匹配 ta_ 前缀标识符 ∩ 已注册 id)。

    screener 的自定义 SQL 条件是原始字符串, 用它决定是否需要在取数阶段补算 TA 列。
    不可用 (未装 extra) 或未引用时返回空集 —— 调用方保持原行为不变。
    """
    ids = ta_factor_ids()
    if not ids or not sql_text:
        return frozenset()
    return frozenset(re.findall(r"\bta_[A-Za-z0-9_]+\b", sql_text)) & ids


_registered = False


def ensure_synced() -> None:
    """把 TA 因子同步进注册表 (幂等, 静态表仅注册一次)。

    与 ext_factors.ensure_synced 的差别: 表是静态的, 无需配置目录签名; 不可用时
    不注册 (也不注销既有条目, 保持进程内稳定)。已存在的 id 跳过, 不触发注册表的
    版本 fail-closed 拒绝。
    """
    global _registered
    if not available() or _registered:
        return
    for spec in ta_factor_specs():
        if get_factor(spec.id) is None:
            register_factor(spec)
    _registered = True


def attach_ta_columns(df: pl.DataFrame, *, needed: set[str] | None) -> pl.DataFrame:
    """把 needed ∩ TA 因子 的列算到帧上 (未安装/无需求/列已存在时原样返回)。

    needed 为显式 opt-in: None 或空集不计算, 保护当日增量热路径。计算前按
    (symbol, date) 排序 (插件 .over("symbol") 要求组内有序); 单指标源失败只
    告警跳过该列 (fail-soft, 对齐 ext_factors 的单配置隔离)。
    """
    if not available() or df.is_empty() or not needed:
        return df
    wanted = [
        _BY_COLUMN[name]
        for name in needed
        if name in _BY_COLUMN and name not in df.columns
    ]
    if not wanted:
        return df
    if "symbol" in df.columns and "date" in df.columns:
        df = df.sort(["symbol", "date"])

    sources = _sources()
    by_source: dict[str, list[TaIndicator]] = {}
    for ind in wanted:
        by_source.setdefault(ind.source, []).append(ind)

    for source, inds in by_source.items():
        try:
            base = sources[source]()
            if "symbol" in df.columns:
                base = base.over("symbol")
            exprs = []
            for ind in inds:
                scalar = base.struct.field(ind.field) if ind.field else base
                scalar = scalar.cast(pl.Float64)
                exprs.append(
                    pl.when(scalar.is_nan() | scalar.is_infinite())
                    .then(None)
                    .otherwise(scalar)
                    .alias(ind.column)
                )
            df = df.with_columns(exprs)
        except Exception as e:
            logger.warning("TA 指标源 %s 计算失败, 跳过: %s", source, e)
    return df


# 内置指标与其第三方对照的实现 (parity)。expected_mismatch 标记口径已知不同者。
def _parity_peers() -> dict[str, tuple[Callable[[], pl.Expr], bool]]:
    peers: dict[str, tuple[Callable[[], pl.Expr], bool]] = {}
    for period in (6, 14, 24):
        peers[f"rsi_{period}"] = (
            lambda period=period: _plta.rsi(pl.col("close"), timeperiod=period),
            False,
        )
    for period in (5, 10, 20, 60):
        peers[f"ma{period}"] = (
            lambda period=period: _plta.sma(pl.col("close"), timeperiod=period),
            False,
        )
        peers[f"ema{period}"] = (
            lambda period=period: _plta.ema(pl.col("close"), timeperiod=period),
            False,
        )
    peers["atr_14"] = (
        lambda: _plta.atr(pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=14),
        False,
    )
    peers["macd_dif"] = (
        lambda: _plta.macd(pl.col("close"), 12, 26, 9).struct.field("macd"),
        False,
    )
    peers["macd_dea"] = (
        lambda: _plta.macd(pl.col("close"), 12, 26, 9).struct.field("macdsignal"),
        False,
    )
    peers["macd_hist"] = (
        lambda: _plta.macd(pl.col("close"), 12, 26, 9).struct.field("macdhist"),
        False,
    )
    peers["boll_upper"] = (
        lambda: _plta.bbands(pl.col("close"), 20, 2.0, 2.0).struct.field("upperband"),
        False,
    )
    peers["boll_lower"] = (
        lambda: _plta.bbands(pl.col("close"), 20, 2.0, 2.0).struct.field("lowerband"),
        False,
    )
    # KDJ: 内置为通达信 RSV/EWM alpha=1/3 口径, 与 TA-Lib KDJ 默认 matype 不同 → 预期不匹配。
    for field in ("k", "d", "j"):
        peers[f"kdj_{field}"] = (
            lambda field=field: _plta.kdj(
                pl.col("high"), pl.col("low"), pl.col("close"),
            ).struct.field(field),
            True,
        )
    return peers


def parity_report(df: pl.DataFrame, names: Iterable[str]) -> dict[str, dict]:
    """内置指标列 vs 第三方实现的逐列对照 (测试与库升级回归用, 不进热路径)。

    输入 df 需已含内置指标列 (如 compute_indicators 的产出)。返回
    {name: {covered, max_abs_diff, mean_abs_diff, expected_mismatch, note?}},
    只统计双方都为有限值的样本。
    """
    result: dict[str, dict] = {}
    if not available() or df.is_empty():
        return result
    peers = _parity_peers()
    if "symbol" in df.columns and "date" in df.columns:
        df = df.sort(["symbol", "date"])
    for name in names:
        peer = peers.get(name)
        if peer is None:
            continue
        make, expected_mismatch = peer
        if name not in df.columns:
            result[name] = {
                "covered": 0, "max_abs_diff": None, "mean_abs_diff": None,
                "expected_mismatch": expected_mismatch, "note": "内置列缺失",
            }
            continue
        try:
            expr = make()
            if "symbol" in df.columns:
                expr = expr.over("symbol")
            lib = df.select(expr.cast(pl.Float64).alias("_lib"))["_lib"].cast(pl.Float64)
            internal = df[name].cast(pl.Float64)
            mask = internal.is_not_null() & internal.is_finite() & lib.is_not_null() & lib.is_finite()
            covered = int(mask.sum())
            if covered == 0:
                result[name] = {
                    "covered": 0, "max_abs_diff": None, "mean_abs_diff": None,
                    "expected_mismatch": expected_mismatch, "note": "无共同有效样本",
                }
                continue
            diff = (internal - lib).filter(mask).abs()
            result[name] = {
                "covered": covered,
                "max_abs_diff": float(diff.max()),
                "mean_abs_diff": float(diff.mean()),
                "expected_mismatch": expected_mismatch,
            }
        except Exception as e:
            result[name] = {
                "covered": 0, "max_abs_diff": None, "mean_abs_diff": None,
                "expected_mismatch": expected_mismatch, "note": f"对照失败: {e}",
            }
    return result
