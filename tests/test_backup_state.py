"""P1-6 — pre-deploy state backup (scripts/backup_state.py).

Runs ON THE HOST and pulls critical /data state OUT of the LIVE
container (read-only direction; ``docker cp`` INTO the container is
forbidden by runbook §11). A backup run only counts as verified when,
for every manifest file:

  1. ``docker cp`` succeeds and the local copy is a non-empty file;
  2. the local sha256 equals the in-container sha256sum output;
  3. JSON-checked entries parse as JSON.

Only when ALL entries verify is ``.backup-verified.json`` atomically
written under ``backups/latest/`` — that marker is what the
``hermes_backup_age_seconds`` gauge reads. Any single failure means no
marker, so backup_age never advances on a partial/failed backup.

The command layer is injected (``runner``) so tests never touch docker.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone

import pytest

from scripts import backup_state as bs


# ---------------------------------------------------------------------------
# Manifest: exact set of critical state files (regression guard vs drift)
# ---------------------------------------------------------------------------

EXPECTED_FILES = {
    ".agent-config.json",
    ".agent-memory.json",
    ".dsl-state.json",
    ".macro-regime-watch.json",
    ".market-circuit.state",
    ".pending-sl-retries.json",
    ".positions-snapshot.json",
    ".pullback-gate.state",
    ".regime-overlay.state",
    ".shadow-book.json",
    ".surge-detector-state.json",
    ".ws-user-fills-seen.json",
    ".xs-reversal.state",
    "ip_drift_state.json",
}


def test_manifest_pins_critical_state_files() -> None:
    assert {e.name for e in bs.STATE_FILES} == EXPECTED_FILES
    assert len(bs.STATE_FILES) == len(EXPECTED_FILES)  # no dupes


@pytest.mark.parametrize("entry", bs.STATE_FILES)
def test_manifest_excludes_derived_and_volatile_names(entry: bs.StateFile) -> None:
    n = entry.name
    assert not n.endswith(".lock")
    assert not n.endswith(".bak")
    assert not n.endswith(".tmp")
    assert not n.endswith(".log")
    assert ".snap" not in n
    assert n not in {
        "events.jsonl",
        "session-log.jsonl",
        ".historical-candles.json",
        "runtime_config.effective.json",
        "reconcile_status.json",
    }


def test_every_manifest_entry_is_json_checked() -> None:
    assert all(e.json_check for e in bs.STATE_FILES)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_sha256_known_vector(tmp_path) -> None:
    f = tmp_path / "x"
    f.write_bytes(b"abc")
    assert bs.hash_file(f) == hashlib.sha256(b"abc").hexdigest()


def test_parse_sha256_line() -> None:
    h = "a" * 64
    assert bs.parse_sha256_line(f"{h}  /data/.dsl-state.json\n") == h
    assert bs.parse_sha256_line("garbage") is None
    assert bs.parse_sha256_line("") is None


def test_validate_json_file(tmp_path) -> None:
    ok = tmp_path / "ok.json"
    ok.write_text('{"a": 1}')
    bs.validate_json_file(ok)  # does not raise

    bad = tmp_path / "bad.state"
    bad.write_text("{not json")
    with pytest.raises(ValueError):
        bs.validate_json_file(bad)


# ---------------------------------------------------------------------------
# Fake docker runner
# ---------------------------------------------------------------------------

class FakeDocker:
    """In-memory stand-in for the container's /data files."""

    def __init__(self, files: dict[str, bytes], fail_cp=(), tamper_hash=()):
        self.files = files
        self.fail_cp = set(fail_cp)
        self.tamper_hash = set(tamper_hash)
        self.cp_calls: list[list[str]] = []

    def __call__(self, argv: list[str]):
        if argv[:2] == ["docker", "cp"]:
            self.cp_calls.append(argv)
            src, dest = argv[2], argv[3]
            name = src.split(":", 1)[1].rsplit("/", 1)[-1]
            if name in self.fail_cp:
                return subprocess.CompletedProcess(argv, 1, b"", b"no such file")
            with open(dest, "wb") as fh:
                fh.write(self.files[name])
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[:2] == ["docker", "exec"]:
            remote_path = argv[-1]
            name = remote_path.rsplit("/", 1)[-1]
            payload = self.files[name]
            if name in self.tamper_hash:
                payload = payload + b"-tampered"
            digest = hashlib.sha256(payload).hexdigest()
            out = f"{digest}  {remote_path}\n"
            return subprocess.CompletedProcess(argv, 0, out.encode(), b"")
        raise AssertionError(f"unexpected argv: {argv}")


def _seed() -> dict[str, bytes]:
    return {e.name: json.dumps({"name": e.name, "i": i}).encode()
            for i, e in enumerate(bs.STATE_FILES)}


