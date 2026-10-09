"""Research OOS preflight gates (Phase 11).

纯校验模块: 在运行重点 OOS observer 之前, fail-closed 地确认上游数据已对齐。
分成两层:

- 纯谓词 (check_*): 接收已加载的基础值, 返回 CheckResult —— 可单测, 无 IO 副作用。
- run_preflight(config): 读取文件/分区后调用谓词, 汇总为 PreflightReport。

校验项 (任一不过即不得运行 observer):
- enriched 覆盖到 as-of 之前最后一个交易日且未过期 (staleness)
- 最新分区 symbol 数不低于近 20 日中位数 * ratio
- 数据 generation 与预期一致 (漂移即拒)
- instruments 快照 as_of == as-of
- ST history manifest 覆盖 ≥ as-of
- 退市 adjusted manifest 覆盖 ≥ as-of
- PIT 股本最后 period_end ≤ as-of
- 交易日历 horizon 跨过 as-of 当月
- 目标 observation 目录尚不存在 (幂等)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import polars as pl


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    as_of: date
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if not c.ok)

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "ok": self.ok,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


# ────────────────────────── 纯谓词 ──────────────────────────

def check_enriched_latest(
    enriched_dates: list[date], as_of: date, *, staleness_days: int = 7
) -> CheckResult:
    name = "enriched_latest"
    if not enriched_dates:
        return CheckResult(name, False, "enriched 无任何日期分区")
    within = [d for d in enriched_dates if d <= as_of]
    if not within:
        return CheckResult(name, False, f"enriched 最早分区 {enriched_dates[0]} 晚于 as-of {as_of}")
    last = within[-1]
    if (as_of - last).days > staleness_days:
        return CheckResult(
            name, False, f"最新交易日 {last} 距 as-of {as_of} 超过 {staleness_days} 天"
        )
    return CheckResult(name, True, f"最新交易日 {last}")


def check_market_completeness(
    latest_symbols: int, baseline_symbols: int, *, ratio: float = 0.99
) -> CheckResult:
    name = "market_completeness"
    if baseline_symbols <= 0:
        return CheckResult(name, True, f"latest={latest_symbols} (无基线, 跳过)")
    if latest_symbols < baseline_symbols * ratio:
        return CheckResult(
            name, False, f"latest={latest_symbols} < baseline={baseline_symbols}*{ratio}"
        )
    return CheckResult(name, True, f"latest={latest_symbols} vs baseline={baseline_symbols}")


def check_generation(actual: str | None, expected: str) -> CheckResult:
    name = "generation"
    if actual != expected:
        return CheckResult(name, False, f"generation={actual}, expected={expected}")
    return CheckResult(name, True, f"generation={actual}")


def check_instruments_as_of(snapshot: date | None, as_of: date) -> CheckResult:
    name = "instruments_as_of"
    if snapshot != as_of:
        return CheckResult(name, False, f"instrument snapshot={snapshot}, expected as-of {as_of}")
    return CheckResult(name, True, f"as_of={snapshot}")


def check_source_coverage(name: str, covered_through: date | None, as_of: date) -> CheckResult:
    if covered_through is None:
        return CheckResult(name, False, "manifest 缺少 end 日期")
    if covered_through < as_of:
        return CheckResult(name, False, f"仅覆盖到 {covered_through}, 早于 as-of {as_of}")
    return CheckResult(name, True, f"覆盖到 {covered_through}")


def check_pit_shares(last_period_end: date | None, as_of: date) -> CheckResult:
    name = "pit_shares"
    if last_period_end is None:
        return CheckResult(name, False, "股本历史为空")
    if last_period_end > as_of:
        return CheckResult(name, False, f"最后 period_end {last_period_end} 晚于 as-of {as_of}")
    return CheckResult(name, True, f"最后 period_end {last_period_end}")


def check_unpublished_bars(
    latest_symbols: set[str],
    recent_symbols: set[str],
    *,
    tolerance: int,
) -> CheckResult:
    """最新分区缺席、但近几个交易日仍在交易的标的 (供应商尚未出数) 不得超过容忍度。

    停牌/退市标的长期无 bar, 属合法缺席; 而"最近还在交易、最新交易日却整只没有"只可能是
    数据未到 —— 这类缺口必须 fail-closed, 否则观察包会建在不完整数据上却报成功 (2026-10-08
    即因此让 2 个目标静默跳过, 且旧版 95% 中位数门照样放行)。
    """
    name = "unpublished_bars"
    missing = sorted(recent_symbols - latest_symbols)
    if len(missing) > tolerance:
        sample = ", ".join(missing[:5])
        return CheckResult(
            name, False, f"{len(missing)} 只近期有成交但最新分区缺失 (> {tolerance}; 样例 {sample})"
        )
    return CheckResult(name, True, f"缺失 {len(missing)} <= {tolerance}")


def check_calendar_horizon(calendar: tuple[date, ...], as_of: date) -> CheckResult:
    name = "calendar_horizon"
    if not calendar:
        return CheckResult(name, False, "交易日历为空")
    if not any((d.year, d.month) > (as_of.year, as_of.month) for d in calendar):
        return CheckResult(name, False, f"日历未跨过 {as_of:%Y-%m}")
    return CheckResult(name, True, f"日历到 {calendar[-1]}")


def check_observation_absent(path: Path) -> CheckResult:
    name = "observation_absent"
    if path.exists():
        return CheckResult(name, False, f"observation 已存在: {path}")
    return CheckResult(name, True, "目录不存在")


# ────────────────────────── IO 组装 ──────────────────────────

@dataclass(frozen=True)
class PreflightConfig:
    as_of: date
    expected_generation: str
    data_dir: Path
    output_root: Path
    trading_calendar: Path
    instruments: Path
    shares: Path
    st_manifest: Path
    delisted_coverage_manifest: Path
    staleness_days: int = 7
    completeness_ratio: float = 0.99


def _read_json_end(path: Path) -> date | None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    value = payload.get("end")
    return date.fromisoformat(str(value)) if value else None


def _read_enriched_dates(data_dir: Path) -> list[date]:
    base = data_dir / "kline_daily_enriched"
    return sorted(
        date.fromisoformat(p.name.removeprefix("date=")) for p in base.glob("date=*")
    )


def _partition_symbol_count(data_dir: Path, day: date) -> int:
    return len(_partition_symbols(data_dir, day))


def _partition_symbols(data_dir: Path, day: date) -> set[str]:
    part = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}" / "part.parquet"
    if not part.exists():
        return set()
    return set(pl.read_parquet(part, columns=["symbol"])["symbol"].to_list())


def _max_date_column(path: Path, column: str) -> date | None:
    frame = pl.read_parquet(path, columns=[column])
    if frame.is_empty():
        return None
    value = frame[column].max()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _read_calendar(path: Path) -> tuple[date, ...]:
    return tuple(
        date.fromisoformat(line.strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _read_generation(data_dir: Path) -> str | None:
    path = data_dir / ".matrix_generation_stock.json"
    if not path.exists():
        return None
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("generation"))
    except Exception:
        return None


def run_preflight(config: PreflightConfig) -> PreflightReport:
    dates = _read_enriched_dates(config.data_dir)
    latest = check_enriched_latest(dates, config.as_of, staleness_days=config.staleness_days)

    latest_count = 0
    baseline_count = 0
    latest_symbols: set[str] = set()
    recent_symbols: set[str] = set()
    within = [d for d in dates if d <= config.as_of]
    if within:
        latest_day = within[-1]
        latest_symbols = _partition_symbols(config.data_dir, latest_day)
        latest_count = len(latest_symbols)
        baseline_days = within[-21:-1]
        if baseline_days:
            counts = sorted(_partition_symbol_count(config.data_dir, d) for d in baseline_days)
            # 用 20 日**最小值**作基线 (中位数会掩盖单日缺口; 2026-10-08 的 95% 中位数门即漏过 1.8% 缺口)
            baseline_count = counts[0]
        for day in within[-6:-1]:
            recent_symbols |= _partition_symbols(config.data_dir, day)

    unpublished_tolerance = max(30, int(latest_count * 0.005))

    checks = (
        latest,
        check_market_completeness(
            latest_count, baseline_count, ratio=config.completeness_ratio
        ),
        check_unpublished_bars(
            latest_symbols, recent_symbols, tolerance=unpublished_tolerance
        ),
        check_generation(_read_generation(config.data_dir), config.expected_generation),
        check_instruments_as_of(_max_date_column(config.instruments, "as_of"), config.as_of),
        check_source_coverage("st_coverage", _read_json_end(config.st_manifest), config.as_of),
        check_source_coverage(
            "delisted_coverage",
            _read_json_end(config.delisted_coverage_manifest),
            config.as_of,
        ),
        check_pit_shares(_max_date_column(config.shares, "period_end"), config.as_of),
        check_calendar_horizon(_read_calendar(config.trading_calendar), config.as_of),
        check_observation_absent(config.output_root / f"as_of={config.as_of.isoformat()}"),
    )
    return PreflightReport(as_of=config.as_of, checks=checks)
