"""R4 orchestration script: entrypoint + evidence mapping regression tests.

Pins the bug where ``gather_evidence`` treated the slim grade snapshot's
``windows`` list as a dict (crash on main), and confirms the script is
fail-closed when no arm has enough evidence.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts import prepare_canary_promotion as pcp  # noqa: E402


def _grade(**over):
    g = {
        "mature_outcomes": 0,
        "windows": [
            {"window_h": 24, "mature_outcomes": 0},
            {"window_h": 168, "mature_outcomes": 0},
        ],
    }
    g.update(over)
    return g


def test_gather_evidence_handles_windows_list():
    ev = pcp.gather_evidence(
        "signal_age_decay", {"signal_age_decay": _grade()},
        killswitch_tested=False, orderable=False)
    assert ev["mature_outcomes"] == 0
    # missing r5/independent/realistic fields stay None (fail closed)
    assert ev["net_edge_bps"] is None and ev["wfr"] is None


def test_gather_evidence_falls_back_to_widest_window():
    grade = _grade()
    del grade["mature_outcomes"]
    grade["windows"][1]["mature_outcomes"] = 7
    ev = pcp.gather_evidence(
        "a", {"a": grade}, killswitch_tested=False, orderable=False)
    assert ev["mature_outcomes"] == 7


def test_main_dry_run_does_not_crash(tmp_path, monkeypatch):
    hist = tmp_path / "shadow_grade_history.jsonl"
    hist.write_text(json.dumps({"arms": [_grade(arm="signal_age_decay")]})
                    + "\n")
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    rc = pcp.main(["--arms", "signal_age_decay"])
    assert rc == 0


def test_main_no_pending_file_when_unqualified(tmp_path, monkeypatch):
    hist = tmp_path / "shadow_grade_history.jsonl"
    hist.write_text(json.dumps({"arms": [_grade(arm="signal_age_decay")]})
                    + "\n")
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_CANARY_PIPELINE_FILE",
                       str(tmp_path / "pending.jsonl"))
    pcp.main(["--write", "--arms", "signal_age_decay"])
    assert not (tmp_path / "pending.jsonl").exists()
