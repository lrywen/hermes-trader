#!/usr/bin/env python3
"""P1-6 — LIVE container preflight gate, in-repo and unit-tested.

Host-side check to run before touching a deployment. The LIVE 10x
container is deploy-ready only when it is:

  * running,
  * docker-health ``healthy``,
  * RestartCount == 0 (a silent restart loop must block the deploy),
  * free of fresh Python tracebacks in the recent log window.

Defaults were set from the measured 2026-09-17 baseline (0 tracebacks
over 1h/24h, healthy, restarts=0). Decision logic is pure; the docker
calls live behind an injectable runner.

Usage::

    uv run python scripts/preflight_live.py
    uv run python scripts/preflight_live.py --since 30m --max-tracebacks 0
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

DEFAULT_CONTAINER = "hermes-trader"
DEFAULT_SINCE = "1h"
DEFAULT_MAX_TRACEBACKS = 0
TRACEBACK_MARKER = "Traceback (most recent call last):"
CMD_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class ContainerStatus:
    running: bool
    health: str  # healthy | starting | unhealthy | none
    restart_count: int


@dataclass(frozen=True)
class Decision:
    ok: bool
    reasons: tuple[str, ...]
    traceback_count: int = 0


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def parse_inspect(raw: object) -> ContainerStatus:
    """Parse the JSON array emitted by ``docker inspect``."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("docker inspect returned no objects")
    if not isinstance(raw[0], dict):
        raise ValueError("docker inspect object is not an object")
    item = raw[0]
    state = item.get("State")
    if not isinstance(state, dict):
        raise ValueError("inspect payload missing State")
    health = "none"
    health_block = state.get("Health")
    if isinstance(health_block, dict) and health_block.get("Status"):
        health = str(health_block["Status"])
    return ContainerStatus(
        running=bool(state.get("Running")),
        health=health,
        restart_count=int(item.get("RestartCount", 0) or 0),
    )


def count_tracebacks(logs: str) -> int:
    return logs.count(TRACEBACK_MARKER)


def decide(
    status: ContainerStatus,
    traceback_count: int,
    *,
    max_tracebacks: int = DEFAULT_MAX_TRACEBACKS,
) -> Decision:
    reasons: list[str] = []
    if not status.running:
        reasons.append("container not running")
    if status.health != "healthy":
        reasons.append(f"health={status.health}")
    if status.restart_count != 0:
        reasons.append(f"restart_count={status.restart_count}")
    if traceback_count > max_tracebacks:
        reasons.append(
            f"traceback_count={traceback_count} > max {max_tracebacks}"
        )
    return Decision(ok=not reasons, reasons=tuple(reasons),
                    traceback_count=traceback_count)


def _subprocess_run(argv: list[str]) -> subprocess.CompletedProcess:
    # docker logs writes the container log to stderr; merge into stdout.
    if argv[:2] == ["docker", "logs"]:
        return subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=CMD_TIMEOUT_SECONDS, check=False,
        )
    return subprocess.run(
        argv, capture_output=True, timeout=CMD_TIMEOUT_SECONDS, check=False
    )


def run_preflight(
    container: str = DEFAULT_CONTAINER,
    *,
    since: str = DEFAULT_SINCE,
    max_tracebacks: int = DEFAULT_MAX_TRACEBACKS,
    runner: Runner = _subprocess_run,
) -> Decision:
    inspect_proc = runner(["docker", "inspect", container])
    if inspect_proc.returncode != 0:
        return Decision(
            False,
            (f"docker inspect failed (rc={inspect_proc.returncode})",),
        )
    try:
        status = parse_inspect(
            json.loads(inspect_proc.stdout.decode()
                       if isinstance(inspect_proc.stdout, bytes)
                       else inspect_proc.stdout)
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return Decision(False, (f"inspect payload unreadable: {exc}",))

    logs_proc = runner(["docker", "logs", "--since", since, container])
    logs = (
        logs_proc.stdout.decode(errors="replace")
        if isinstance(logs_proc.stdout, bytes)
        else (logs_proc.stdout or "")
    )
    return decide(status, count_tracebacks(logs),
                  max_tracebacks=max_tracebacks)


def _format(container: str, since: str, decision: Decision) -> str:
    lines = ["=== LIVE preflight ===", f"container={container} since={since}"]
    if decision.ok:
        lines.append("PASS: running, healthy, no restarts, no fresh tracebacks")
    else:
        lines.append("FAIL:")
        lines.extend(f"  - {r}" for r in decision.reasons)
    return "\n".join(lines)


def main(
    argv: Optional[list[str]] = None,
    *,
    runner: Runner = _subprocess_run,
) -> int:
    parser = argparse.ArgumentParser(description="LIVE container preflight")
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help="docker logs --since window (default: %(default)s)")
    parser.add_argument("--max-tracebacks", type=int,
                        default=DEFAULT_MAX_TRACEBACKS)
    args = parser.parse_args(argv)
    decision = run_preflight(
        args.container, since=args.since,
        max_tracebacks=args.max_tracebacks, runner=runner,
    )
    print(_format(args.container, args.since, decision))
    return 0 if decision.ok else 1


if __name__ == "__main__":
    sys.exit(main())