FIXED_NOW = datetime(2026, 9, 17, 3, 4, 5, tzinfo=timezone.utc).timestamp()


def _run(tmp_path, **kw):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    runner = FakeDocker(_seed(), **kw)
    outcome = bs.run_backup(
        deploy, now=lambda: FIXED_NOW, runner=runner,
    )
    return deploy, runner, outcome


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_full_backup_verifies_and_writes_marker(tmp_path) -> None:
    deploy, runner, outcome = _run(tmp_path)
    assert outcome.exit_code == 0
    assert all(r.ok for r in outcome.results)

    run_dir = deploy / "backups" / "pre-deploy-20260917-030405"
    assert run_dir.is_dir()
    for entry in bs.STATE_FILES:
        pulled = run_dir / entry.name
        assert pulled.read_bytes() == _seed()[entry.name]

    marker = deploy / "backups" / "latest" / bs.MARKER_NAME
    assert marker.is_file()
    data = json.loads(marker.read_text())
    assert data["version"] == 1
    assert data["ts"] == FIXED_NOW
    assert data["container"] == bs.DEFAULT_CONTAINER
    assert data["backup_dir"] == "pre-deploy-20260917-030405"
    assert {f["name"] for f in data["files"]} == EXPECTED_FILES
    for f in data["files"]:
        assert f["size"] > 0
        assert f["sha256"] == hashlib.sha256(_seed()[f["name"]]).hexdigest()

    # every file really went through an out-direction docker cp
    assert len(runner.cp_calls) == len(bs.STATE_FILES)
    for argv in runner.cp_calls:
        assert argv[2].startswith(f"{bs.DEFAULT_CONTAINER}:/data/")


def test_no_tmp_marker_left_behind(tmp_path) -> None:
    deploy, _, _ = _run(tmp_path)
    latest = deploy / "backups" / "latest"
    assert {p.name for p in latest.iterdir()} == {bs.MARKER_NAME}


def test_second_run_within_same_second_gets_distinct_dir_and_replaces_marker(tmp_path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    docker = FakeDocker(_seed())
    bs.run_backup(deploy, now=lambda: FIXED_NOW, runner=docker)
    outcome = bs.run_backup(deploy, now=lambda: FIXED_NOW, runner=docker)
    assert outcome.exit_code == 0
    assert outcome.run_dir.name == "pre-deploy-20260917-030405-2"
    marker = json.loads((deploy / "backups" / "latest" / bs.MARKER_NAME).read_text())
    assert marker["backup_dir"] == "pre-deploy-20260917-030405-2"


# ---------------------------------------------------------------------------
# Failure paths: marker must NEVER be written
# ---------------------------------------------------------------------------

def test_cp_failure_blocks_marker_but_continues_remaining_files(tmp_path) -> None:
    deploy, _, outcome = _run(tmp_path, fail_cp={".dsl-state.json"})
    assert outcome.exit_code == 1
    bad = next(r for r in outcome.results if r.name == ".dsl-state.json")
    assert not bad.ok and bad.error
    # other files were still attempted and verified
    assert sum(1 for r in outcome.results if r.ok) == len(bs.STATE_FILES) - 1
    assert not (deploy / "backups" / "latest" / bs.MARKER_NAME).exists()


def test_hash_mismatch_blocks_marker(tmp_path) -> None:
    deploy, _, outcome = _run(tmp_path, tamper_hash={".shadow-book.json"})
    assert outcome.exit_code == 1
    bad = next(r for r in outcome.results if r.name == ".shadow-book.json")
    assert "sha256" in bad.error
    assert not (deploy / "backups" / "latest" / bs.MARKER_NAME).exists()


def test_malformed_json_blocks_marker(tmp_path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    files = _seed()
    files[".positions-snapshot.json"] = b"{broken"
    outcome = bs.run_backup(deploy, now=lambda: FIXED_NOW, runner=FakeDocker(files))
    assert outcome.exit_code == 1
    bad = next(r for r in outcome.results if r.name == ".positions-snapshot.json")
    assert "json" in bad.error.lower()
    assert not (deploy / "backups" / "latest" / bs.MARKER_NAME).exists()


def test_missing_remote_hash_blocks_marker(tmp_path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()

    class NoHash(FakeDocker):
        def __call__(self, argv):
            if argv[:2] == ["docker", "exec"]:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return super().__call__(argv)

    outcome = bs.run_backup(deploy, now=lambda: FIXED_NOW, runner=NoHash(_seed()))
    assert outcome.exit_code == 1
    assert all(not r.ok for r in outcome.results)
    assert not (deploy / "backups" / "latest" / bs.MARKER_NAME).exists()
