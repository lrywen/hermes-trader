"""Tests for the P1-6' CI test baseline guard (scripts/ci_test_guard.py)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_GUARD_PATH = _REPO / "scripts" / "ci_test_guard.py"

spec = importlib.util.spec_from_file_location("ci_test_guard", _GUARD_PATH)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def test_baseline_constants_are_sane():
    # A floor at the sampled 2026-09-18 offline count; warn ceiling above the
    # ~360s local reference so normal CI variance does not warn.
    assert guard.MIN_OFFLINE_TESTS >= 4089
    assert guard.WARN_TOTAL_WALLTIME_S > 360.0


def test_guard_count_passes_at_or_above_floor(monkeypatch, capsys):
    monkeypatch.setattr(guard, "_run_collect", lambda: (guard.MIN_OFFLINE_TESTS, ""))
    assert guard.guard_count() == 0
    monkeypatch.setattr(guard, "_run_collect", lambda: (guard.MIN_OFFLINE_TESTS + 50, ""))
    assert guard.guard_count() == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_guard_count_fails_below_floor(monkeypatch, capsys):
    monkeypatch.setattr(guard, "_run_collect", lambda: (guard.MIN_OFFLINE_TESTS - 1, ""))
    assert guard.guard_count() == 1
    err = capsys.readouterr().err
    assert "FAIL" in err and str(guard.MIN_OFFLINE_TESTS) in err


def test_run_collect_parses_post_deselect_count(monkeypatch):
    tail = "4081/4095 tests collected (14 deselected) in 3.30s"
    class _P:
        returncode = 0
        stdout = tail
        stderr = ""
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: _P())
    count, _ = guard._run_collect()
    assert count == 4081


def test_guard_walltime_warns_but_does_not_fail(tmp_path, capsys):
    junit = tmp_path / "out.xml"
    over = guard.WARN_TOTAL_WALLTIME_S + 100
    junit.write_text(
        f'<?xml version="1.0"?><testsuites>'
        f'<testsuite name="pytest" time="{over}" tests="1"/></testsuites>',
        encoding="utf-8")
    assert guard.guard_walltime(str(junit)) == 0  # warn-only
    assert "warning" in capsys.readouterr().out.lower()


def test_guard_walltime_ok_under_threshold(tmp_path, capsys):
    junit = tmp_path / "out.xml"
    junit.write_text(
        '<?xml version="1.0"?><testsuites>'
        '<testsuite name="pytest" time="120.5" tests="1"/></testsuites>',
        encoding="utf-8")
    assert guard.guard_walltime(str(junit)) == 0
    assert "walltime OK" in capsys.readouterr().out


def test_guard_walltime_missing_file_is_skip(tmp_path):
    assert guard.guard_walltime(str(tmp_path / "nope.xml")) == 0


def test_guard_script_not_on_runtime_whitelist():
    # CI tooling must never ship inside the runtime image (P0-1 whitelist).
    from scripts.runtime_whitelist import RUNTIME_SCRIPTS
    assert "ci_test_guard.py" not in RUNTIME_SCRIPTS
