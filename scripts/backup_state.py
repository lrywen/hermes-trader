#!/usr/bin/env python3
"""P1-6 — verified pre-deploy backup of the LIVE container's /data state.

Runs ON THE HOST. Every critical state file is pulled OUT of the
container with ``docker cp`` (the read-only direction; copying INTO the
container is forbidden by runbook §11) and then verified:

  1. local copy exists and is non-empty;
  2. local sha256 == in-container ``sha256sum`` output;
  3. JSON entries parse as JSON.

Only when EVERY manifest file verifies is ``backups/latest/
.backup-verified.json`` atomically written; the
``hermes_backup_age_seconds`` gauge reads that marker's ``ts``. A single
failure leaves the old (or no) marker in place, so a partial backup can
never advance backup_age.

Usage::

    uv run python scripts/backup_state.py
    uv run python scripts/backup_state.py --deploy-dir /home/ldy/hermes-deploy
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

DEFAULT_DEPLOY_DIR = Path("/home/ldy/hermes-deploy")
DEFAULT_CONTAINER = "hermes-trader"
DEFAULT_DATA_DIR = "/data"
BACKUPS_DIRNAME = "backups"
LATEST_DIRNAME = "latest"
MARKER_NAME = ".backup-verified.json"
MARKER_VERSION = 1
CMD_TIMEOUT_SECONDS = 120

UTC = timezone.utc


@dataclass(frozen=True)
class StateFile:
    name: str
    json_check: bool = True


# Funds / position / decision-state only. Excluded on purpose:
#   *.lock / *.bak / *.tmp, *.log and rotated logs, shadow *.jsonl
#   research output, the historical-candle cache, the derived
#   runtime_config.effective.json, and reconcile_status.json (a
#   reproducible last-run report, not decision state).
STATE_FILES: tuple[StateFile, ...] = (
    StateFile(".agent-config.json"),
    StateFile(".agent-memory.json"),
    StateFile(".dsl-state.json"),
    StateFile(".macro-regime-watch.json"),
    StateFile(".market-circuit.state"),
    StateFile(".pending-sl-retries.json"),
    StateFile(".positions-snapshot.json"),
    StateFile(".pullback-gate.state"),
    StateFile(".regime-overlay.state"),
    StateFile(".shadow-book.json"),
    StateFile(".surge-detector-state.json"),
    StateFile(".ws-user-fills-seen.json"),
    StateFile(".xs-reversal.state"),
    StateFile("ip_drift_state.json"),
)


@dataclass(frozen=True)
class FileResult:
    name: str
    ok: bool
    size: int = 0
    sha256: str = ""
    error: Optional[str] = None


@dataclass(frozen=True)
class BackupOutcome:
    run_dir: Path
    results: list[FileResult]
    marker_path: Optional[Path]
    exit_code: int


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_sha256_line(stdout: str) -> Optional[str]:
    """Parse '<64-hex>  /path' from sha256sum output."""
    line = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
    digest = line.split()[0] if line.split() else ""
    if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
        return digest
    return None


def validate_json_file(path: Path) -> None:
    """Raise ValueError if the file is not parseable JSON."""
    try:
        with path.open(encoding="utf-8") as fh:
            json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON in {path.name}: {exc}") from exc


def _subprocess_run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, timeout=CMD_TIMEOUT_SECONDS, check=False
    )


def backup_one(
    entry: StateFile,
    *,
    container: str,
    data_dir: str,
    run_dir: Path,
    runner: Runner,
) -> FileResult:
    """Copy one file out of the container and verify the copy."""
    name = entry.name
    remote_path = f"{data_dir}/{name}"
    dest = run_dir / name

    cp = runner(["docker", "cp", f"{container}:{remote_path}", str(dest)])
    if cp.returncode != 0:
        return FileResult(name, False, error=f"docker cp failed (rc={cp.returncode})")
    if not dest.is_file():
        return FileResult(name, False, error="copy missing after docker cp")
    size = dest.stat().st_size
    if size == 0:
        return FileResult(name, False, error="copied file is empty")

    hp = runner(["docker", "exec", container, "sha256sum", remote_path])
    if hp.returncode != 0:
        return FileResult(name, False, error="remote sha256sum failed")
    remote_sha = parse_sha256_line(
        hp.stdout.decode() if isinstance(hp.stdout, bytes) else hp.stdout
    )
    if not remote_sha:
        return FileResult(name, False, error="could not read remote sha256")

    local_sha = hash_file(dest)
    if local_sha != remote_sha:
        return FileResult(
            name, False, size=size,
            error="sha256 mismatch: local copy differs from container file",
        )

    if entry.json_check:
        try:
            validate_json_file(dest)
        except ValueError as exc:
            return FileResult(name, False, size=size, error=str(exc))

    return FileResult(name, True, size=size, sha256=local_sha)


def write_marker(
    marker_path: Path,
    *,
    ts: float,
    container: str,
    run_dir_name: str,
    results: list[FileResult],
) -> None:
    """Atomically write the verified marker (tmp file + os.replace)."""
    payload = {
        "version": MARKER_VERSION,
        "ts": ts,
        "utc": datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z"),
        "container": container,
        "backup_dir": run_dir_name,
        "files": [
            {"name": r.name, "size": r.size, "sha256": r.sha256}
            for r in results
        ],
    }
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker_path.with_name(f"{marker_path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, marker_path)
    finally:
        if tmp.exists():
            tmp.unlink()


def run_backup(
    deploy_dir: Path,
    *,
    container: str = DEFAULT_CONTAINER,
    data_dir: str = DEFAULT_DATA_DIR,
    now: Callable[[], float] = time.time,
    runner: Runner = _subprocess_run,
) -> BackupOutcome:
    ts = now()
    backups_root = deploy_dir / BACKUPS_DIRNAME
    base = "pre-deploy-" + datetime.fromtimestamp(ts, UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = backups_root / base
    suffix = 2
    while run_dir.exists():  # same-second re-run guard
        run_dir = backups_root / f"{base}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True)

    results = [
        backup_one(
            entry, container=container, data_dir=data_dir,
            run_dir=run_dir, runner=runner,
        )
        for entry in STATE_FILES
    ]

    marker_path: Optional[Path] = None
    if results and all(r.ok for r in results):
        marker_path = backups_root / LATEST_DIRNAME / MARKER_NAME
        write_marker(
            marker_path, ts=ts, container=container,
            run_dir_name=run_dir.name, results=results,
        )

    return BackupOutcome(
        run_dir=run_dir,
        results=results,
        marker_path=marker_path,
        exit_code=0 if marker_path is not None else 1,
    )


def _format_outcome(outcome: BackupOutcome) -> str:
    lines = [f"=== state backup -> {outcome.run_dir} ==="]
    for r in outcome.results:
        if r.ok:
            lines.append(f"  OK   {r.name} ({r.size} bytes, sha256 {r.sha256[:12]})")
        else:
            lines.append(f"  FAIL {r.name}: {r.error}")
    lines.append("=== summary ===")
    if outcome.exit_code == 0:
        lines.append(f"PASS: {len(outcome.results)} files verified, marker written")
    else:
        failed = [r.name for r in outcome.results if not r.ok]
        lines.append(f"FAIL: {len(failed)} file(s) not verified: {', '.join(failed)}")
        lines.append("marker NOT written — backup_age will not advance")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verified pre-deploy state backup")
    parser.add_argument("--deploy-dir", type=Path, default=DEFAULT_DEPLOY_DIR)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)
    outcome = run_backup(
        args.deploy_dir, container=args.container, data_dir=args.data_dir
    )
    print(_format_outcome(outcome))
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
