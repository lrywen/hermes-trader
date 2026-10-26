"""Three-layer re-audit (2026-10-26 12:50) §4.3 — volume-profile failure visibility.

The observation-only volume_profile call in perception is wrapped in
try/except so an indicator bug can never drop a coin from the scan. That guard
used to be a *silent* `except: _vp = None`, which would also hide a SYSTEMATIC
failure (every coin getting vp=None while the heartbeat still reads "0 errors").

``_note_vp_failure`` now (a) counts per-scan failures (reset at scan start,
surfaced in the heartbeat as "<n> vp-fail"), and (b) logs the FIRST occurrence
of each distinct exception type exactly once so the cause is visible without
spamming one warning per coin per cycle. These tests pin that behaviour; they
exercise the real helpers, not mirrored log strings.
"""

from __future__ import annotations

import logging

import pytest

from hermes_trader.agents import perception


@pytest.fixture(autouse=True)
def _clean_state():
    perception._reset_vp_failures()
    perception._vp_warned.clear()
    yield
    perception._reset_vp_failures()
    perception._vp_warned.clear()


def test_vp_failures_count_and_reset():
    assert perception._get_vp_failures() == 0
    perception._note_vp_failure("AAA", ValueError("boom1"))
    perception._note_vp_failure("BBB", ValueError("boom2"))
    perception._note_vp_failure("CCC", KeyError("k"))
    assert perception._get_vp_failures() == 3
    perception._reset_vp_failures()
    assert perception._get_vp_failures() == 0  # per-scan reset


def test_vp_failure_warns_only_once_per_exception_type(caplog):
    with caplog.at_level(logging.WARNING, logger="hermes_trader.agents.perception"):
        for coin in ("AAA", "BBB", "CCC"):
            perception._note_vp_failure(coin, ValueError("x"))
    value_error_warns = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "volume_profile enrichment failed" in r.message
        and "ValueError" in r.message
    ]
    # Three failures, but only the FIRST ValueError emits a warning.
    assert len(value_error_warns) == 1
    assert "AAA" in value_error_warns[0].message
    assert perception._get_vp_failures() == 3

    # A distinct exception type warns once more; repeat ValueError stays silent.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hermes_trader.agents.perception"):
        perception._note_vp_failure("DDD", RuntimeError("y"))
        perception._note_vp_failure("EEE", ValueError("z"))
    warns2 = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns2) == 1
    assert "RuntimeError" in warns2[0].message
    assert perception._get_vp_failures() == 5
