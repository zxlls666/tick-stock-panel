"""Research-run manifest template for bias-repair studies.

Phase 0+ runs must freeze code identity, data fingerprints, parameters,
and known degradations so later deltas can be attributed to intentional
repairs rather than silent data drift.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REQUIRED_TOP_LEVEL_KEYS = (
    "schema_version",
    "run_id",
    "run_type",
    "created_at",
    "code",
    "data",
    "parameters",
    "degradations",
    "sources",
)

SCHEMA_VERSION = "bias-repair-manifest/v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_git_identity(repo: Path) -> dict[str, Any]:
    """Return commit SHA and dirty flag without reading secrets."""
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if rev.returncode != 0:
        raise RuntimeError(f"unable to read git HEAD in {repo}: {rev.stderr.strip()}")
    commit = rev.stdout.strip().lower()
    if not commit:
        raise RuntimeError(f"empty git HEAD in {repo}")

    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if porcelain.returncode != 0:
        raise RuntimeError(
            f"unable to read git status in {repo}: {porcelain.stderr.strip()}"
        )
    return {
        "git_commit": commit,
        "git_dirty": bool(porcelain.stdout.strip()),
    }


def file_fingerprint(path: Path, *, rows: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": path.as_posix(),
        "exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() else 0,
        "sha256": sha256_file(path) if path.exists() and path.is_file() else None,
    }
    if rows is not None:
        payload["rows"] = int(rows)
    return payload


def build_research_run_manifest(
    *,
    run_id: str,
    run_type: str,
    code: Mapping[str, Any],
    data: Mapping[str, Any],
    parameters: Mapping[str, Any],
    degradations: Sequence[str],
    sources: Mapping[str, Any] | Sequence[str] | None = None,
    extras: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a canonical research-run manifest.

    Required sections:
    - code: git commit / dirty
    - data: generation, SHA fingerprints, row counts, date bounds
    - parameters: frozen scenario knobs
    - degradations: explicit fail-closed / bias notes
    - sources: dataset provenance labels
    """
    if not run_id.strip():
        raise ValueError("run_id must be non-empty")
    if not run_type.strip():
        raise ValueError("run_type must be non-empty")
    if "git_commit" not in code:
        raise ValueError("code.git_commit is required")
    if not isinstance(degradations, Sequence) or isinstance(degradations, (str, bytes)):
        raise ValueError("degradations must be a sequence of strings")

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_type": run_type,
        "created_at": created_at or datetime.now(UTC).isoformat(),
        "code": dict(code),
        "data": dict(data),
        "parameters": dict(parameters),
        "degradations": [str(item) for item in degradations],
        "sources": (
            dict(sources)
            if isinstance(sources, Mapping)
            else list(sources or [])
        ),
    }
    if extras:
        overlap = set(extras) & set(REQUIRED_TOP_LEVEL_KEYS)
        if overlap:
            raise ValueError(f"extras collide with reserved keys: {sorted(overlap)}")
        manifest.update(dict(extras))

    missing = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in manifest]
    if missing:
        raise ValueError(f"manifest missing required keys: {missing}")
    return manifest


def write_research_run_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(manifest), ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def stable_json_sha256(payload: Mapping[str, Any]) -> str:
    """Fingerprint JSON with sorted keys; drops volatile fields for equality checks."""
    cleaned = {
        key: value
        for key, value in payload.items()
        if key not in {"created_at", "fingerprint"}
    }
    encoded = json.dumps(
        cleaned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)
