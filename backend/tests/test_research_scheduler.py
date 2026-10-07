"""run_research_scheduler 的编排测试 (Phase 11)。

用注入的 run_command_fn / preflight_fn 覆盖: 串行构建、fail-fast、preflight 门、
dry-run、幂等短路、observer 竞态; 另测真实 run_command 的退出码与日志落盘。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from app.backtest.research_preflight import CheckResult, PreflightReport
from scripts.run_research_scheduler import (
    SchedulerConfig,
    Step,
    build_observer_step,
    build_steps,
    config_from_args,
    parse_args,
    run_command,
    run_scheduler,
)

AS_OF = date(2026, 9, 30)


def _cfg(tmp_path: Path, **overrides) -> SchedulerConfig:
    base = dict(
        as_of=AS_OF,
        expected_generation="g1",
        repo=tmp_path,
        data_dir=tmp_path / "data",
        output_root=tmp_path / "out",
        log_root=tmp_path / "logs",
        trading_calendar=tmp_path / "cal.txt",
        exclusion_history=tmp_path / "exclusion.parquet",
        instruments=tmp_path / "inst.parquet",
        shares=tmp_path / "shares.parquet",
        st_history=tmp_path / "st.parquet",
        st_manifest=tmp_path / "st.json",
        delisted_instruments=tmp_path / "d_inst.parquet",
        delisted_coverage_manifest=tmp_path / "d_cov.json",
        delisted_adjusted_manifest=tmp_path / "d_adj.json",
        delisted_daily=tmp_path / "d_daily",
    )
    base.update(overrides)
    return SchedulerConfig(**base)


class _Recorder:
    def __init__(self, codes: dict[str, int] | None = None, on_observer=None) -> None:
        self.calls: list[str] = []
        self.codes = codes or {}
        self.on_observer = on_observer

    def __call__(self, step, *, cwd, env, log_path) -> int:
        self.calls.append(step.name)
        if step.name == "observer" and self.on_observer is not None:
            self.on_observer()
        return self.codes.get(step.name, 0)


def _report(ok: bool) -> PreflightReport:
    return PreflightReport(as_of=AS_OF, checks=(CheckResult("x", ok, "" if ok else "bad"),))


# ────────────────────────── 纯构建 ──────────────────────────

def test_build_steps_order_and_skip(tmp_path) -> None:
    steps = build_steps(_cfg(tmp_path))
    assert [s.name for s in steps] == ["sync_daily", "sync_instruments", "build_st_history", "audit_delisting"]
    assert all(s.argv is None for s in steps)  # 未配置 → 跳过

    configured = build_steps(_cfg(
        tmp_path,
        sync_daily_command=("sync-daily",),
        sync_instruments_command=("sync-inst",),
        build_st_command=("build-st",),
        audit_command=("audit",),
    ))
    assert [s.argv for s in configured] == [("sync-daily",), ("sync-inst",), ("build-st",), ("audit",)]


def test_build_observer_step_carries_required_flags(tmp_path) -> None:
    step = build_observer_step(_cfg(tmp_path))
    assert step.name == "observer"
    argv = list(step.argv or ())
    assert "--as-of" in argv and "2026-09-30" in argv
    assert "--expected-generation" in argv and "g1" in argv
    assert "--exclusion-history" in argv
    assert "--output-root" in argv


# ────────────────────────── 真实 run_command ──────────────────────────

def test_run_command_returns_code_and_logs(tmp_path) -> None:
    log = tmp_path / "ok.log"
    assert run_command(Step("ok", (sys.executable, "-c", "print('hi')")),
                       cwd=tmp_path, env={}, log_path=log) == 0
    assert "hi" in log.read_text(encoding="utf-8")

    assert run_command(Step("bad", (sys.executable, "-c", "import sys; sys.exit(3)")),
                       cwd=tmp_path, env={}, log_path=tmp_path / "bad.log") == 3


# ────────────────────────── 编排 ──────────────────────────

def test_idempotent_short_circuit(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    (cfg.observation).mkdir(parents=True)
    recorder = _Recorder()
    assert run_scheduler(cfg, run_command_fn=recorder, preflight_fn=lambda c: _report(True)) == 0
    assert recorder.calls == []


def test_fail_fast_on_step_error(tmp_path) -> None:
    cfg = _cfg(tmp_path, sync_daily_command=("x",), sync_instruments_command=("y",))
    recorder = _Recorder(codes={"sync_daily": 3})
    assert run_scheduler(cfg, run_command_fn=recorder, preflight_fn=lambda c: _report(True)) == 3
    assert recorder.calls == ["sync_daily"]  # 后续步骤与 observer 未运行


def test_preflight_failure_blocks_observer(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    recorder = _Recorder()
    assert run_scheduler(cfg, run_command_fn=recorder, preflight_fn=lambda c: _report(False)) == 2
    assert "observer" not in recorder.calls
    assert (cfg.log_dir / "scheduler.jsonl").exists()


def test_dry_run_skips_observer(tmp_path) -> None:
    cfg = _cfg(tmp_path, dry_run=True)
    recorder = _Recorder()
    assert run_scheduler(cfg, run_command_fn=recorder, preflight_fn=lambda c: _report(True)) == 0
    assert recorder.calls == []


def test_success_runs_observer_and_logs(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    recorder = _Recorder()
    assert run_scheduler(cfg, run_command_fn=recorder, preflight_fn=lambda c: _report(True)) == 0
    assert recorder.calls == ["observer"]
    records = [json.loads(line) for line in (cfg.log_dir / "scheduler.jsonl").read_text().splitlines()]
    assert any(r.get("kind") == "observer" and r.get("exit_code") == 0 for r in records)


def test_observer_failure_with_existing_observation_is_idempotent(tmp_path) -> None:
    cfg = _cfg(tmp_path)

    def _create() -> None:
        cfg.observation.mkdir(parents=True)

    recorder = _Recorder(codes={"observer": 1}, on_observer=_create)
    assert run_scheduler(cfg, run_command_fn=recorder, preflight_fn=lambda c: _report(True)) == 0


def test_observer_failure_without_observation_propagates(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    recorder = _Recorder(codes={"observer": 4})
    assert run_scheduler(cfg, run_command_fn=recorder, preflight_fn=lambda c: _report(True)) == 4


def test_config_from_args_parses_commands() -> None:
    cfg = config_from_args(parse_args([
        "--as-of", "2026-09-30",
        "--expected-generation", "g1",
        "--trading-calendar", "cal.txt",
        "--exclusion-history", "excl.parquet",
        "--sync-daily-command", "uv run python sync_daily.py --start 2026-09-29",
        "--skip-sync",
    ]))
    assert cfg.sync_daily_command == ("uv", "run", "python", "sync_daily.py", "--start", "2026-09-29")
    assert cfg.skip_sync is True
    assert cfg.sync_instruments_command is None
