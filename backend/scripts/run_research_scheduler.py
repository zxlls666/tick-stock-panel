#!/usr/bin/env python3
"""Phase 11 external scheduler entry point for the research OOS observer.

One serial entry used by manual runs and (later) launchd/cron. It only orchestrates
existing commands — it never re-implements observer, ST, or delisting logic:

    sync daily bars -> sync instruments -> build ST history -> audit delisting coverage
        -> preflight (fail-closed) -> run_a_share_oos_observer

Rules (Phase 11):
- Same ``--as-of`` for every step; any non-zero step aborts immediately, so the observer
  never runs on partial upstream data and leaves no half-written observation.
- If the ``as_of=`` observation already exists, the run is a safe no-op (idempotent success).
- stdout/stderr/exit codes/generation/artifacts are logged per run under ``--log-root``.
- Fixed working directory + absolute paths + the same uv/Python environment as manual runs.

The two production syncs have no standalone CLI (their official entry is the running
backend's pipeline); supply ``--sync-daily-command`` / ``--sync-instruments-command``
(each a shell command string), or pass ``--skip-sync`` when the backend already synced.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.backtest.research_preflight import (
    PreflightConfig,
    PreflightReport,
    run_preflight,
)
from app.config import settings

OBSERVER_SCRIPT = "backend/scripts/run_a_share_oos_observer.py"
BUILD_ST_SCRIPT = "backend/scripts/build_a_share_st_history.py"
AUDIT_SCRIPT = "backend/scripts/audit_a_share_research_universe.py"


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple[str, ...] | None  # None = skipped (logged, not run)


@dataclass(frozen=True)
class SchedulerConfig:
    as_of: date
    expected_generation: str
    repo: Path
    data_dir: Path
    output_root: Path
    log_root: Path
    trading_calendar: Path
    exclusion_history: Path
    instruments: Path
    shares: Path
    st_history: Path
    st_manifest: Path
    delisted_instruments: Path
    delisted_coverage_manifest: Path
    delisted_adjusted_manifest: Path
    delisted_daily: Path
    skip_sync: bool = False
    dry_run: bool = False
    sync_daily_command: tuple[str, ...] | None = None
    sync_instruments_command: tuple[str, ...] | None = None
    build_st_command: tuple[str, ...] | None = None
    audit_command: tuple[str, ...] | None = None
    extra_steps: tuple[Step, ...] = field(default_factory=tuple)

    @property
    def observation(self) -> Path:
        return self.output_root / f"as_of={self.as_of.isoformat()}"

    @property
    def log_dir(self) -> Path:
        return self.log_root / f"as_of={self.as_of.isoformat()}"


def _resolve(command: tuple[str, ...] | None) -> tuple[str, ...] | None:
    return command


def build_steps(cfg: SchedulerConfig) -> list[Step]:
    """Build the serial pre-observer steps; None argv means "skipped"."""
    return [
        Step("sync_daily", _resolve(cfg.sync_daily_command)),
        Step("sync_instruments", _resolve(cfg.sync_instruments_command)),
        Step("build_st_history", _resolve(cfg.build_st_command)),
        Step("audit_delisting", _resolve(cfg.audit_command)),
        *cfg.extra_steps,
    ]


def build_observer_step(cfg: SchedulerConfig) -> Step:
    argv = (
        sys.executable,
        OBSERVER_SCRIPT,
        "--as-of", cfg.as_of.isoformat(),
        "--expected-generation", cfg.expected_generation,
        "--trading-calendar", str(cfg.trading_calendar),
        "--exclusion-history", str(cfg.exclusion_history),
        "--output-root", str(cfg.output_root),
        "--current-instruments", str(cfg.instruments),
        "--share-history", str(cfg.shares),
        "--st-history", str(cfg.st_history),
        "--st-manifest", str(cfg.st_manifest),
        "--delisted-instruments", str(cfg.delisted_instruments),
        "--delisted-coverage-manifest", str(cfg.delisted_coverage_manifest),
        "--delisted-adjusted-manifest", str(cfg.delisted_adjusted_manifest),
        "--delisted-daily", str(cfg.delisted_daily),
    )
    return Step("observer", argv)


def preflight_config(cfg: SchedulerConfig) -> PreflightConfig:
    return PreflightConfig(
        as_of=cfg.as_of,
        expected_generation=cfg.expected_generation,
        data_dir=cfg.data_dir,
        output_root=cfg.output_root,
        trading_calendar=cfg.trading_calendar,
        instruments=cfg.instruments,
        shares=cfg.shares,
        st_manifest=cfg.st_manifest,
        delisted_coverage_manifest=cfg.delisted_coverage_manifest,
    )


def run_command(step: Step, *, cwd: Path, env: dict, log_path: Path) -> int:
    """Run one step, tee-ing stdout/stderr into log_path. Returns the exit code."""
    if not step.argv:
        raise ValueError(f"step {step.name!r} has no command")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(f"$ {' '.join(step.argv)}\n")
        handle.flush()
        proc = subprocess.run(
            list(step.argv),
            cwd=str(cwd),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return int(proc.returncode)


def run_scheduler(
    cfg: SchedulerConfig,
    *,
    run_command_fn: Callable[..., int] = run_command,
    preflight_fn: Callable[[PreflightConfig], PreflightReport] = run_preflight,
) -> int:
    env = {
        **os.environ,
        "PYTHONPATH": str(cfg.repo / "backend"),
        "DATA_DIR": str(cfg.data_dir),
    }
    records: list[dict] = []

    def emit(**record: object) -> None:
        records.append(record)

    if cfg.observation.exists():
        emit(kind="idempotent_skip", reason="observation already exists", path=str(cfg.observation))
        _flush_log(cfg, records)
        print(f"[scheduler] observation already exists, idempotent no-op: {cfg.observation}")
        return 0

    for step in build_steps(cfg):
        if step.argv is None:
            emit(kind="step_skipped", step=step.name)
            continue
        rc = run_command_fn(step, cwd=cfg.repo, env=env, log_path=cfg.log_dir / f"{step.name}.log")
        emit(kind="step", step=step.name, exit_code=rc)
        if rc != 0:
            _flush_log(cfg, records)
            print(f"[scheduler] step {step.name} failed (exit {rc}); observer not run", file=sys.stderr)
            return rc

    report = preflight_fn(preflight_config(cfg))
    emit(kind="preflight", ok=report.ok, checks=report.as_dict()["checks"])
    print(json.dumps({"preflight": report.as_dict()}, ensure_ascii=False, indent=2))
    if not report.ok:
        _flush_log(cfg, records)
        print(
            "[scheduler] preflight failed: "
            + "; ".join(f"{c.name}: {c.detail}" for c in report.failures),
            file=sys.stderr,
        )
        return 2

    if cfg.dry_run:
        emit(kind="dry_run", note="preflight passed; observer not run")
        _flush_log(cfg, records)
        return 0

    observer = build_observer_step(cfg)
    rc = run_command_fn(observer, cwd=cfg.repo, env=env, log_path=cfg.log_dir / "observer.log")
    emit(kind="observer", exit_code=rc, artifacts=str(cfg.observation))
    if rc != 0:
        if cfg.observation.exists():
            emit(kind="observer_idempotent", note="observation appeared despite non-zero exit")
            _flush_log(cfg, records)
            print(f"[scheduler] observation already produced: {cfg.observation}")
            return 0
        _flush_log(cfg, records)
        print(f"[scheduler] observer failed (exit {rc})", file=sys.stderr)
        return rc
    _flush_log(cfg, records)
    print(f"[scheduler] observation written: {cfg.observation}")
    return 0


def _flush_log(cfg: SchedulerConfig, records: Sequence[dict]) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.log_dir / "scheduler.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _parse_command(value: str | None) -> tuple[str, ...] | None:
    return tuple(shlex.split(value)) if value else None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--expected-generation", required=True)
    parser.add_argument("--trading-calendar", type=Path, required=True)
    parser.add_argument("--exclusion-history", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=Path(settings.data_dir))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path, default=Path("data/research/a_share_oos"))
    parser.add_argument("--log-root", type=Path, default=None)
    parser.add_argument("--instruments", type=Path, default=Path("data/instruments/instruments.parquet"))
    parser.add_argument("--shares", type=Path, default=Path("data/financials/shares/part.parquet"))
    parser.add_argument("--st-history", type=Path, default=Path(
        "data/research/a_share_universe/20260928T-phase2-st-history-v1/st_history.parquet"))
    parser.add_argument("--st-manifest", type=Path, default=Path(
        "data/research/a_share_universe/20260928T-phase2-st-history-v1/manifest.json"))
    parser.add_argument("--delisted-instruments", type=Path, default=Path(
        "data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/delisted_instruments.parquet"))
    parser.add_argument("--delisted-coverage-manifest", type=Path, default=Path(
        "data/research/a_share_universe/20260928T-phase2-baostock-v1/manifest.json"))
    parser.add_argument("--delisted-adjusted-manifest", type=Path, default=Path(
        "data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/manifest.json"))
    parser.add_argument("--delisted-daily", type=Path, default=Path(
        "data/research/a_share_universe/20260928T-phase2-baostock-adjusted-v2/delisted_daily"))
    parser.add_argument("--skip-sync", action="store_true", help="上游同步已由后端完成, 跳过 sync 步骤")
    parser.add_argument("--dry-run", action="store_true", help="只跑步骤与 preflight, 不运行 observer")
    parser.add_argument("--sync-daily-command", default=None)
    parser.add_argument("--sync-instruments-command", default=None)
    parser.add_argument("--build-st-command", default=None)
    parser.add_argument("--audit-command", default=None)
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> SchedulerConfig:
    log_root = args.log_root or (args.output_root / "_scheduler_logs")
    return SchedulerConfig(
        as_of=args.as_of,
        expected_generation=args.expected_generation,
        repo=args.repo.resolve(),
        data_dir=args.data_dir.resolve(),
        output_root=args.output_root,
        log_root=log_root,
        trading_calendar=args.trading_calendar,
        exclusion_history=args.exclusion_history,
        instruments=args.instruments,
        shares=args.shares,
        st_history=args.st_history,
        st_manifest=args.st_manifest,
        delisted_instruments=args.delisted_instruments,
        delisted_coverage_manifest=args.delisted_coverage_manifest,
        delisted_adjusted_manifest=args.delisted_adjusted_manifest,
        delisted_daily=args.delisted_daily,
        skip_sync=args.skip_sync,
        dry_run=args.dry_run,
        sync_daily_command=_parse_command(args.sync_daily_command),
        sync_instruments_command=_parse_command(args.sync_instruments_command),
        build_st_command=_parse_command(args.build_st_command),
        audit_command=_parse_command(args.audit_command),
    )


def main(argv: Sequence[str] | None = None) -> int:
    cfg = config_from_args(parse_args(argv))
    return run_scheduler(cfg)


if __name__ == "__main__":
    sys.exit(main())
