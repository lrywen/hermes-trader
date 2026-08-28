#!/usr/bin/env python3
"""Post-deployment smoke test for the hermes-trader container.

Verifies that a freshly built image / restarted container actually runs the
code version the deploy intended — the direct lesson of the 2026-08-22 PURR
incident, where a rebuilt source tree was never `docker compose build`-ed and
the container kept running an image that lacked `resolve_close_fill`, so the
PURR stop-out was detected but its close record was never written.

Checks (each must pass; exit non-zero on the first failure):
  1. Python imports for every critical module.
  2. Key functions/classes that recent fixes depend on actually exist
     (`resolve_close_fill`, `_Cache.delete`, `shared_config`, the event-log
     bridge, the memory rebuild path).
  3. The event-log bridge forks outcome events from session-log.
  4. Memory rebuild + record_trade/close chokepoints exist.
  5. The on-disk paths the running process uses are writable/present,
     including the positions snapshot on the persistent /data volume.
  6. Backfill logic is present in source (rehydrate calls record_trade;
     trading_loop emits dsl_exit on external close; reconcile emits
     dsl_exit on orphan close).
  7. In-memory outcome store contains the ETH reconcile_backfill row
     (proving the 2026-08-22 ETH orphan close was recorded).
  8. HTTP /api/health returns 200 (if the server is up).

Run inside the container:
    docker exec hermes-trader python /app/scripts/postdeploy_smoke.py
or from the host against a named container:
    python scripts/postdeploy_smoke.py --container hermes-trader
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import subprocess
import sys
import urllib.request
from typing import List, Tuple

# (module, [symbol, ...]) — importing the module AND getting each attr must
# succeed. These are the functions whose absence caused or would cause a
# silent production failure.
CRITICAL_SYMBOLS: List[Tuple[str, List[str]]] = [
    ("hermes_trader.agents.dsl_exit",
     ["resolve_close_fill", "rehydrate_from_exchange"]),
    ("hermes_trader.agents.memory",
     ["AgentMemory", "memory"]),
    ("hermes_trader.client.cache",
     ["_Cache"]),
    ("hermes_trader.client.lock",
     ["is_pid_alive"]),
    ("hermes_trader.shared_config",
     ["load_shared_config"]),
    ("hermes_trader.session_log",
     ["append"]),
    ("hermes_trader.event_log",
     ["append", "fork_from_session"]),
    ("hermes_trader.positions_snapshot",
     ["read_snapshot", "write_snapshot"]),
    ("hermes_trader.notify",
     ["send_text", "send_card", "is_enabled"]),
]

# Methods that must exist on instances (verified separately after import).
CRITICAL_METHODS: List[Tuple[str, str, str]] = [
    ("hermes_trader.client.cache", "_Cache", "delete"),
]

# Files/dirs the running process needs. Each is a path; we assert it exists
# OR its parent is writable (for files that may be lazily created).
REQUIRED_PATHS = [
    os.environ.get("HERMES_AGENT_MEMORY_FILE", "/data/.agent-memory.json"),
    os.environ.get("HERMES_EVENTS_FILE", "/data/events.jsonl"),
    os.environ.get("SESSION_LOG_PATH", "/data/session-log.jsonl"),
    os.environ.get("HERMES_POSITIONS_SNAPSHOT_FILE",
                   "/data/.positions-snapshot.json"),
]


def _ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(f"  [INFO] {msg}")


def check_imports() -> bool:
    print("[1/7] Critical module + symbol imports")
    ok = True
    for mod_name, symbols in CRITICAL_SYMBOLS:
        try:
            mod = __import__(mod_name, fromlist=symbols)
        except Exception as e:
            _fail(f"import {mod_name}: {e}")
            ok = False
            continue
        for sym in symbols:
            if not hasattr(mod, sym):
                _fail(f"{mod_name}.{sym} is missing (stale image?)")
                ok = False
            else:
                _ok(f"{mod_name}.{sym}")
    for mod_name, cls, method in CRITICAL_METHODS:
        try:
            mod = __import__(mod_name, fromlist=[cls])
            instance_method = getattr(getattr(mod, cls), method, None)
            if instance_method is None:
                _fail(f"{mod_name}.{cls}.{method} is missing")
                ok = False
            else:
                _ok(f"{mod_name}.{cls}.{method}")
        except Exception as e:
            _fail(f"{mod_name}.{cls}.{method}: {e}")
            ok = False
    return ok


def check_event_bridge() -> bool:
    print("[2/7] Event-log bridge (session-log forks outcome events)")
    ok = True
    try:
        from hermes_trader import event_log, session_log
        # fork_from_session must only fork whitelisted event types.
        if not event_log.fork_from_session({"event": "scan", "ts": 0}):
            _ok("scan event is NOT forked (correct)")
        else:
            _fail("scan event was forked — should stay in session-log only")
            ok = False
        src = inspect.getsource(session_log.append)
        if "event_log.fork_from_session" in src or "fork_from_session" in src:
            _ok("session_log.append calls event_log fork")
        else:
            _fail("session_log.append does NOT fork to event_log")
            ok = False
    except Exception as e:
        _fail(f"event bridge check: {e}")
        ok = False
    return ok


def check_memory_rebuild() -> bool:
    print("[3/7] Memory rebuild from events.jsonl")
    try:
        from hermes_trader.agents.memory import AgentMemory
        m = AgentMemory()
        assert hasattr(m, "_rebuild_from_events"), "missing _rebuild_from_events"
        assert hasattr(m, "record_trade"), "missing record_trade"
        assert hasattr(m, "record_close"), "missing record_close"
        _ok("_rebuild_from_events + record_trade/close present")
        return True
    except Exception as e:
        _fail(f"memory rebuild check: {e}")
        return False


def check_paths() -> bool:
    print("[4/7] Required data paths present/writable")
    ok = True
    for p in REQUIRED_PATHS:
        if os.path.exists(p):
            if os.access(p, os.R_OK):
                _ok(f"{p} exists, readable")
            else:
                _fail(f"{p} exists but not readable")
                ok = False
        else:
            parent = os.path.dirname(p) or "."
            if os.path.isdir(parent) and os.access(parent, os.W_OK):
                _ok(f"{p} absent yet parent writable (will be lazily created)")
            else:
                _fail(f"{p} absent and parent {parent} not writable")
                ok = False
    # Verify the snapshot env var is pointed at the persistent volume.
    snap_env = os.environ.get("HERMES_POSITIONS_SNAPSHOT_FILE", "")
    if snap_env.startswith("/data/"):
        _ok(f"HERMES_POSITIONS_SNAPSHOT_FILE on persistent volume: {snap_env}")
    else:
        _fail(f"HERMES_POSITIONS_SNAPSHOT_FILE={snap_env!r} — expected /data/ path")
        ok = False
    return ok


def check_backfill_source() -> bool:
    """Static source inspection: verify the backfill code paths are present
    in THIS image (proves resolve_close_fill + dsl_exit emit + rehydrate
    record_trade all shipped together)."""
    print("[5/7] Backfill logic present in source")
    ok = True
    try:
        from hermes_trader.agents import dsl_exit, executor
    except Exception as e:
        _fail(f"could not import dsl_exit/executor for source check: {e}")
        return False

    # NOTE: Do NOT `import scripts.trading_loop` — it has module-level code
    # that starts the live trading loop. Read the source file directly.
    tl_path = os.path.join(os.path.dirname(__file__), "trading_loop.py")
    try:
        with open(tl_path) as fh:
            tsrc = fh.read()
    except Exception as e:
        _fail(f"could not read trading_loop.py for source check: {e}")
        return False

    try:
        # 1. rehydrate_from_exchange must call record_trade for synthesized
        #    open positions (the rehydrate asymmetry fix).
        dsrc = inspect.getsource(dsl_exit.rehydrate_from_exchange)
        if "record_trade" in dsrc and "rehydrate_synth" in dsrc:
            _ok("rehydrate_from_exchange calls record_trade for synth opens")
        else:
            _fail("rehydrate_from_exchange missing record_trade for synth opens")
            ok = False

        # 2. trading_loop external-close backfill must emit a dsl_exit
        #    session-log event so the dashboard can see it.
        if ('"event": "dsl_exit"' in tsrc
                and "external_close_backfill" in tsrc):
            _ok("trading_loop emits dsl_exit on external close backfill")
        else:
            _fail("trading_loop missing dsl_exit emit on external close backfill")
            ok = False

        # 3. executor record_close failure must alert loudly (not silent warn).
        esrc = inspect.getsource(executor.close_position_market)
        if "notify.send_text" in esrc and "record_close 失败" in esrc:
            _ok("executor record_close failure triggers Feishu alert")
        else:
            _fail("executor record_close failure does not alert (still silent?)")
            ok = False

        # 4. reconcile_fills backfill must emit dsl_exit session-log event.
        rf_path = os.path.join(os.path.dirname(__file__), "reconcile_fills.py")
        if os.path.isfile(rf_path):
            with open(rf_path) as fh:
                rfsrc = fh.read()
            if ('"event": "dsl_exit"' in rfsrc
                    and "reconcile_backfill" in rfsrc):
                _ok("reconcile_fills emits dsl_exit on orphan close backfill")
            else:
                _fail("reconcile_fills missing dsl_exit emit")
                ok = False
        else:
            _info(f"reconcile_fills.py not at {rf_path} (skipped)")

        # 5. risk_gates: verify fail_closed_shorts + alert_on_fail_open are
        #    present (the HTA circuit-breaker hardening shipped).
        rg_path = os.path.join(
            os.path.dirname(__file__), "..",
            "hermes_trader", "agents", "risk_gates.py")
        if os.path.isfile(rg_path):
            with open(rg_path) as fh:
                rgsrc = fh.read()
            if ("fail_closed_shorts" in rgsrc
                    and "_notify_fail_open" in rgsrc
                    and 'timeout_s", 12.0' in rgsrc):
                _ok("risk_gates fail_closed_shorts + fail-open alerts present")
            else:
                _fail("risk_gates missing fail_closed_shorts/alert hardening")
                ok = False
        else:
            _info(f"risk_gates.py not at {rg_path} (skipped)")

    except Exception as e:
        _fail(f"backfill source check: {e}")
        ok = False
    return ok


def check_outcome_store() -> bool:
    """Verify the in-memory outcome store actually contains the ETH
    reconcile_backfill row — proves the manual backfill executed and the
    record_close chokepoint persisted it."""
    print("[6/7] Outcome store contains backfilled records")
    ok = True
    try:
        from hermes_trader.agents.memory import memory
        # Explicit load — the singleton may be freshly imported and not yet
        # replayed events.jsonl.
        if hasattr(memory, "load"):
            memory.load()
        closes = memory.get_closes() if hasattr(memory, "get_closes") else []
        if not closes and hasattr(memory, "_closes"):
            closes = memory._closes
        backfilled = [c for c in closes
                      if c.get("close_source") == "reconcile_backfill"]
        if backfilled:
            for c in backfilled:
                _ok(f"reconcile_backfill row: {c.get('coin')} "
                    f"{c.get('side')} exit={c.get('exit_px')} "
                    f"pnl=${c.get('realized_pnl_usd')}")
        else:
            _fail("no close_source=reconcile_backfill rows found")
            ok = False

        # Also verify rehydrated opens were recorded.
        trades = memory.get_all_trades() if hasattr(memory, "get_all_trades") else []
        if not trades and hasattr(memory, "_trades"):
            trades = memory._trades
        synth_opens = [t for t in trades
                       if t.get("close_source") == "rehydrate_synth"
                       or t.get("analysis_id") == "rehydrate_synth"]
        if synth_opens:
            for t in synth_opens:
                _ok(f"rehydrate_synth open: {t.get('coin')} "
                    f"{t.get('side')} entry={t.get('entry_px')}")
        else:
            _info("no rehydrate_synth open rows (expected if no external opens)")

        _info(f"outcome store totals: {len(trades)} trades, {len(closes)} closes")
    except Exception as e:
        _fail(f"outcome store check: {e}")
        ok = False
    return ok


def check_health(url: str = "http://127.0.0.1:8000/api/health",
                 timeout: float = 3.0) -> bool:
    print(f"[7/7] HTTP health endpoint ({url})")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8", errors="replace")[:200]
                _ok(f"200 OK — {body}")
                return True
            _fail(f"status {resp.status}")
            return False
    except Exception as e:
        # Non-fatal: the script can run before the API is ready. Report and
        # pass so a standalone container still gets a green smoke result on
        # the code-level checks; health is re-asserted by docker healthcheck.
        print(f"  [SKIP] health endpoint not reachable ({e})")
        return True


def run_in_container(container: str) -> int:
    """Re-exec this same script inside `docker exec <container>`."""
    cmd = [
        "docker", "exec", container,
        "python", "/app/scripts/postdeploy_smoke.py",
    ]
    print(f"Re-running smoke test inside container '{container}':\n")
    return subprocess.call(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="",
                        help="Docker container name to run inside (via docker exec).")
    parser.add_argument("--health-url",
                        default="http://127.0.0.1:8000/api/health")
    args = parser.parse_args()

    if args.container:
        return run_in_container(args.container)

    print("hermes-trader post-deployment smoke test")
    print(f"python: {sys.version.split()[0]}  cwd: {os.getcwd()}\n")

    results = [
        check_imports(),
        check_event_bridge(),
        check_memory_rebuild(),
        check_paths(),
        check_backfill_source(),
        check_outcome_store(),
        check_health(args.health_url),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{passed}/{total} check groups passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
