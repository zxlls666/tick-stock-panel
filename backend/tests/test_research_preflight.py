"""research_preflight 的纯谓词与 IO 组装测试 (Phase 11 前置门)。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import polars as pl

from app.backtest.research_preflight import (
    CheckResult,
    PreflightConfig,
    PreflightReport,
    check_calendar_horizon,
    check_enriched_latest,
    check_generation,
    check_instruments_as_of,
    check_market_completeness,
    check_observation_absent,
    check_pit_shares,
    check_source_coverage,
    check_unpublished_bars,
    run_preflight,
)

AS_OF = date(2026, 9, 30)


def test_check_enriched_latest_staleness() -> None:
    dates = [date(2026, 9, 29), date(2026, 9, 30)]
    assert check_enriched_latest(dates, AS_OF).ok
    # 只有旧分区 → 过期
    assert not check_enriched_latest([date(2026, 9, 1)], AS_OF).ok
    assert not check_enriched_latest([], AS_OF).ok


def test_check_market_completeness_ratio() -> None:
    # 收紧到 0.99 (旧 0.95 放过了 2026-10-08 的 1.8% 缺口)
    assert check_market_completeness(5100, 5100).ok
    assert check_market_completeness(5050, 5100).ok
    assert not check_market_completeness(5000, 5100).ok
    assert not check_market_completeness(3000, 5100).ok
    assert check_market_completeness(100, 0).ok  # 无基线跳过


def test_check_unpublished_bars_gate() -> None:
    latest = {"600000.SH", "000001.SZ"}
    # 少量合法缺席 (停牌/退市长期无 bar) → 通过
    assert check_unpublished_bars(latest, latest | {"600002.SH"}, tolerance=30).ok
    # 大面积"近期有成交却缺席" → 拦下
    recent = latest | {f"{600100 + i:06d}.SH" for i in range(100)}
    failed = check_unpublished_bars(latest, recent, tolerance=30)
    assert not failed.ok and "样例" in failed.detail


def test_check_generation_and_snapshot_and_coverage() -> None:
    assert check_generation("g1", "g1").ok
    assert not check_generation("g2", "g1").ok
    assert check_instruments_as_of(AS_OF, AS_OF).ok
    assert not check_instruments_as_of(date(2026, 9, 29), AS_OF).ok
    assert check_source_coverage("st", AS_OF, AS_OF).ok
    assert not check_source_coverage("st", date(2026, 9, 1), AS_OF).ok
    assert not check_source_coverage("st", None, AS_OF).ok


def test_check_pit_shares_calendar_observation(tmp_path) -> None:
    assert check_pit_shares(date(2026, 6, 30), AS_OF).ok
    assert not check_pit_shares(date(2026, 10, 31), AS_OF).ok
    assert not check_pit_shares(None, AS_OF).ok

    assert check_calendar_horizon((date(2026, 9, 30), date(2026, 10, 1)), AS_OF).ok
    assert not check_calendar_horizon((date(2026, 9, 30),), AS_OF).ok
    assert not check_calendar_horizon((), AS_OF).ok

    target = tmp_path / "as_of=2026-09-30"
    assert check_observation_absent(target).ok
    target.mkdir()
    assert not check_observation_absent(target).ok


def test_report_aggregation() -> None:
    ok = PreflightReport(as_of=AS_OF, checks=(CheckResult("a", True, ""), CheckResult("b", True, "")))
    bad = PreflightReport(as_of=AS_OF, checks=(CheckResult("a", True, ""), CheckResult("b", False, "x")))
    assert ok.ok and not ok.failures
    assert not bad.ok and [c.name for c in bad.failures] == ["b"]
    assert bad.as_dict()["ok"] is False


def _write_env(tmp_path, *, generation: str = "g1") -> PreflightConfig:
    data_dir = tmp_path / "data"
    for offset in (1, 0):
        part = data_dir / "kline_daily_enriched" / f"date={(AS_OF - timedelta(days=offset)).isoformat()}"
        part.mkdir(parents=True)
        pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"]}).write_parquet(part / "part.parquet")
    (data_dir / ".matrix_generation_stock.json").write_text(
        json.dumps({"generation": generation}), encoding="utf-8"
    )
    instruments = tmp_path / "instruments.parquet"
    pl.DataFrame({"symbol": ["600000.SH"], "as_of": [AS_OF]}).write_parquet(instruments)
    shares = tmp_path / "shares.parquet"
    pl.DataFrame({"symbol": ["600000.SH"], "period_end": [date(2026, 6, 30)]}).write_parquet(shares)
    st_manifest = tmp_path / "st_manifest.json"
    st_manifest.write_text(json.dumps({"end": AS_OF.isoformat()}), encoding="utf-8")
    delisted = tmp_path / "delisted_manifest.json"
    delisted.write_text(json.dumps({"end": AS_OF.isoformat()}), encoding="utf-8")
    calendar = tmp_path / "calendar.txt"
    calendar.write_text("2026-09-29\n2026-09-30\n2026-10-01\n", encoding="utf-8")
    return PreflightConfig(
        as_of=AS_OF,
        expected_generation="g1",
        data_dir=data_dir,
        output_root=tmp_path / "out",
        trading_calendar=calendar,
        instruments=instruments,
        shares=shares,
        st_manifest=st_manifest,
        delisted_coverage_manifest=delisted,
    )


def test_run_preflight_passes_on_aligned_inputs(tmp_path) -> None:
    report = run_preflight(_write_env(tmp_path))
    assert report.ok, [c.detail for c in report.failures]
    assert {c.name for c in report.checks} == {
        "enriched_latest", "market_completeness", "unpublished_bars", "generation",
        "instruments_as_of", "st_coverage", "delisted_coverage", "pit_shares",
        "calendar_horizon", "observation_absent",
    }


def test_run_preflight_fails_on_latest_partition_dip(tmp_path) -> None:
    """最新分区比前一交易日少 4% → 0.99 门拒绝 (旧 0.95 中位数门会放行)。"""
    config = _write_env(tmp_path)
    base = config.data_dir / "kline_daily_enriched"
    symbols = [f"{600000 + i}.SH" for i in range(100)]
    pl.DataFrame({"symbol": symbols}).write_parquet(
        base / f"date={(AS_OF - timedelta(days=1)).isoformat()}" / "part.parquet"
    )
    pl.DataFrame({"symbol": symbols[:96]}).write_parquet(
        base / f"date={AS_OF.isoformat()}" / "part.parquet"
    )

    report = run_preflight(config)

    assert not report.ok
    assert "market_completeness" in {c.name for c in report.failures}


def test_run_preflight_fails_on_generation_drift(tmp_path) -> None:
    config = _write_env(tmp_path, generation="g2")
    report = run_preflight(config)
    assert not report.ok
    assert [c.name for c in report.failures] == ["generation"]


def test_run_preflight_fails_when_observation_exists(tmp_path) -> None:
    config = _write_env(tmp_path)
    (config.output_root / f"as_of={AS_OF.isoformat()}").mkdir(parents=True)
    report = run_preflight(config)
    assert not report.ok
    assert "observation_absent" in {c.name for c in report.failures}
