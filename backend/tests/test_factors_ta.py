"""第三方 TA 指标适配层 (polars-talib, 可选 extra `ta`) 测试。

数值/集成用例在依赖不可用时跳过; 降级用例始终运行, 保证未安装 extra 的主流程不受影响。
"""
from __future__ import annotations

import datetime as dt
import math

import polars as pl
import pytest

from app.factors import ta_factors
from app.factors.registry import _REGISTRY, all_factors
from app.indicators import pipeline


def _frame(n: int = 140, symbols: tuple[str, ...] = ("A", "B")) -> pl.DataFrame:
    rows = []
    d0 = dt.date(2025, 1, 1)
    for si, sym in enumerate(symbols):
        for i in range(n):
            c = 100.0 + 10.0 * math.sin((i + si * 3) / 7.0) + i * 0.05 + (i % 3)
            rows.append({
                "symbol": sym, "date": d0 + dt.timedelta(days=i),
                "open": c - 0.2, "high": c + 1.5, "low": c - 1.5, "close": c,
                "volume": 1000.0 + 50.0 * (i % 10),
                "amount": c * (1000.0 + 50.0 * (i % 10)) * 100.0,
                "raw_close": c, "turnover_rate": 1.0 + (i % 5) * 0.1,
            })
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


def _require_ta() -> None:
    if not ta_factors.available():
        pytest.skip("polars-talib (可选 extra `ta`) 未安装")


@pytest.fixture(autouse=True)
def _cleanup_ta_registry():
    """每个用例前后清掉注册表里的 ta_ 条目, 不污染其他测试文件。"""

    def _purge() -> None:
        for fid in [k for k in list(_REGISTRY) if k.startswith(ta_factors.TA_PREFIX)]:
            _REGISTRY.pop(fid, None)
        ta_factors._registered = False

    _purge()
    yield
    _purge()


# ── 降级: 不依赖真实安装 ────────────────────────────────

def test_unavailable_degrades_to_noop(monkeypatch):
    monkeypatch.setattr(ta_factors, "available", lambda: False)
    ta_factors._registered = False

    assert ta_factors.ta_factor_specs() == []
    assert ta_factors.ta_factor_ids() == frozenset()

    frame = _frame(6, symbols=("A",))
    assert ta_factors.attach_ta_columns(frame, needed={"ta_adx_14"}) is frame
    assert ta_factors.parity_report(frame, ["rsi_14"]) == {}

    ta_factors.ensure_synced()
    assert ta_factors._registered is False
    assert not any(s.id.startswith(ta_factors.TA_PREFIX) for s in all_factors())


# ── 数值 ────────────────────────────────────────────────

def test_attach_ta_columns_values_and_nan_normalized():
    _require_ta()
    frame = _frame(140, symbols=("A", "B"))
    out = ta_factors.attach_ta_columns(frame, needed=set(ta_factors.ta_factor_ids()))

    for col in ta_factors.ta_factor_ids():
        assert col in out.columns, col
        series = out[col]
        # TA-Lib 预热期的 NaN 必须归一为 null (评分/IC 口径)
        assert not series.is_nan().fill_null(False).any(), col

    adx = out["ta_adx_14"].drop_nulls()
    assert adx.len() > 0 and float(adx.min()) >= 0.0 and float(adx.max()) <= 100.0

    willr = out["ta_willr_14"].drop_nulls()
    assert float(willr.min()) >= -100.0001 and float(willr.max()) <= 0.0001

    stoch = out["ta_stoch_k"].drop_nulls()
    assert float(stoch.min()) >= 0.0 and float(stoch.max()) <= 100.0

    aroon = out["ta_aroon_up"].drop_nulls()
    assert float(aroon.min()) >= 0.0 and float(aroon.max()) <= 100.0

    # 预热期不足 → null (首行 per symbol 排序后)
    assert out["ta_adx_14"].head(3).null_count() == 3


def test_attach_is_noop_without_needed():
    _require_ta()
    frame = _frame(40)
    assert ta_factors.attach_ta_columns(frame, needed=None) is frame
    assert ta_factors.attach_ta_columns(frame, needed=set()) is frame
    assert ta_factors.attach_ta_columns(frame, needed={"ma20"}) is frame  # 非 TA id


# ── 注册与集成 ──────────────────────────────────────────

def test_registry_includes_ta_factors():
    _require_ta()
    ids = ta_factors.ta_factor_ids()
    assert len(ids) == 14
    assert {"ta_adx_14", "ta_obv", "ta_ha_close"} <= ids

    specs = {s.id: s for s in all_factors()}
    assert "ta_adx_14" in specs
    assert specs["ta_adx_14"].kind == "base"
    assert specs["ta_adx_14"].group == ta_factors.TA_GROUP
    assert specs["ta_adx_14"].dependencies == frozenset()


def test_allowed_fields_include_ta():
    _require_ta()
    from app.strategy import custom_signals

    assert "ta_adx_14" in custom_signals.allowed_fields()


def test_compute_signals_injects_ta_column():
    _require_ta()
    indicators = pipeline.compute_indicators(_frame(140))
    out = pipeline.compute_signals(indicators, needed={"ta_adx_14"})
    assert "ta_adx_14" in out.columns
    assert out["ta_adx_14"].drop_nulls().len() > 0


def test_backtest_missing_factors_injects_ta():
    _require_ta()
    from app.backtest.factor import FactorBacktestService

    out = FactorBacktestService._compute_missing_factors(_frame(140), {"ta_cci_14"})
    assert "ta_cci_14" in out.columns
    assert out["ta_cci_14"].drop_nulls().len() > 0


# ── parity 对照 ─────────────────────────────────────────

def test_parity_report_ma_exact_kdj_expected_mismatch():
    _require_ta()
    indicators = pipeline.compute_indicators(_frame(200))
    names = ["ma5", "ma10", "ma20", "ma60", "ema20", "rsi_14", "boll_upper", "kdj_k", "kdj_d", "kdj_j"]
    report = ta_factors.parity_report(indicators, names)

    for name in names:
        assert report[name]["covered"] > 0, name

    # MA 与 TA-Lib 同口径 → 精确匹配
    for name in ("ma5", "ma10", "ma20", "ma60"):
        assert report[name]["max_abs_diff"] < 1e-9, (name, report[name])

    # KDJ 口径不同 (内置为通达信 RSV/EWM) → 标记预期不匹配
    for name in ("kdj_k", "kdj_d", "kdj_j"):
        assert report[name]["expected_mismatch"] is True

    # EMA/RSI/BOLL 与内置实现存在口径差异 (EMA 种子/BOLL 自由度), 不是可直接替换 ——
    # 记录该事实, 防止未来误把第三方值等同内置值 (双重真相)。
    for name in ("ema20", "rsi_14", "boll_upper"):
        assert report[name]["max_abs_diff"] > 1e-3, (name, report[name])


def test_parity_report_missing_internal_column():
    _require_ta()
    frame = _frame(60)
    report = ta_factors.parity_report(frame, ["macd_dif"])
    assert report["macd_dif"]["covered"] == 0
    assert report["macd_dif"]["note"] == "内置列缺失"
