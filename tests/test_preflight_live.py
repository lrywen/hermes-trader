"""P1-6 — LIVE container preflight (scripts/preflight_live.py).

Host-side gate run before a deploy. The LIVE 10x container must be
running, healthy, never restarted, and show no fresh Python tracebacks
in the recent log window. Decision logic is pure; docker interaction is
a thin injectable collection layer.

Thresholds come from measured baseline (2026-09-17: 0 tracebacks in
1h/24h, healthy, restarts=0), not borrowed from any other project.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from scripts import preflight_live as pl


def _inspect_payload(running=True, health="healthy", restart_count=0,
                     with_health=True):
    state = {"Running": running}
    if with_health:
        state["Health"] = {"Status": health}
    return [{"State": state, "RestartCount": restart_count}]


# ---------------------------------------------------------------------------
# parse_inspect
# ---------------------------------------------------------------------------

def test_parse_inspect_healthy() -> None:
    s = pl.parse_inspect(_inspect_payload())
    assert s.running is True
    assert s.health == "healthy"
    assert s.restart_count == 0


def test_parse_inspect_without_health_is_none() -> None:
    s = pl.parse_inspect(_inspect_payload(with_health=False))
    assert s.health == "none"


@pytest.mark.parametrize("bad", [None, [], {}, [{"State": {}}]])
def test_parse_inspect_rejects_garbage(bad) -> None:
    if bad == [{"State": {}}]:
        s = pl.parse_inspect(bad)  # missing keys tolerated, defaults apply
        assert s.running is False and s.health == "none"
    else:
        with pytest.raises(ValueError):
            pl.parse_inspect(bad)


# ---------------------------------------------------------------------------
# traceback counting
# ---------------------------------------------------------------------------

def test_count_tracebacks() -> None:
    assert pl.count_tracebacks("clean log\nINFO all good\n") == 0
    log = "\n".join([
        "INFO start",
        "Traceback (most recent call last):",
        "  File x",
        "ERROR boom",
        "Traceback (most recent call last):",
        "  File y",
    ])
    assert pl.count_tracebacks(log) == 2


# ---------------------------------------------------------------------------
# decide — pure gate
# ---------------------------------------------------------------------------

def test_decide_passes_on_measured_baseline() -> None:
    status = pl.ContainerStatus(True, "healthy", 0)
    d = pl.decide(status, 0)
    assert d.ok and d.reasons == ()


@pytest.mark.parametrize(
    "status,tb,expected_fragment",
    [
        (pl.ContainerStatus(False, "healthy", 0), 0, "not running"),
        (pl.ContainerStatus(True, "unhealthy", 0), 0, "health=unhealthy"),
        (pl.ContainerStatus(True, "starting", 0), 0, "health=starting"),
        (pl.ContainerStatus(True, "none", 0), 0, "health=none"),
        (pl.ContainerStatus(True, "healthy", 2), 0, "restart_count=2"),
        (pl.ContainerStatus(True, "healthy", 0), 1, "traceback"),
    ],
)
def test_decide_failure_reasons(status, tb, expected_fragment) -> None:
    d = pl.decide(status, tb)
    assert not d.ok
    assert any(expected_fragment in r for r in d.reasons)


def test_decide_traceback_limit_threshold() -> None:
    status = pl.ContainerStatus(True, "healthy", 0)
    assert pl.decide(status, 2, max_tracebacks=2).ok
    assert not pl.decide(status, 3, max_tracebacks=2).ok


# ---------------------------------------------------------------------------
# collection layer (fake docker)
# ---------------------------------------------------------------------------

class FakeDocker:
    def __init__(self, payload=None, logs="", inspect_rc=0, logs_rc=0):
        self.payload = payload if payload is not None else _inspect_payload()
        self.logs = logs
        self.inspect_rc = inspect_rc
        self.logs_rc = logs_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]):
        self.calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(
                argv, self.inspect_rc, json.dumps(self.payload).encode(), b""
            )
        if argv[:2] == ["docker", "logs"]:
            assert argv[2] == "--since"
            return subprocess.CompletedProcess(argv, self.logs_rc,
                                               self.logs.encode(), b"")
        raise AssertionError(f"unexpected argv: {argv}")


def test_run_preflight_passes(fake=None) -> None:
    d = pl.run_preflight(container="hermes-trader", runner=FakeDocker())
    assert d.ok


def test_run_preflight_inspect_failure() -> None:
    d = pl.run_preflight(
        container="gone", runner=FakeDocker(inspect_rc=1, payload=[])
    )
    assert not d.ok
    assert any("inspect" in r for r in d.reasons)


def test_run_preflight_counts_logs_window() -> None:
    logs = "Traceback (most recent call last):\nERROR\n"
    d = pl.run_preflight(
        container="hermes-trader", since="2h",
        runner=FakeDocker(logs=logs),
    )
    assert not d.ok
    assert any("traceback" in r for r in d.reasons)


def test_main_exit_codes(capsys) -> None:
    assert pl.main([], runner=FakeDocker()) == 0
    out = capsys.readouterr().out
    assert "PASS" in out

    bad_logs = "Traceback (most recent call last):\n"
    rc = pl.main(["--since", "30m"], runner=FakeDocker(logs=bad_logs))
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
