"""Tests for research-run manifest template."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.backtest.research_run_manifest import (
    REQUIRED_TOP_LEVEL_KEYS,
    build_research_run_manifest,
    sha256_file,
    stable_json_sha256,
    write_research_run_manifest,
)


def test_build_research_run_manifest_requires_core_sections() -> None:
    manifest = build_research_run_manifest(
        run_id="phase0",
        run_type="phase0_data_baseline_audit",
        code={"git_commit": "abc", "git_dirty": False},
        data={
            "data_generation": "gen",
            "shares": {"rows": 1, "sha256": "x"},
        },
        parameters={"phase": 0},
        degradations=["st_status_from_current_name_not_pit"],
        sources={"shares": "data/financials/shares/part.parquet"},
        created_at="2026-09-28T00:00:00+00:00",
    )
    for key in REQUIRED_TOP_LEVEL_KEYS:
        assert key in manifest
    assert manifest["schema_version"] == "bias-repair-manifest/v1"
    assert manifest["degradations"] == ["st_status_from_current_name_not_pit"]


def test_build_research_run_manifest_rejects_missing_commit() -> None:
    with pytest.raises(ValueError, match="git_commit"):
        build_research_run_manifest(
            run_id="phase0",
            run_type="audit",
            code={"git_dirty": False},
            data={},
            parameters={},
            degradations=[],
        )


def test_stable_json_sha256_ignores_created_at(tmp_path: Path) -> None:
    first = build_research_run_manifest(
        run_id="phase0",
        run_type="audit",
        code={"git_commit": "abc", "git_dirty": True},
        data={"rows": 10},
        parameters={"phase": 0},
        degradations=["a"],
        sources=["local"],
        created_at="2026-09-28T00:00:00+00:00",
    )
    second = build_research_run_manifest(
        run_id="phase0",
        run_type="audit",
        code={"git_commit": "abc", "git_dirty": True},
        data={"rows": 10},
        parameters={"phase": 0},
        degradations=["a"],
        sources=["local"],
        created_at="2026-09-28T12:00:00+00:00",
    )
    assert stable_json_sha256(first) == stable_json_sha256(second)

    path = tmp_path / "manifest.json"
    write_research_run_manifest(path, first)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["run_id"] == "phase0"
    assert sha256_file(path) == sha256_file(path)
