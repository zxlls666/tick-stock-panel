"""第三方 TA 列接入自定义 SQL screener (#187 同类问题)。

ta_* 列原本只在被自定义信号引用时才由 compute_signals 注入; screener 用 needed=None,
故条件里的 ta_* 列缺失 → DuckDB Binder Error 被吞成静默空结果。本测试证明:
条件中出现 ta_* 时代码自动走带 warmup 的即时计算路径补算该列, 条件真正生效。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.factors import ta_factors
from app.services.screener import ScreenerService

SYM = "600000.SH"


def _require_ta() -> None:
    if not ta_factors.available():
        pytest.skip("polars-talib (可选 extra `ta`) 未安装")


def _write_enriched(tmp_path, days: int = 60) -> date:
    """逐日写单行 enriched 分区 (正弦+漂移价, 保证 ADX 有方向性运动)。"""
    base = tmp_path / "kline_daily_enriched"
    base.mkdir(parents=True, exist_ok=True)
    d = date(2026, 5, 1)
    i = 0
    while i < days:
        if d.weekday() < 5:
            close = 10.0 + math.sin(i / 4.0) * 2.0 + i * 0.02
            high = close * 1.02
            low = close * 0.98
            row = {
                "symbol": SYM, "date": d,
                "open": close * 0.995, "high": high, "low": low, "close": close,
                "volume": 10000.0 + (i % 10) * 500.0, "amount": 1.0e7,
                "raw_close": close, "raw_high": high, "raw_low": low,
                "turnover_rate": 2.0,
                "consecutive_limit_ups": 0, "consecutive_limit_downs": 0,
            }
            part = base / f"date={d.isoformat()}"
            part.mkdir(exist_ok=True)
            pl.DataFrame({k: [v] for k, v in row.items()}).write_parquet(part / "part.parquet")
            i += 1
        d += timedelta(days=1)
    return max(date.fromisoformat(p.name.removeprefix("date=")) for p in base.glob("date=*"))


def _service(tmp_path) -> ScreenerService:
    repo = MagicMock()
    repo.store.data_dir = tmp_path
    # 缓存全部未命中 → 走 scan_parquet + 即时计算; instruments 空 → 无重算
    repo.get_enriched_latest_asset.return_value = (pl.DataFrame(), None)
    repo.get_enriched_history.return_value = None
    repo.get_instruments_asset.return_value = pl.DataFrame()
    repo.get_historical_shares.return_value = pl.DataFrame()
    return ScreenerService(repo, asset_type="stock")


def test_referenced_ta_columns_detection() -> None:
    _require_ta()
    assert ta_factors.referenced_ta_columns("ta_adx_14 > 25 AND close > 10") == {"ta_adx_14"}
    assert ta_factors.referenced_ta_columns("turnover_rate > 3") == frozenset()
    assert ta_factors.referenced_ta_columns("ta_not_a_real_col > 1") == frozenset()


def test_jit_frame_with_ta_columns_has_column(tmp_path) -> None:
    _require_ta()
    target = _write_enriched(tmp_path, 60)
    svc = _service(tmp_path)

    df = svc._load_enriched_for_date(target, ta_columns={"ta_adx_14"})
    assert not df.is_empty()
    assert "ta_adx_14" in df.columns
    assert df["ta_adx_14"].null_count() < df.height, "预热后 ta_adx_14 不应全为 null"


def test_custom_sql_can_filter_on_ta(tmp_path) -> None:
    _require_ta()
    target = _write_enriched(tmp_path, 60)
    svc = _service(tmp_path)

    result = svc.run(target, ["ta_adx_14 > 0"], limit=10)
    # 条件真正生效 (非空结果), 而非 Binder Error 被吞成的静默空
    assert [r["symbol"] for r in result.rows] == [SYM]


def test_ta_unavailable_degrades_without_raising(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ta_factors, "available", lambda: False)
    target = _write_enriched(tmp_path, 60)
    svc = _service(tmp_path)

    # 未装 extra: 识别为空 → 旧路径; ta 列不存在 → 条件无法命中, 但不得抛异常
    assert ta_factors.referenced_ta_columns("ta_adx_14 > 0") == frozenset()
    result = svc.run(target, ["ta_adx_14 > 0"], limit=10)
    assert result.rows == []
